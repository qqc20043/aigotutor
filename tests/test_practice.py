"""智能练习集的回归测试：顽固错题识别、混排交错语义、AI 干预降级链。"""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.practice import (
    build_interleaved,
    find_leeches,
    rule_intervention,
)
from app.db import session_scope
from app.models import ReviewQueue
from app.services import create_problem, upsert_review_item


async def _force_due(uid: str, pid: str):
    """把 FSRS 计划拨到"已到期"（rating=1 的 interval=0.5 天，当天造不出 due）。"""
    async with session_scope() as db:
        row_id = (
            await db.execute(
                select(ReviewQueue.id).where(
                    ReviewQueue.user_id == uid, ReviewQueue.problem_id == pid
                )
            )
        ).scalar_one()
        row = await db.get(ReviewQueue, row_id)
        row.due_at = row.due_at - timedelta(days=2)


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
    # 三组同知识点题 + 每组都有到期记录（rating=1 的 interval=0.5 天，
    # 当天造不出 due —— 用 _force_due 把 due_at 拨到过去）
    tags_groups = [["贪心"], ["二分"], ["动态规划"]]
    for gi, tags in enumerate(tags_groups):
        for k in range(2):
            pid = await _make_problem(f"混排{gi}-{k}", tags)
            async with session_scope() as db:
                for _ in range(2):
                    await upsert_review_item(db, uid, pid, 1)
            await _force_due(uid, pid)

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
            for _ in range(3):
                await upsert_review_item(db, uid, pid, 1)
        await _force_due(uid, pid)
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


# ==================================================== 搁置语义 + 配方（Anki 对齐第二轮）
async def test_suspend_removes_from_queue_and_mix(pr_user):
    """搁置的顽固卡：雷达仍可见（带状态），每日队列与混排不再出现。"""
    _c, _t, uid = pr_user
    pid = await _make_problem("搁置语义题", ["贪心"])
    async with session_scope() as db:
        for _ in range(4):
            await upsert_review_item(db, uid, pid, 1)

    # 搁置前：在队列与雷达
    from app.services import get_review_queue

    async with session_scope() as db:
        q = await get_review_queue(db, uid, due_only=False, limit=50)
        assert any(i["problem_id"] == pid for i in q)
        radar = await find_leeches(db, uid)
        assert any(i["problem_id"] == pid for i in radar["items"])

    # API 搁置
    client, token, _uid = pr_user
    H = {"Authorization": f"Bearer {token}"}
    r = await client.post(f"/api/practice/leech/{pid}/suspend", json={"suspended": True}, headers=H)
    assert r.status_code == 200 and r.json()["suspended"] is True

    # 队列与混排都不再有它；雷达仍可见且标记 suspended
    async with session_scope() as db:
        q2 = await get_review_queue(db, uid, due_only=False, limit=50)
        assert not any(i["problem_id"] == pid for i in q2), "搁置卡必须移出队列"
        radar2 = await find_leeches(db, uid)
        item = next(i for i in radar2["items"] if i["problem_id"] == pid)
        assert item["suspended"] is True, "雷达仍应可见（可恢复）"
        mix = await build_interleaved(db, uid, 10)
        assert not any(i["problem_id"] == pid for i in mix["items"]), "搁置卡不得进混排"

    # 恢复：回到队列
    r = await client.post(
        f"/api/practice/leech/{pid}/suspend", json={"suspended": False}, headers=H
    )
    assert r.status_code == 200
    async with session_scope() as db:
        q3 = await get_review_queue(db, uid, due_only=False, limit=50)
        assert any(i["problem_id"] == pid for i in q3)


async def test_mastered_auto_restores_suspended(pr_user):
    """标记掌握自动恢复搁置卡（修复完成回到队列的 Anki 闭环）。"""
    client, token, uid = pr_user
    H = {"Authorization": f"Bearer {token}"}
    pid = await _make_problem("掌握恢复题", ["二分"])
    async with session_scope() as db:
        for _ in range(4):
            await upsert_review_item(db, uid, pid, 1)
        from app.services import upsert_wrong_book

        await upsert_wrong_book(db, uid, pid, "WA")
    r = await client.post(f"/api/practice/leech/{pid}/suspend", json={"suspended": True}, headers=H)
    assert r.status_code == 200
    r = await client.post(f"/api/wrong-book/{pid}/mastered", headers=H)
    assert r.status_code == 200
    from app.services import get_review_queue

    async with session_scope() as db:
        q = await get_review_queue(db, uid, due_only=False, limit=50)
        assert any(i["problem_id"] == pid for i in q), "标记掌握必须自动恢复"


async def test_recipes_crud_and_generate(pr_user):
    """配方（Anki 筛选牌组）：存条件不存快照，按当前状态重抽。"""
    client, token, uid = pr_user
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for k in range(3):
        pid = await _make_problem(f"配方题贪心{k}", ["贪心"])
        async with session_scope() as db:
            await upsert_review_item(db, uid, pid, 2)
        pid2 = await _make_problem(f"配方题二分{k}", ["二分"])
        async with session_scope() as db:
            await upsert_review_item(db, uid, pid2, 2)

    # 保存"只练二分"的配方
    r = await client.post(
        "/api/practice/recipes",
        json={"name": "二分专项", "size": 5, "tags": ["二分"]},
        headers=H,
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["recipe"]["tags"] == ["二分"]

    # 按配方生成：只含二分题
    r = await client.post(f"/api/practice/recipes/{rid}/generate", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["items"]
    assert all("二分" in i["primary_tag"] or "二分" in i["tags"] for i in body["items"])
    assert body["recipe"]["name"] == "二分专项"

    # 列表 + 删除
    assert any(
        i["id"] == rid
        for i in (await client.get("/api/practice/recipes", headers=H)).json()["items"]
    )
    r = await client.delete(f"/api/practice/recipes/{rid}", headers=H)
    assert r.status_code == 200
    r = await client.post(f"/api/practice/recipes/{rid}/generate", headers=H)
    assert r.status_code == 404


async def test_interleaved_tags_filter(pr_user):
    """混排的 Custom Study 条件：tags 筛选只保留命中知识点。"""
    client, token, uid = pr_user
    pid_g = await _make_problem("筛贪心", ["贪心"])
    pid_d = await _make_problem("筛动态", ["动态规划"])
    async with session_scope() as db:
        await upsert_review_item(db, uid, pid_g, 2)
        await upsert_review_item(db, uid, pid_d, 2)
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = await client.post(
        "/api/practice/interleaved",
        json={"size": 5, "tags": ["贪心"]},
        headers=H,
    )
    body = r.json()
    assert body["items"]
    assert all("贪心" in i["tags"] for i in body["items"])
