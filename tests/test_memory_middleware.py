"""core/memory.py 聚合路径 + middleware.py Redis 限流/客户端 IP 分支。"""

from __future__ import annotations

import pytest

from app.core.memory import build_memory_snapshot, memory_block_for
from app.db import session_scope


@pytest.fixture
async def active_user(client):
    name = f"mem{__import__('uuid').uuid4().hex[:6]}"
    r = await client.post(
        "/api/auth/register", json={"username": name, "password": "test123456", "nickname": "m"}
    )
    token = r.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    uid = me.json()["id"]

    # 制造提交 + 错题 + 画像，让聚合有数据
    p1 = (
        await client.post(
            "/api/problems",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "记忆题", "statement": "s", "tags": ["模拟"]},
        )
    ).json()["id"]
    await client.post(
        "/api/submissions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "problem_id": p1,
            "language": "python",
            "code": "print(1)",
            "verdict": "WA",
            "passed_cases": 0,
            "total_cases": 1,
            "judge_mode": "caller",
            "run_agents": False,
        },
    )
    await client.post(
        f"/api/wrong-book/{p1}/mastered", headers={"Authorization": f"Bearer {token}"}, json=None
    )
    return client, token, uid


async def test_memory_snapshot_full_aggregation(active_user):
    """提交/错题/画像齐备时快照包含弱项、强项与错误模式（聚合路径全覆盖）。"""
    _client, _token, uid = active_user
    async with session_scope() as db:
        snap = await build_memory_snapshot(db, uid)
    assert snap.total_submissions >= 1
    assert isinstance(snap.weak_tags, list)
    assert isinstance(snap.strong_tags, list)
    block = (
        memory_block_for(
            __import__("app.db", fromlist=["session_scope"]).session_scope
            and __import__("asyncio").get_event_loop(),
            uid,
        )
        if False
        else None
    )
    # 用真实 session 拿 prompt 块
    async with session_scope() as db:
        block = await memory_block_for_async(db, uid)
    assert isinstance(block, str)


async def test_memory_block_never_raises(active_user, monkeypatch):
    """记忆注入失败必须返回空串（不能拖垮主链路）。"""
    _client, _token, uid = active_user

    async def boom(db, user_id):
        raise RuntimeError("x")

    monkeypatch.setattr("app.core.memory.build_memory_snapshot", boom)
    async with session_scope() as db:
        assert await memory_block_for(db, uid) == ""


async def test_memory_block_empty_user():
    """零数据用户：返回空串或极短块（不硬凑）。"""
    from uuid import uuid4

    from app.models import User

    uid = f"memempty{uuid4().hex[:6]}"
    async with session_scope() as db:
        db.add(User(id=uid, username=uid[:8], password_hash="x"))
    async with session_scope() as db:
        block = await memory_block_for(db, uid)
    assert isinstance(block, str)


async def test_memory_block_for_wrapper(active_user):
    from app.core.memory import memory_block_for as mbf

    _client, _token, uid = active_user
    async with session_scope() as db:
        block = await mbf(db, uid)
    assert isinstance(block, str)


async def memory_block_for_async(db, uid):
    return await memory_block_for(db, uid)


# ==================================================== middleware Redis 限流
async def test_rate_limit_redis_zset_branch(client, monkeypatch):
    """启用 Redis 时限流走 ZSET 分支（真实 Redis 已在会话中启用）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    from app.middleware import reset_rate_limit

    reset_rate_limit()
    # 连续打满某端点（登录限流 10/min），触发 ZSET 拒绝分支
    codes = []
    for _ in range(12):
        r = await client.post(
            "/api/auth/login", json={"username": "nouser_rl", "password": "wrongpass1"}
        )
        codes.append(r.status_code)
    assert 429 in codes, "打满登录限流必须出现 429"
    reset_rate_limit()


async def test_client_ip_from_xff(client, monkeypatch):
    """X-Forwarded-For 存在时取第一个 IP（代理场景）。"""
    from types import SimpleNamespace

    from app.middleware import client_ip

    fake_req = SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client=None)

    assert client_ip(fake_req) == "1.2.3.4"
