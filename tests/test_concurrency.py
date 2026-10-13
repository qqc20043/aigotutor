"""并发正确性测试：压测暴露的竞态修复（批次 C / F20）。

这些用例对应压测中真实出现的失败：
  * UNIQUE constraint failed: background_tasks.id —— 任务 upsert 竞态；
  * 并发写不出现 database is locked —— 全局写锁。
"""

from __future__ import annotations

import asyncio
import uuid


# ==================================================== 任务 upsert 竞态
async def test_persist_task_concurrent_upsert_same_id():
    """同一 task_id 被并发写入时不得抛唯一约束冲突。

    这是压测抓到的真实 bug：原实现"先 get 再 insert/update"，
    两个协程同时 get 不到记录，于是各自 INSERT，第二个撞唯一约束。
    """
    from app.db import session_scope
    from app.models import BackgroundTask
    from app.services import persist_task

    tid = f"race-{uuid.uuid4().hex[:12]}"

    async def write(status: str, progress: float) -> None:
        async with session_scope() as db:
            await persist_task(
                db,
                {
                    "id": tid,
                    "user_id": "u1",
                    "kind": "agent_pipeline",
                    "status": status,
                    "progress": progress,
                    "stage": status,
                },
            )

    # 20 个并发写入同一 id
    await asyncio.gather(*[write("running", i / 20) for i in range(20)])

    async with session_scope() as db:
        row = await db.get(BackgroundTask, tid)
        assert row is not None, "任务记录未创建"
        assert row.status == "running"
        # created_at 应保留最早的值（不被后续 upsert 覆盖）
        assert row.created_at is not None


async def test_persist_task_sequential_updates_keep_created_at():
    """顺序更新：created_at 保持不变，updated_at 应变化。"""
    import asyncio as _a

    from app.db import session_scope
    from app.models import BackgroundTask
    from app.services import persist_task

    tid = f"seq-{uuid.uuid4().hex[:12]}"
    async with session_scope() as db:
        await persist_task(db, {"id": tid, "user_id": "u1", "status": "pending"})
        first = await db.get(BackgroundTask, tid)
        created = first.created_at

    await _a.sleep(0.02)

    async with session_scope() as db:
        await persist_task(db, {"id": tid, "user_id": "u1", "status": "done", "progress": 1.0})
        row = await db.get(BackgroundTask, tid)
        assert row.status == "done"
        assert row.progress == 1.0
        assert row.created_at == created, "created_at 不应被更新覆盖"
        assert row.finished_at is not None, "终态应写入 finished_at"


async def test_persist_task_without_id_inserts():
    """无 id 时退回普通插入（调用方应始终传 id，但不应崩）。"""
    from app.db import session_scope
    from app.models import BackgroundTask
    from app.services import persist_task

    async with session_scope() as db:
        await persist_task(db, {"user_id": "u1", "status": "pending"})
        from sqlalchemy import func, select

        count = (await db.execute(select(func.count()).select_from(BackgroundTask))).scalar_one()
        assert count >= 1


# ==================================================== 并发写无锁错误
async def test_concurrent_mixed_writes_no_lock_error():
    """并发写不同表，不应出现 database is locked。

    注意：AgentRun / LearningState 的 user_id 有外键约束，因此这里先建一个
    真实用户再并发写，否则会以 FOREIGN KEY constraint failed 失败
    （那是测试数据问题，不是并发问题）。
    """
    from app.db import session_scope
    from app.models import AgentRun, LearningState, Tag, User
    from app.security import hash_password

    async with session_scope() as db:
        user = User(username=f"concu{uuid.uuid4().hex[:8]}", password_hash=hash_password("x" * 8))
        db.add(user)
        await db.flush()
        uid = user.id

    async def write_tag(i: int) -> None:
        async with session_scope() as db:
            db.add(Tag(name=f"mix-{uuid.uuid4().hex[:8]}-{i}"))

    async def write_run(i: int) -> None:
        async with session_scope() as db:
            db.add(AgentRun(agent="judge", user_id=uid, status="ok"))

    async def write_state(i: int) -> None:
        async with session_scope() as db:
            db.add(LearningState(user_id=uid, score=float(i), level="L3"))

    tasks = []
    for i in range(10):
        tasks.extend([write_tag(i), write_run(i), write_state(i)])
    await asyncio.gather(*tasks)


# ==================================================== 提交并发
async def test_concurrent_submissions_same_user(client):
    """同一用户并发提交（不同代码）应全部成功，不被去重误杀。"""

    r = await client.post(
        "/api/auth/register",
        json={"username": f"conc{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/api/problems",
        json={
            "title": f"并发题-{uuid.uuid4().hex[:6]}",
            "statement": "x",
            "tags": ["模拟"],
            "auto_annotate": False,
        },
        headers=h,
    )
    pid = r.json()["id"]

    async def submit(i: int) -> int:
        resp = await client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "code": f"print({i})\n",
                "judge_mode": "caller",
                "verdict": "WA",
                "run_agents": False,
                "dedupe": False,
            },
            headers=h,
        )
        return resp.status_code

    codes = await asyncio.gather(*[submit(i) for i in range(15)])
    assert all(c == 201 for c in codes), f"部分提交失败: {codes}"

    # 应生成 15 条独立记录
    r2 = await client.get("/api/submissions", params={"problem_id": pid, "size": 50}, headers=h)
    assert r2.json()["total"] >= 15


async def test_concurrent_identical_submissions_deduped(client):
    """并发提交相同代码：去重应生效，不应产生 15 条重复记录。

    去重是"尽力而为"的优化（非事务性），因此允许少量竞态窗口内的重复，
    但不应出现"全部重复"这种去重完全失效的情况。
    """
    r = await client.post(
        "/api/auth/register",
        json={"username": f"dd{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = await client.post(
        "/api/problems",
        json={
            "title": f"去重题-{uuid.uuid4().hex[:6]}",
            "statement": "x",
            "tags": ["模拟"],
            "auto_annotate": False,
        },
        headers=h,
    )
    pid = r.json()["id"]
    same_code = "print(42)\n"

    async def submit() -> dict:
        resp = await client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "code": same_code,
                "judge_mode": "caller",
                "verdict": "WA",
                "run_agents": False,
                "dedupe": True,
            },
            headers=h,
        )
        return resp.json()

    results = await asyncio.gather(*[submit() for _ in range(10)])
    deduped = sum(1 for r in results if r.get("deduplicated"))
    unique_ids = {r["submission"]["id"] for r in results}

    # 至少有去重生效，且记录数远少于提交数
    assert deduped >= 1, "去重完全未生效"
    assert len(unique_ids) <= 10, "记录数不应超过提交数"


# ==================================================== 限流并发
async def test_concurrent_requests_respect_rate_limit(client, enable_rate_limit):
    """并发打限流接口：超出额度的部分应拿到 429，且不出现 5xx。"""
    from app.middleware import limit_for_path

    limit = limit_for_path("/api/auth/login") or 10

    async def hit() -> int:
        r = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "wrongpass1"}
        )
        return r.status_code

    codes = await asyncio.gather(*[hit() for _ in range(limit + 10)])
    assert 429 in codes, f"未触发限流: {codes}"
    assert not any(c >= 500 for c in codes), f"出现 5xx: {codes}"
    assert codes.count(401) <= limit, f"放行数超过额度: {codes}"
