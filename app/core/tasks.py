"""后台任务编排：asyncio 任务表 + SSE 事件流 + 可中断。

为什么不用 Celery：
  MVP 阶段引入 broker 会显著抬高部署成本，而本项目的任务是"秒级到十秒级"的
  Agent 链路，用 asyncio 足够；压测阶段换 Redis/Celery 时接口不变。

写并发收敛不在这里，而在 app/db.py 的事务提交层（全局写锁）。
上一版曾在此实现 serialize_write() 但从未接线，已删除 —— 保留一份不生效的
实现比没有更危险，它会让文档与注释撒谎。

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
    return _TASKS.get(task_id)


def list_tasks(user_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    items = [t for t in _TASKS.values() if not user_id or t.user_id == user_id]
    items.sort(key=lambda t: t.created_at, reverse=True)
    return [t.snapshot() for t in items[:limit]]


def _emit(task: TaskState, event: str, data: dict[str, Any] | None = None) -> None:
    task.events.append({"event": event, "data": data or {}, "ts": round(time.time(), 4)})


def _advance(task: TaskState, progress: float, stage: str) -> None:
    task.progress = max(task.progress, min(1.0, progress))
    task.stage = stage
    _emit(task, "progress", {"progress": task.progress, "stage": stage})


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
    task.handle = asyncio.create_task(_run(task, pipeline))
    return task


async def _run(task: TaskState, pipeline: Pipeline) -> None:
    sem = _get_semaphore()
    if sem.locked():
        task.stage = "queued"
        _emit(task, "queued", {"reason": f"并发已达上限 {_concurrency_limit}，排队中"})
    async with sem:
        await _run_inner(task, pipeline)


async def _run_inner(task: TaskState, pipeline: Pipeline) -> None:
    task.status = "running"
    _emit(task, "started", {})
    try:
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


def cancel(task_id: str) -> bool:
    task = _TASKS.get(task_id)
    if task is None or task.status in ("done", "failed", "cancelled"):
        return False
    task.cancel_requested = True
    if task.handle is not None:
        task.handle.cancel()
    return True


async def wait(task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """同步等待任务完成（测试与内部调用用）。"""
    task = _TASKS.get(task_id)
    if task is None:
        raise KeyError(f"未知任务: {task_id}")
    deadline = time.time() + timeout
    while task.status in ("pending", "running") and time.time() < deadline:
        await asyncio.sleep(settings.TASK_POLL_INTERVAL)
    return task.snapshot()


async def events(task_id: str, from_index: int = 0) -> AsyncIterator[dict[str, Any]]:
    """SSE 事件流：先补发历史事件，再增量推送，任务终止后自动收尾。"""
    task = _TASKS.get(task_id)
    if task is None:
        yield {"event": "error", "data": {"error": f"未知任务: {task_id}"}}
        return

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
    }
