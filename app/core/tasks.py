"""后台任务编排：asyncio 任务表 + SSE 事件流 + 可中断。

为什么不用 Celery：
  MVP 阶段引入 broker 会显著抬高部署成本，而本项目的任务是"秒级到十秒级"的
  Agent 链路，用 asyncio 足够；压测阶段换 Redis/Celery 时接口不变。

写并发收敛不在这里，而在 app/db.py 的事务提交层（全局写锁）。
上一版曾在此实现 serialize_write() 但从未接线，已删除 —— 保留一份不生效的
实现比没有更危险，它会让文档与注释撒谎。

多 worker 支持（见 core/task_store.py）：
  **执行留在本进程**（asyncio 句柄不可跨进程传递），但状态/事件/取消信号镜像到
  Redis。于是任意 worker 都能查询任务详情、订阅事件流、发起取消 —— 解决
  "提交在 A、轮询被负载均衡到 B 就 404"的问题。未配置 Redis 时全部走进程内，
  行为与改造前完全一致。

对外契约：
  submit(kind, payload, user_id) -> task_id
  events(task_id) -> AsyncIterator[dict]   # SSE
  cancel(task_id) -> bool
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from . import task_store

# 任务状态锁：_TASKS 会被多个协程读写（提交/取消/SSE 轮询），需要保护
_tasks_guard = asyncio.Lock()
_tasks_guard_loop: asyncio.AbstractEventLoop | None = None


def _get_task_guard() -> asyncio.Lock:
    global _tasks_guard, _tasks_guard_loop
    loop = asyncio.get_running_loop()
    if _tasks_guard_loop is not loop:
        _tasks_guard = asyncio.Lock()
        _tasks_guard_loop = loop
    return _tasks_guard


# --------------------------------------------------------------- 任务状态
@dataclass
class TaskState:
    id: str
    user_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending|running|done|failed|cancelled
    progress: float = 0.0
    stage: str = ""
    cancel_requested: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    handle: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stage": self.stage,
            "error": self.error,
            "result": self.result if self.status in ("done", "failed") else {},
            "elapsed_ms": int(((self.finished_at or time.time()) - self.created_at) * 1000),
        }


_TASKS: dict[str, TaskState] = {}


def get_task(task_id: str) -> TaskState | None:
    """取任务对象。**只在本进程有效** —— 跨进程查询请用 task_detail()。"""
    return _TASKS.get(task_id)


def task_detail(task_id: str) -> dict[str, Any] | None:
    """跨进程的任务快照：本地有则用本地（最新），否则从 Redis 镜像读。

    这个函数是 API 层应当使用的入口 —— 用 get_task() 会让多 worker 部署下
    轮询随机 404（本地字典里没有别的 worker 创建的任务）。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        return task.snapshot()
    return task_store.read_state(task_id)


def task_owner(task_id: str) -> str | None:
    """取任务属主 user_id（属主鉴权用；本地与 Redis 镜像同 task_detail 的顺序）。

    返回 None 表示任务不存在（本地无、镜像也无）；返回空串表示存在但
    拿不到属主（理论上不会发生，调用方按"非本人"处理）。

    安全背景：任务详情/取消/SSE 此前只验证"登录"不验证"是谁的"，
    任何用户拿别人的 task_id 就能读代码与 Agent 输出、取消别人的任务。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        return task.user_id
    state = task_store.read_state(task_id)
    if state is not None:
        return str(state.get("user_id") or "")
    return None


def list_tasks(user_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """列出任务（本地 + Redis 镜像合并，按创建时间倒序）。

    合并策略：本地任务优先（信息最新），Redis 里补上"别的 worker 创建的"。
    同一 task_id 只出现一次。
    """
    merged: dict[str, dict[str, Any]] = {}
    for t in _TASKS.values():
        if not user_id or t.user_id == user_id:
            merged[t.id] = t.snapshot()

    if user_id:
        # Redis 侧：先清理过期索引，再逐个读状态
        task_store.cleanup_expired_index(user_id)
        for tid in task_store.list_user_task_ids(user_id, limit=limit):
            if tid in merged:
                continue
            state = task_store.read_state(tid)
            if state is not None:
                merged[tid] = _state_to_snapshot(state)

    items = sorted(merged.values(), key=lambda s: s.get("created_at", 0), reverse=True)
    return items[:limit]


def _state_to_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """把 Redis 镜像状态转成与 TaskState.snapshot() 同构的 dict。"""
    created = float(state.get("created_at") or 0)
    finished = state.get("finished_at")
    status = str(state.get("status", "pending"))
    return {
        "task_id": state.get("task_id", ""),
        "status": status,
        "progress": float(state.get("progress") or 0),
        "stage": state.get("stage", ""),
        "error": state.get("error", ""),
        "result": state.get("result") if status in ("done", "failed") else {},
        "elapsed_ms": int(((finished or time.time()) - created) * 1000),
        "created_at": created,
        "owner": state.get("owner", ""),
    }


def _emit(task: TaskState, event: str, data: dict[str, Any] | None = None) -> None:
    evt = {"event": event, "data": data or {}, "ts": round(time.time(), 4)}
    task.events.append(evt)
    # 同步镜像到 Redis（未配置时是 no-op）
    task_store.publish_event(task.id, evt)


def _advance(task: TaskState, progress: float, stage: str) -> None:
    task.progress = max(task.progress, min(1.0, progress))
    task.stage = stage
    _emit(task, "progress", {"progress": task.progress, "stage": stage})
    task_store.publish_state(task)


# --------------------------------------------------------------- 任务执行
Pipeline = Callable[[TaskState], Awaitable[dict[str, Any]]]


# 并发上限（清单 N4）：防止多用户同时刷题时同时拉起海量 Agent 链路打爆 LLM 配额。
# 超限不是拒绝，而是排队等待 —— 用户体验是"慢一点"，而不是"失败"。
_concurrency: asyncio.Semaphore | None = None
_concurrency_limit: int = 0
_concurrency_loop: asyncio.AbstractEventLoop | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """取当前事件循环的并发信号量（循环变化时重建）。"""
    global _concurrency, _concurrency_limit, _concurrency_loop
    loop = asyncio.get_running_loop()
    limit = max(1, settings.MAX_CONCURRENT_PIPELINES)
    if _concurrency is None or _concurrency_loop is not loop or _concurrency_limit != limit:
        _concurrency = asyncio.Semaphore(limit)
        _concurrency_limit = limit
        _concurrency_loop = loop
    return _concurrency


def submit(
    kind: str,
    pipeline: Pipeline,
    *,
    user_id: str = "",
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> TaskState:
    """注册并启动一个后台任务，立刻返回 task_id（不阻塞请求）。"""
    tid = task_id or uuid.uuid4().hex
    task = TaskState(id=tid, user_id=user_id, kind=kind, payload=payload or {})
    _TASKS[tid] = task
    _emit(task, "created", {"kind": kind})
    task_store.publish_state(task)  # 让别的 worker 立刻能查到（含 user_id 索引）
    task.handle = asyncio.create_task(_run(task, pipeline))
    return task


async def _run(task: TaskState, pipeline: Pipeline) -> None:
    sem = _get_semaphore()
    if sem.locked():
        task.stage = "queued"
        _emit(task, "queued", {"reason": f"并发已达上限 {_concurrency_limit}，排队中"})
        task_store.publish_state(task)
    async with sem:
        await _run_inner(task, pipeline)


async def _run_inner(task: TaskState, pipeline: Pipeline) -> None:
    task.status = "running"
    _emit(task, "started", {})
    task_store.publish_state(task)
    watcher: asyncio.Task | None = None
    try:
        # 跨进程取消：别的 worker 收到 cancel 请求时写 Redis 信号，
        # 由 owner 侧这个轮询任务感知并取消本地句柄（句柄不能跨进程传）。
        watcher = asyncio.create_task(_watch_remote_cancel(task))
        result = await pipeline(task)
        if task.cancel_requested:
            task.status = "cancelled"
            _emit(task, "cancelled", {})
        else:
            task.status = "done"
            task.result = result or {}
            task.progress = 1.0
            _emit(task, "done", task.result)
    except asyncio.CancelledError:
        task.status = "cancelled"
        _emit(task, "cancelled", {})
        raise
    except Exception as e:
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        _emit(task, "failed", {"error": task.error})
    finally:
        task.finished_at = time.time()
        if task.handle is not None:
            task.handle = None
        if watcher is not None and not watcher.done():
            watcher.cancel()
        task_store.publish_state(task)  # 终态必须落库，否则别处永远看到 running


async def _watch_remote_cancel(task: TaskState, interval: float = 1.0) -> None:
    """轮询 Redis 取消信号，命中则取消本地执行。

    为什么是轮询而不是订阅（pub/sub）：取消是低频操作，轮询实现简单且不需要
    维护订阅连接的生命周期（订阅断了会静默失效，反而更难排查）。间隔 1 秒对
    "用户点取消"这个交互来说足够及时。
    """
    try:
        while True:
            await asyncio.sleep(interval)
            if task.cancel_requested:
                return
            if task_store.is_cancel_requested(task.id):
                task.cancel_requested = True
                task_store.publish_state(task)
                if task.handle is not None:
                    task.handle.cancel()
                return
    except asyncio.CancelledError:
        return


def cancel(task_id: str) -> bool:
    """取消任务。本地有则直接取消；本地没有（别的 worker 创建的）则写 Redis 信号。

    返回值语义：True = 取消请求已生效（本地已取消，或信号已投递）。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        if task.status in ("done", "failed", "cancelled"):
            return False
        task.cancel_requested = True
        if task.handle is not None:
            task.handle.cancel()
        task_store.publish_state(task)
        return True

    # 本地没有 -> 可能是别的 worker 的任务：写跨进程取消信号
    state = task_store.read_state(task_id)
    if state is None:
        return False  # 任务根本不存在（或已过期）
    if state.get("status") in ("done", "failed", "cancelled"):
        return False
    return task_store.request_cancel(task_id)


async def wait(task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """同步等待任务完成（测试与内部调用用）。

    跨进程场景下读 Redis 镜像 —— 但轮询间隔要比进程内更宽松（Redis 往返
    比内存读贵得多），因此这里用 2 倍 poll interval。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        deadline = time.time() + timeout
        while task.status in ("pending", "running") and time.time() < deadline:
            await asyncio.sleep(settings.TASK_POLL_INTERVAL)
        return task.snapshot()

    # 本地没有 -> 轮询 Redis 镜像
    interval = max(0.3, settings.TASK_POLL_INTERVAL * 2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = task_store.read_state(task_id)
        if state is None:
            raise KeyError(f"未知任务: {task_id}")
        if state.get("status") in ("done", "failed", "cancelled"):
            return _state_to_snapshot(state)
        await asyncio.sleep(interval)
    state = task_store.read_state(task_id)
    if state is None:
        raise KeyError(f"未知任务: {task_id}")
    return _state_to_snapshot(state)


async def events(task_id: str, from_index: int = 0) -> AsyncIterator[dict[str, Any]]:
    """SSE 事件流：先补发历史事件，再增量推送，任务终止后自动收尾。

    两种数据源：
      * 任务在本进程（owner）-> 直接读内存 deque，延迟最低；
      * 任务在别的 worker -> 读 Redis 事件列表（镜像），按索引增量拉取。

    owner 判定用 task_store 里记录的 instance_id —— 比"本地字典里有没有"
    更准确（本地字典可能因 cleanup 清掉了已完成任务，但状态镜像还在）。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        async for evt in _events_local(task, from_index):
            yield evt
        return

    # 跨进程路径
    state = task_store.read_state(task_id)
    if state is None:
        yield {"event": "error", "data": {"error": f"未知任务: {task_id}"}}
        return
    async for evt in _events_remote(task_id, state, from_index):
        yield evt


async def _events_local(task: TaskState, from_index: int) -> AsyncIterator[dict[str, Any]]:
    """owner 路径：内存事件流（原实现，语义不变）。"""
    idx = from_index
    while True:
        while idx < len(task.events):
            evt = task.events[idx]
            idx += 1
            yield evt
        if task.status in ("done", "failed", "cancelled"):
            if idx >= len(task.events):
                yield {"event": "closed", "data": task.snapshot()}
                return
            continue
        if task.cancel_requested and task.handle is None:
            yield {"event": "closed", "data": task.snapshot()}
            return
        await asyncio.sleep(settings.TASK_POLL_INTERVAL)


async def _events_remote(
    task_id: str, initial_state: dict[str, Any], from_index: int
) -> AsyncIterator[dict[str, Any]]:
    """非 owner 路径：轮询 Redis 事件列表。

    终止判定用**Redis 里的状态**而不是本地字典 —— 任务可能已结束但事件还在
    写入（终态事件与状态更新之间有一个极短窗口），因此要"事件读完 + 状态终态"
    两个条件同时满足才收尾。
    """
    idx = from_index
    # 轮询间隔比进程内宽松：Redis 往返比内存读贵，且 SSE 场景下 0.5s 的
    # 推送延迟用户无感（Agent 链路本身要跑数秒）。
    interval = max(0.3, settings.TASK_POLL_INTERVAL * 2)
    while True:
        batch = task_store.read_events(task_id, idx)
        if batch:
            idx += len(batch)
            for evt in batch:
                yield evt
            continue

        state = task_store.read_state(task_id) or initial_state
        status = str(state.get("status", "running"))
        if status in ("done", "failed", "cancelled"):
            # 再拉一次，确保终态事件不丢
            tail = task_store.read_events(task_id, idx)
            for evt in tail:
                yield evt
            yield {"event": "closed", "data": _state_to_snapshot(state)}
            return
        await asyncio.sleep(interval)


def sse_format(evt: dict[str, Any]) -> str:
    """把事件格式化成 SSE 帧。"""
    name = evt.get("event", "message")
    payload = json.dumps(evt.get("data", {}), ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


async def cleanup(max_age_seconds: float = 3600.0) -> int:
    """清理过期任务，防止内存表无限增长。

    只清理已进入终态的任务。running/pending 的任务即使超过 max_age 也不清，
    否则会留下"内存里没了、DB 里还 running"的幽灵状态（清单 N5）。
    """
    now = time.time()
    terminal = ("done", "failed", "cancelled", "interrupted")
    dead = [
        tid
        for tid, t in _TASKS.items()
        if t.status in terminal
        and t.finished_at is not None
        and now - t.finished_at > max_age_seconds
    ]
    for tid in dead:
        _TASKS.pop(tid, None)
    return len(dead)


def stats() -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for t in _TASKS.values():
        by_status[t.status] = by_status.get(t.status, 0) + 1
    return {
        "total": len(_TASKS),
        "by_status": by_status,
        "concurrency_limit": _concurrency_limit or settings.MAX_CONCURRENT_PIPELINES,
        "queued": by_status.get("pending", 0) + by_status.get("queued", 0),
        # 跨进程能力说明：多 worker 下这是"任务能否被任意 worker 查到"的关键
        "store": task_store.store_info(),
    }
