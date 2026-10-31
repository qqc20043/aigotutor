"""core/tasks.py 生命周期全路径：排队事件 / 取消 / 失败 / 跨进程等待与事件 / 清理。"""

from __future__ import annotations

import asyncio

import pytest

from app.core import task_store, tasks


async def _noop_pipeline(task) -> dict:
    return {"ok": True}


async def test_submit_emits_queued_when_semaphore_full(monkeypatch):
    """并发打满时新任务收到 queued 事件（排队而非拒绝）。"""
    monkeypatch.setattr(tasks, "_concurrency_limit", 1)
    # 占满信号量
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    monkeypatch.setattr(tasks, "_get_semaphore", lambda: sem)

    task = tasks.submit(
        "probe",
        _noop_pipeline,
        user_id="u1",
        payload={},
    )  # 收集 created/queued 事件
    await asyncio.sleep(0.05)  # 给 _run 协程机会走到 queued 分支
    got_queued = any(e.get("event") == "queued" for e in list(task.events))
    assert got_queued or task.stage == "queued", f"stage={task.stage}"
    sem.release()
    # 等任务结束避免悬挂
    for _ in range(50):
        if task.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)


async def test_pipeline_failure_marks_failed():
    """pipeline 抛异常 -> 任务 failed 并携带错误事件（主链路兜底语义）。"""

    async def boom(task):
        raise RuntimeError("爆炸")

    task = tasks.submit("probe-fail", boom, user_id="u1", payload={})
    for _ in range(100):
        if task.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert task.status == "failed"
    assert "爆炸" in task.error
    ev_types = [e.get("event") for e in list(task.events)]
    assert "failed" in ev_types


async def test_cancel_running_task():
    """取消运行中的任务：cancelled 状态 + 事件。"""

    async def slow(task):
        await asyncio.sleep(30)

    task = tasks.submit("probe-cancel", slow, user_id="u1", payload={})
    await asyncio.sleep(0.15)
    ok = tasks.cancel(task.id)
    assert ok is True
    for _ in range(100):
        if task.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert task.status == "cancelled"


async def test_task_owner_from_local_and_store():
    uid = "owner-probe"
    task = tasks.submit("probe-owner", _noop_pipeline, user_id=uid, payload={})
    assert tasks.task_owner(task.id) == uid
    for _ in range(100):
        if task.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)


async def test_wait_unknown_task_raises():
    with pytest.raises(KeyError):
        await tasks.wait("no-such-task", timeout=0.1)


async def test_events_unknown_task_yields_error():
    got = [e async for e in tasks.events("no-such-task-xyz")]
    assert got and got[0]["event"] == "error"


async def test_events_remote_path_with_redis():
    """跨进程事件路径：Redis 里有状态 + 事件时，events() 走远程读取。"""
    tid = "remote-probe-1"
    task_store.publish_state(
        type(
            "T",
            (),
            {
                "id": tid,
                "user_id": "u",
                "kind": "k",
                "status": "done",
                "progress": 1.0,
                "stage": "",
                "error": "",
                "result": {"r": 1},
                "cancel_requested": False,
                "created_at": 0,
                "finished_at": 1,
            },
        )()
    )
    task_store.publish_event(tid, {"event": "progress", "data": {"p": 1}})
    got = [e async for e in tasks.events(tid, from_index=0)]
    kinds = [e["event"] for e in got]
    assert "closed" in kinds


async def test_cleanup_removes_finished_tasks():
    async def quick(task):
        return {"r": 1}

    task = tasks.submit("probe-clean", quick, user_id="u1", payload={})
    for _ in range(100):
        if task.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    # 把 finished_at 拨老触发清理
    task.finished_at = task.finished_at - 100000
    removed = await tasks.cleanup(max_age_seconds=1000)
    assert removed >= 1


async def test_get_task_local():
    task = tasks.submit("probe-get", _noop_pipeline, user_id="u1", payload={})
    assert tasks.get_task(task.id) is task
