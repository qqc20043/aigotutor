"""学习计划 API：进度派生（AC 事实源）、详情清单、404/鉴权。"""

from __future__ import annotations

import uuid

import pytest

from app.db import session_scope
from app.models import StudyPlan, StudyPlanItem


async def _make_plan(problem_ids: list[str], title: str = "测试计划") -> str:
    """直接建计划（避免依赖 scripts.seed_study_plans 的标题匹配）。"""
    async with session_scope() as db:
        plan = StudyPlan(title=title, subtitle="测试用", category="精选", theme="green")
        db.add(plan)
        await db.flush()
        for i, pid in enumerate(problem_ids):
            db.add(StudyPlanItem(plan_id=plan.id, problem_id=pid, order_no=i))
        return plan.id


async def _auth(client) -> str:
    name = f"sp{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


@pytest.fixture
async def plan_setup(client):
    """两个题 + 一个计划 + 一个用户 token。"""
    token = await _auth(client)
    h = {"Authorization": f"Bearer {token}"}
    pids = []
    for i in range(2):
        r = await client.post(
            "/api/problems",
            json={"title": f"计划题{i}-{uuid.uuid4().hex[:4]}", "statement": "x", "tags": ["模拟"]},
            headers=h,
        )
        assert r.status_code == 201, r.text
        pids.append(r.json()["id"])
    plan_id = await _make_plan(pids)
    return client, token, h, plan_id, pids


async def test_list_requires_auth(client):
    r = await client.get("/api/study-plans")
    assert r.status_code == 401


async def test_progress_derived_from_ac(plan_setup):
    """零报名：未做过 → 0%；AC 一题 → 50% 且 in_progress=True；清空 AC 不影响。"""
    client, _token, h, plan_id, pids = plan_setup

    r = await client.get("/api/study-plans", headers=h)
    plan = next(i for i in r.json()["items"] if i["id"] == plan_id)
    assert plan["total"] == 2 and plan["done"] == 0 and plan["percent"] == 0.0
    assert plan["in_progress"] is False and plan["completed"] is False
    assert r.json()["in_progress"] == []

    # AC 第一题（调用方判定模式，不跑沙箱）
    r = await client.post(
        "/api/submissions",
        json={
            "problem_id": pids[0],
            "language": "python",
            "code": "print(1)",
            "verdict": "AC",
            "judge_mode": "caller",
            "run_agents": False,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text

    r = await client.get("/api/study-plans", headers=h)
    plan = next(i for i in r.json()["items"] if i["id"] == plan_id)
    assert plan["done"] == 1 and plan["percent"] == 50.0
    assert plan["in_progress"] is True
    assert [i["id"] for i in r.json()["in_progress"]] == [plan_id]

    # 详情：第一题 solved=True，第二题 False
    r = await client.get(f"/api/study-plans/{plan_id}", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [(i["order_no"], i["solved"]) for i in items] == [(0, True), (1, False)]
    assert items[0]["title"].startswith("计划题0")


async def test_completed_plan(plan_setup):
    """两题都 AC → completed=True、percent=100%、不再出现在进行中。"""
    client, _token, h, plan_id, pids = plan_setup
    for pid in pids:
        r = await client.post(
            "/api/submissions",
            json={
                "problem_id": pid,
                "language": "python",
                "code": "print(1)",
                "verdict": "AC",
                "judge_mode": "caller",
                "run_agents": False,
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
    r = await client.get("/api/study-plans", headers=h)
    plan = next(i for i in r.json()["items"] if i["id"] == plan_id)
    assert plan["completed"] is True and plan["percent"] == 100.0
    assert plan["in_progress"] is False


async def test_detail_404_for_unknown_plan(plan_setup):
    client, _token, h, _, _ = plan_setup
    r = await client.get("/api/study-plans/no-such-plan", headers=h)
    assert r.status_code == 404


async def test_seed_plans_script_idempotent():
    """种子脚本可用且幂等（测试库没有题包时 missing 非空也不报错）。"""
    from scripts.seed_study_plans import seed_plans

    first = await seed_plans()
    assert first["created"] >= 1, f"应至少建出 1 个计划: {first}"
    second = await seed_plans()
    assert second["created"] == 0 and second["skipped"] >= 1

    async with session_scope() as db:
        plans = (await db.execute(__import__("sqlalchemy").select(StudyPlan))).scalars().all()
    assert {p.title for p in plans} >= {"编程基础 0 → 1", "面试冲刺 15 题"}
