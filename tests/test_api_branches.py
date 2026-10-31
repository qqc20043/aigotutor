"""API 层错误分支补齐：各端点的 404/409/422/403 路径（覆盖率收尾用）。"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
async def admin_and_user(client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User as U

    async def _reg(name):
        r = await client.post(
            "/api/auth/register",
            json={"username": name, "password": "test123456", "nickname": name},
        )
        return r.json()["access_token"]

    ta = await _reg(f"ax{uuid.uuid4().hex[:6]}")
    tb = await _reg(f"bx{uuid.uuid4().hex[:6]}")
    async with session_scope() as db:
        (await db.execute(select(U).where(U.username == f"ax{''}"))).scalar_one_or_none()
    # 用 me 接口拿 id，再提权
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ta}"})
    admin_id = me.json()["id"]
    async with session_scope() as db:
        u = await db.get(U, admin_id)
        u.role = "admin"
    return client, ta, tb


async def _admin_problem(client, token, *, cases=True):
    r = await client.post(
        "/api/admin/problems",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": f"分支题{uuid.uuid4().hex[:6]}",
            "statement": "s",
            "testcases": [{"input": "1", "output": "1"}] if cases else [],
        },
    )
    return r.json()


# ================================================ admin 错误分支
async def test_admin_branch_errors(admin_and_user):
    client, ta, _ = admin_and_user
    H = {"Authorization": f"Bearer {ta}"}
    # 建题缺标题/题面
    r = await client.post("/api/admin/problems", headers=H, json={"title": "", "statement": ""})
    assert r.status_code == 422
    # 改/删不存在的题
    assert (
        await client.patch("/api/admin/problems/nope", headers=H, json={"title": "x"})
    ).status_code == 404
    assert (
        await client.delete("/api/admin/problems/nope?confirm=true", headers=H)
    ).status_code == 404
    # 删题不确认
    prob = (
        await client.post(
            "/api/admin/problems", headers=H, json={"title": "待删题", "statement": "s"}
        )
    ).json()
    assert (await client.delete(f"/api/admin/problems/{prob['id']}", headers=H)).status_code == 400
    # 重判/详情/重置密码不存在
    assert (await client.post("/api/admin/submissions/nope/rejudge", headers=H)).status_code == 404
    assert (await client.get("/api/admin/users/nope", headers=H)).status_code == 404
    assert (await client.post("/api/admin/users/nope/reset-password", headers=H)).status_code == 404
    # 公告到不存在用户
    r = await client.post(
        "/api/admin/announce", headers=H, json={"title": "x", "username": "ghost_user"}
    )
    assert r.status_code == 404
    # 批量操作只含自己 -> 400
    me = (await client.get("/api/auth/me", headers=H)).json()["id"]
    r = await client.post(
        "/api/admin/users/batch", headers=H, json={"user_ids": [me], "is_active": False}
    )
    assert r.status_code == 400
    # agents 统计与 overview 带 days 参数
    assert (await client.get("/api/admin/agents?days=7", headers=H)).status_code == 200
    assert (await client.get("/api/admin/overview?days=1", headers=H)).status_code == 200
    # 审计按动作筛选
    assert (
        await client.get("/api/admin/audit?action=problem.create", headers=H)
    ).status_code == 200


# ================================================ agent/chat/data/practice 错误分支
async def test_agent_and_misc_branch_errors(client):
    name = f"cx{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # 未知 Agent
    r = await client.post("/api/agent/run", headers=H, json={"agent": "nope", "payload": {}})
    assert r.status_code == 404
    # 缓存清理
    r = await client.post("/api/agent/cache/clear", headers=H)
    assert r.status_code == 200
    # stats 带 days
    r = await client.get("/api/agent/stats?days=7", headers=H)
    assert r.status_code == 200
    # chat 会话绑不存在题
    r = await client.post("/api/chat/sessions", headers=H, json={"problem_id": "nope"})
    assert r.status_code == 404
    # practice 干预方案 404 / 配方生成 404
    r = await client.post("/api/practice/leech/nope/plan", headers=H)
    assert r.status_code == 404
    r = await client.post("/api/practice/recipes/nope/generate", headers=H)
    assert r.status_code == 404
    r = await client.delete("/api/practice/recipes/nope", headers=H)
    assert r.status_code == 404
    # data 导出题目
    r = await client.get("/api/data/export/problems", headers=H)
    assert r.status_code == 200


async def test_interview_branch_errors(client):
    name = f"ix{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # 开场绑不存在题
    assert (
        await client.post("/api/interview/start", headers=H, json={"problem_id": "nope"})
    ).status_code == 404
    # 场次不存在
    assert (await client.get("/api/interview/nope", headers=H)).status_code == 404
    assert (await client.get("/api/interview/nope/report", headers=H)).status_code == 404
    # 建真场次后：消息/提交/结束时序分支
    pid = (
        await client.post(
            "/api/problems",
            headers=H,
            json={"title": f"分支面试题{uuid.uuid4().hex[:4]}", "statement": "s", "tags": ["模拟"]},
        )
    ).json()["id"]
    sid = (
        await client.post(
            "/api/interview/start", headers=H, json={"problem_id": pid, "time_limit_minutes": 5}
        )
    ).json()["id"]
    r = await client.post(
        f"/api/interview/{sid}/message", headers=H, json={"content": "开始", "ask_hint": False}
    )
    assert r.status_code == 200
    # 结束后 message/submit 409
    assert (await client.post(f"/api/interview/{sid}/finish", headers=H)).status_code == 200
    assert (
        await client.post(f"/api/interview/{sid}/message", headers=H, json={"content": "x"})
    ).status_code == 409
    assert (
        await client.post(
            f"/api/interview/{sid}/submit",
            headers=H,
            json={"language": "python", "code": "print(1)"},
        )
    ).status_code == 409
    # 详情与报告
    assert (await client.get(f"/api/interview/{sid}", headers=H)).status_code == 200
    rep = await client.get(f"/api/interview/{sid}/report", headers=H)
    assert rep.status_code == 200 and rep.json()["report"]


# ================================================ mastery/review/problems 错误分支
async def test_mastery_review_problems_branch_errors(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"mx{uuid.uuid4().hex[:6]}", "password": "test123456"},
    )
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # recalc 带 window_days
    r = await client.post("/api/mastery/recalc?window_days=30", headers=H)
    assert r.status_code == 200
    # wrong-book 标记不存在的题（应 404 或 200 幂等，不 5xx）
    r = await client.post("/api/wrong-book/nope/mastered", headers=H)
    assert r.status_code in (200, 404)
    # 复习标记不存在的题（修复前 INSERT 悬空外键 -> 500）
    r = await client.post("/api/review/mark", headers=H, json={"problem_id": "nope", "rating": 3})
    assert r.status_code == 404, "不存在的题必须 404"
    # 复习队列 due_only 与 forecast
    r = await client.get("/api/review/queue?due_only=true", headers=H)
    assert r.status_code == 200
    r = await client.get("/api/review/due", headers=H)
    assert r.status_code == 200
    # 题目 random/search/tags/route/hint/similar
    pid = (
        await client.post(
            "/api/problems",
            headers=H,
            json={
                "title": f"分支题{uuid.uuid4().hex[:4]}",
                "statement": "求两个数的和",
                "tags": ["模拟"],
            },
        )
    ).json()["id"]
    assert (await client.get("/api/problems/random", headers=H)).status_code == 200
    assert (await client.get("/api/problems/search?q=分支", headers=H)).status_code == 200
    assert (await client.get("/api/problems/tags", headers=H)).status_code == 200
    r = await client.post(f"/api/problems/{pid}/route", headers=H)
    assert r.status_code == 200
    assert (await client.get("/api/problems/nope", headers=H)).status_code == 404
    # 提交详情不存在
    assert (await client.get("/api/submissions/nope", headers=H)).status_code == 404
    # 题解生成（不存在提交）
    assert (await client.post("/api/submissions/nope/solution", headers=H)).status_code == 404


# ================================================ recommend 路线图/解释
async def test_recommend_endpoints(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"rx{uuid.uuid4().hex[:6]}", "password": "test123456"},
    )
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = (
        await client.post(
            "/api/problems",
            headers=H,
            json={
                "title": f"推荐分支题{uuid.uuid4().hex[:4]}",
                "statement": "两数之和",
                "tags": ["模拟"],
            },
        )
    ).json()["id"]
    r = await client.get("/api/recommend/next", headers=H)
    assert r.status_code == 200
    r = await client.get("/api/recommend/roadmap?weeks=4", headers=H)
    assert r.status_code == 200
    r = await client.get(f"/api/recommend/explain?problem_id={pid}", headers=H)
    assert r.status_code in (200, 404)


# ================================================ agents run_rule 分支
async def test_agents_run_rule_branches():
    """各 Agent 的 run_rule 规则兜底直接调用（LLM 失败时的同一路径）。"""
    from app.agents import get_agent
    from app.core.agent import AgentContext

    ctx = AgentContext(
        data={
            "language": "python",
            "tags": ["模拟"],
            "title": "t",
            "statement": "求和",
            "mastery": {"模拟": 30.0},
            "candidates": [],
            "tag_stats": {"模拟": {"attempts": 3, "ac_count": 1}},
            "code": "def f():\n    pass",
            "verdict": "WA",
        }
    )
    for name in ("difficulty", "judge", "quality", "recommend", "roadmap", "review", "solution"):
        agent = get_agent(name)
        out = agent.run_rule(ctx)
        assert out is not None, f"{name} 的规则兜底不应为空"


async def test_agents_validate_registry_branch():
    from app.agents import validate_registry

    assert validate_registry() == []


# ================================================ users 分支
async def test_users_branch_errors(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"ux{uuid.uuid4().hex[:6]}", "password": "test123456"},
    )
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # 改密码：旧密码错
    r = await client.post(
        "/api/auth/change-password",
        headers=H,
        json={"old_password": "wrong-old", "new_password": "newpass12345"},
    )
    assert r.status_code in (400, 401, 403)
    # 昵称/等级 patch
    r = await client.patch("/api/auth/me", headers=H, json={"nickname": "新昵称", "daily_goal": 5})
    assert r.status_code == 200
    # model config 流程
    r = await client.put(
        "/api/users/me/model-config",
        headers=H,
        json={"provider": "mock", "model": "mock-1", "purpose": "default"},
    )
    assert r.status_code == 200
    r = await client.get("/api/users/me/model-config", headers=H)
    assert r.status_code == 200
    # refresh 无效 token
    r = await client.post("/api/auth/refresh", json={"refresh_token": "bad"})
    assert r.status_code in (401, 422)
