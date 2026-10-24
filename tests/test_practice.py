"""智能练习集的回归测试：顽固错题识别、混排交错语义、AI 干预降级链。"""

from __future__ import annotations

import itertools
import uuid

import pytest

from app.core.practice import (
    build_interleaved,
    find_leeches,
    rule_intervention,
)
from app.db import session_scope
from app.services import create_problem, upsert_review_item


@pytest.fixture
async def pr_user(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"pr{uuid.uuid4().hex[:8]}", "password": "test123456", "nickname": "pr"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    return client, token, me.json()["id"]


async def _make_problem(title: str, tags: list[str], score: float = 40.0) -> str:
    async with session_scope() as db:
        p = await create_problem(db, {"title": title, "statement": f"{title} 题面"}, tag_names=tags)
        p.difficulty_score = score
        return p.id


async def test_find_leeches_threshold(pr_user):
    """lapses 达阈值的题必须被标记，未达阈值的不出现。"""
    _client, _token, uid = pr_user
    p_leech = await _make_problem("顽固题A", ["贪心"])
    p_ok = await _make_problem("健康题B", ["二分"])

    # 直接造 FSRS 记录：rating=1 四次 -> lapses=3（首次失败不计 lapse，
    # 与 Anki 同语义，实测 reps=1 lapses=0 起步）
    async with session_scope() as db:
        for _ in range(4):
            await upsert_review_item(db, uid, p_leech, 1)

    async with session_scope() as db:
        radar = await find_leeches(db, uid)

    ids = [i["problem_id"] for i in radar["items"]]
    assert p_leech in ids
    assert p_ok not in ids, "无 FSRS 记录的题不该出现"
    item = next(i for i in radar["items"] if i["problem_id"] == p_leech)
    assert item["lapses"] >= 3
    assert item["reason"], "必须带可解释理由"
    assert radar["thresholds"]["lapses"] == 3


async def test_interleaved_adjacent_tags_differ(pr_user):
    """混排核心语义：相邻两题主知识点尽量不同。"""
    _client, _token, uid = pr_user
    # 三组同知识点题 + 每组都有到期/顽固记录
    tags_groups = [["贪心"], ["二分"], ["动态规划"]]
    for gi, tags in enumerate(tags_groups):
        for k in range(2):
            pid = await _make_problem(f"混排{gi}-{k}", tags)
            async with session_scope() as db:
                for _ in range(2):
                    await upsert_review_item(db, uid, pid, 1)

    async with session_scope() as db:
        result = await build_interleaved(db, uid, size=6)

    items = result["items"]
    assert len(items) == 6
    adjacent_same = sum(
        1 for a, b in itertools.pairwise(items) if a["primary_tag"] == b["primary_tag"]
    )
    assert adjacent_same == 0, "三桶轮转下相邻必须不同知识点"
    assert result["stats"]["interleaved_pairs"] == len(items) - 1
    for i in items:
        assert i["reason"] and i["source"] in ("review", "leech", "fresh")


async def test_interleaved_size_cap(pr_user):
    _client, _token, uid = pr_user
    for k in range(3):
        pid = await _make_problem(f"容量{k}", ["模拟"])
        async with session_scope() as db:
            await upsert_review_item(db, uid, pid, 2)
    async with session_scope() as db:
        result = await build_interleaved(db, uid, size=2)
    assert len(result["items"]) == 2


def test_rule_intervention_shape():
    item = {"tags": ["贪心"], "reason": "已遗忘 3 次；最弱掌握度 25", "lapses": 3}
    plan = rule_intervention(item)
    assert "规则引擎诊断" in plan["diagnosis"]
    assert len(plan["sub_problems"]) >= 2
    assert plan["degraded"] is True


async def test_practice_api_endpoints(pr_user):
    """API 契约：雷达/混排/干预方案 三端点 + 404 分支。"""
    client, token, uid = pr_user
    H = {"Authorization": f"Bearer {token}"}
    pid = await _make_problem("API练习题", ["贪心"])
    async with session_scope() as db:
        for _ in range(4):
            await upsert_review_item(db, uid, pid, 1)

    r = await client.get("/api/practice/leech", headers=H)
    assert r.status_code == 200 and "items" in r.json() and "thresholds" in r.json()

    r = await client.post("/api/practice/interleaved", json={"size": 3}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] and body["stats"]["size"] == len(body["items"])

    # 不在雷达中的题 -> 404
    other = await _make_problem("非顽固题", ["二分"])
    r = await client.post(f"/api/practice/leech/{other}/plan", headers=H)
    assert r.status_code == 404

    # mock 下干预方案走规则路径
    r = await client.post(f"/api/practice/leech/{pid}/plan", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["diagnosis"]
