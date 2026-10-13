"""任务状态跨进程共享测试（遗留 1 的修复验证）。

问题背景：任务表 `_TASKS` 是进程内字典。多 worker 部署下，提交任务的请求
可能落在 worker A，而轮询 `/api/tasks/{id}` 被负载均衡到 worker B —— B 的
字典里没有这个任务，返回 404；cancel 同理打不到任务真正所在的进程。

修复思路：**执行留在 owner 进程**（asyncio 句柄不可跨进程传递），但状态、
事件、取消信号镜像到 Redis。本文件守护这套镜像机制。

测试手法：直接操作 `task_store`（模拟"另一个 worker 的视角"），
因为单进程测试里没法真的起两个进程。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.core import task_store, tasks
from app.core.tasks import _advance


@pytest.fixture
def with_redis(monkeypatch):
    """启用 Redis（本机 6379）。不可用时跳过整个用例。

    刻意用真实 Redis 而不是 mock：镜像机制的价值就在"真的跨进程可见"，
    mock 掉存储层等于没测。
    """
    import redis

    try:
        probe = redis.Redis(host="127.0.0.1", port=6379, socket_connect_timeout=1, protocol=2)
        probe.ping()
    except Exception:
        pytest.skip("本机无可用 Redis，跳过跨进程用例")

    from app.core.redis_client import reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    reset_redis_cache()
    yield
    reset_redis_cache()


@pytest.fixture
def without_redis(monkeypatch):
    """禁用 Redis，验证降级路径行为与改造前一致。"""
    from app.core.redis_client import reset_redis_cache

    monkeypatch.setattr(settings, "REDIS_URL", "")
    reset_redis_cache()
    yield
    reset_redis_cache()


async def _noop_pipeline(task) -> dict:
    return {"ok": True}


# ============================================== 跨进程可见性
@pytest.mark.asyncio
async def test_task_state_visible_via_redis(with_redis):
    """任务状态必须镜像到 Redis，供别的 worker 读取。"""
    t = tasks.submit("probe", _noop_pipeline, user_id="u-mirror")
    await tasks.wait(t.id, timeout=10)

    state = task_store.read_state(t.id)
    assert state is not None, "状态未镜像到 Redis"
    assert state["task_id"] == t.id
    assert state["status"] == "done"
    assert state["user_id"] == "u-mirror"
    assert state["owner"] == task_store.INSTANCE_ID


@pytest.mark.asyncio
async def test_task_events_mirrored_in_order(with_redis):
    """事件必须按顺序镜像（SSE 增量拉取依赖顺序）。"""
    t = tasks.submit("probe", _noop_pipeline, user_id="u-events")
    await tasks.wait(t.id, timeout=10)

    events = task_store.read_events(t.id, 0)
    names = [e["event"] for e in events]
    assert names[0] == "created"
    assert "started" in names
    assert names[-1] == "done"
    # 与进程内事件序列一致
    local = [e["event"] for e in t.events]
    assert names == local


@pytest.mark.asyncio
async def test_progress_updates_mirrored(with_redis):
    """进度推进必须实时镜像（轮询任务详情要看到中间进度）。"""
    gate = asyncio.Event()

    async def pipeline(task) -> dict:
        _advance(task, 0.4, "stage-a")
        await gate.wait()
        return {}

    t = tasks.submit("probe", pipeline, user_id="u-progress")
    await asyncio.sleep(0.3)

    mid = task_store.read_state(t.id)
    assert mid is not None
    assert mid["status"] == "running"
    assert mid["progress"] == pytest.approx(0.4, abs=0.01)
    assert mid["stage"] == "stage-a"

    gate.set()
    await tasks.wait(t.id, timeout=10)


@pytest.mark.asyncio
async def test_user_index_lists_task(with_redis):
    """用户任务索引必须包含该任务（列表接口依赖它）。"""
    t = tasks.submit("probe", _noop_pipeline, user_id="u-index")
    await tasks.wait(t.id, timeout=10)

    ids = task_store.list_user_task_ids("u-index")
    assert t.id in ids


@pytest.mark.asyncio
async def test_list_tasks_merges_remote(with_redis):
    """list_tasks 必须合并"本地没有但 Redis 里有"的任务。

    模拟方式：写一条状态到 Redis（不经过本地 submit），再验证它出现在列表里。
    """
    import json

    from app.core.redis_client import get_redis

    client = get_redis()
    assert client is not None
    fake_id = "remote-task-for-list"
    prefix = f"{settings.REDIS_PREFIX}:task:{fake_id}"
    state = {
        "task_id": fake_id,
        "user_id": "u-remote",
        "kind": "remote",
        "status": "done",
        "progress": 1.0,
        "stage": "finished",
        "error": "",
        "result": {"from": "other-worker"},
        "cancel_requested": False,
        "created_at": task_store.now(),
        "finished_at": task_store.now(),
        "owner": "other-worker:1234",
    }
    client.setex(f"{prefix}:state", 60, json.dumps(state))
    client.zadd(f"{settings.REDIS_PREFIX}:tasks:user:u-remote", {fake_id: state["created_at"]})

    listed = tasks.list_tasks("u-remote")
    assert any(item["task_id"] == fake_id for item in listed), "远程任务未出现在列表"
    found = next(item for item in listed if item["task_id"] == fake_id)
    assert found["result"] == {"from": "other-worker"}

    client.delete(f"{prefix}:state")
    client.delete(f"{settings.REDIS_PREFIX}:tasks:user:u-remote")


@pytest.mark.asyncio
async def test_task_detail_reads_remote(with_redis):
    """task_detail() 在本地没有该任务时必须回落到 Redis。"""
    import json

    from app.core.redis_client import get_redis

    client = get_redis()
    assert client is not None
    fake_id = "remote-task-for-detail"
    prefix = f"{settings.REDIS_PREFIX}:task:{fake_id}"
    client.setex(
        f"{prefix}:state",
        60,
        json.dumps(
            {
                "task_id": fake_id,
                "status": "running",
                "progress": 0.7,
                "stage": "mid",
                "error": "",
                "result": {},
                "created_at": task_store.now(),
                "finished_at": None,
            }
        ),
    )

    snap = tasks.task_detail(fake_id)
    assert snap is not None
    assert snap["status"] == "running"
    assert snap["progress"] == pytest.approx(0.7, abs=0.01)

    client.delete(f"{prefix}:state")


# ============================================== 跨进程取消
@pytest.mark.asyncio
async def test_cross_process_cancel_interrupts_owner(with_redis):
    """别的 worker 发起取消 -> owner 侧必须感知并中断执行。

    这是遗留 1 里最难的一环：asyncio 句柄不能跨进程传，所以只能
    "信号 + owner 轮询感知 + 取消本地句柄"。
    """
    ticks: list[int] = []

    async def long_pipeline(task) -> dict:
        for i in range(50):
            await asyncio.sleep(0.05)
            ticks.append(i)
        return {"finished": True}

    t = tasks.submit("long", long_pipeline, user_id="u-cancel")
    await asyncio.sleep(0.2)

    # 模拟别的 worker：只写 Redis 信号，不碰本地对象
    assert task_store.request_cancel(t.id) is True
    assert task_store.is_cancel_requested(t.id) is True

    # 等 owner 的轮询任务感知（间隔 1s，留足余量）
    for _ in range(40):
        await asyncio.sleep(0.15)
        if t.status == "cancelled":
            break

    assert t.status == "cancelled", f"跨进程取消失效，状态仍是 {t.status}"
    assert len(ticks) < 50, "任务跑完了，说明取消没有真正中断执行"
    # 终态必须镜像回去（否则别的 worker 永远看到 running）
    state = task_store.read_state(t.id)
    assert state is not None
    assert state["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_remote_task_returns_true(with_redis):
    """本地没有的任务，cancel 应写信号并返回 True（表示请求已受理）。"""
    import json

    from app.core.redis_client import get_redis

    client = get_redis()
    assert client is not None
    fake_id = "remote-task-for-cancel"
    prefix = f"{settings.REDIS_PREFIX}:task:{fake_id}"
    client.setex(
        f"{prefix}:state",
        60,
        json.dumps({"task_id": fake_id, "status": "running", "created_at": task_store.now()}),
    )

    assert tasks.cancel(fake_id) is True
    assert task_store.is_cancel_requested(fake_id) is True

    # 已完成的任务不应接受取消
    client.setex(
        f"{prefix}:state",
        60,
        json.dumps({"task_id": fake_id, "status": "done", "created_at": task_store.now()}),
    )
    assert tasks.cancel(fake_id) is False

    client.delete(f"{prefix}:state", f"{prefix}:cancel")


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_false(with_redis):
    """不存在的任务：cancel 返回 False（不抛异常）。"""
    assert tasks.cancel("definitely-not-a-task") is False


# ============================================== 降级路径
@pytest.mark.asyncio
async def test_without_redis_behaves_as_before(without_redis):
    """未配置 Redis 时，行为必须与改造前完全一致（本地路径照常工作）。"""
    t = tasks.submit("probe", _noop_pipeline, user_id="u-local")
    await tasks.wait(t.id, timeout=10)

    assert t.status == "done"
    # 本地能查到
    assert tasks.task_detail(t.id) is not None
    assert any(item["task_id"] == t.id for item in tasks.list_tasks("u-local"))
    # 镜像函数是 no-op，不应报错
    assert task_store.read_state(t.id) is None
    assert task_store.read_events(t.id, 0) == []
    assert task_store.request_cancel(t.id) is False


@pytest.mark.asyncio
async def test_store_info_reports_redis_backend(with_redis):
    """Redis 可用时运行态信息应上报 redis。"""
    info = task_store.store_info()
    assert info["backend"] == "redis"
    assert info["instance_id"]
    assert "跨进程" in info["note"]


@pytest.mark.asyncio
async def test_store_info_reports_memory_backend(without_redis):
    """Redis 不可用时运行态信息应上报 memory，并说明多 worker 的后果。"""
    info = task_store.store_info()
    assert info["backend"] == "memory"
    assert "进程" in info["note"]


@pytest.mark.asyncio
async def test_events_stream_local_path_unchanged(without_redis):
    """SSE 事件流的本地路径语义不变：created→started→done→closed。"""
    t = tasks.submit("probe", _noop_pipeline, user_id="u-sse")
    collected: list[str] = []
    async for evt in tasks.events(t.id, 0):
        collected.append(evt["event"])
        if evt["event"] == "closed":
            break
    assert collected[0] == "created"
    assert "done" in collected
    assert collected[-1] == "closed"
