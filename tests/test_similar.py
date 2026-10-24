"""相似题检索（举一反三）的回归测试：召回、排序、可解释性与 API 契约。"""

from __future__ import annotations

import pytest

from app.core.similar import _fts_terms, find_similar
from app.db import session_scope
from app.services import get_problem, get_problem_tags


async def _create(client, title: str, statement: str, tags: list[str], score: float = 40.0):
    r = await client.post(
        "/api/problems",
        json={
            "title": title,
            "statement": statement,
            "tags": tags,
            "difficulty_score": score,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def gallery(auth_client):
    """一个小题库：两组同型题 + 一个无关题。"""
    ids = {}
    ids["src"] = await _create(
        auth_client,
        "区间合并",
        "给出 n 个区间，把重叠的区间合并，输出合并后的结果。",
        ["排序", "贪心"],
        40.0,
    )
    ids["same_tag"] = await _create(
        auth_client,
        "会议室安排",
        "若干会议时间段，求最多能参加多少个不冲突的会议。",
        ["贪心", "排序"],
        38.0,
    )
    ids["partial"] = await _create(
        auth_client, "跳跃游戏", "数组每格表示最大跳跃步数，判断能否到达终点。", ["贪心"], 30.0
    )
    ids["unrelated"] = await _create(
        auth_client, "树的前序遍历", "输出二叉树的前序遍历序列。", ["树"], 20.0
    )
    return ids


async def test_similar_ranks_same_tag_first(gallery):
    """同标签题必须排在无关题前面，且排除自身、带可解释理由。"""
    async with session_scope() as db:
        problem = await get_problem(db, gallery["src"])
        tag_map = await get_problem_tags(db, [problem.id])
        source = {
            "id": problem.id,
            "title": problem.title,
            "statement": problem.statement,
            "tags": [t["name"] for t in tag_map.get(problem.id, [])],
            "difficulty_score": problem.difficulty_score,
        }
        result = await find_similar(db, source, limit=5)

    ids = [i.problem_id for i in result["items"]]
    assert gallery["src"] not in ids, "不得返回自身"
    assert gallery["same_tag"] in ids, "双标签同型题必须被召回（标签路）"
    assert gallery["unrelated"] not in ids, "无共享标签且题面无关的题不应出现"
    # 排序：同标签 > 仅题面/部分标签
    assert ids.index(gallery["same_tag"]) < ids.index(gallery["partial"])
    top = result["items"][0]
    assert top.shared_tags, "结果必须带共享标签（可解释性）"
    assert "同标签" in top.reason
    assert result["retrieval"]["candidates"] >= 2
    assert result["retrieval"]["rebuilt"] is True, "首次检索应触发 FTS 全量重建"


async def test_similar_fts_rebuild_on_change(gallery):
    """题目更新后索引自动重建（updated_at 漂移检测）。"""
    async with session_scope() as db:
        problem = await get_problem(db, gallery["src"])
        source = {
            "id": problem.id,
            "title": problem.title,
            "statement": problem.statement,
            "tags": [],
            "difficulty_score": 40.0,
        }
        first = await find_similar(db, source, limit=5)
        assert first["retrieval"]["rebuilt"] is True
        second = await find_similar(db, source, limit=5)
        assert second["retrieval"]["rebuilt"] is False, "无变更时不应重复重建"


async def test_similar_empty_result_is_honest():
    """没有任何可召回题时返回空列表，不硬凑。"""
    from app.core.similar import find_similar as fs

    class _FakeDB:
        async def execute(self, *_a, **_kw):
            class _R:
                def fetchone(self):
                    return (0, 0, 0)

                def fetchall(self):
                    return []

                def scalar_one(self):
                    return 0

            return _R()

    result = await fs(
        _FakeDB(), {"id": "x", "title": "t", "statement": "", "tags": [], "difficulty_score": 0}
    )
    assert result["items"] == []
    assert result["retrieval"]["candidates"] == 0


def test_fts_terms_construction():
    """查询词构造：标题、≥4 字汉字段、≥4 字符英文词；过滤短词并限量。"""
    terms = _fts_terms(
        {
            "title": "区间合并",
            "tags": ["排序", "双指针"],
            "statement": "给定数组 nums 与目标 target，输出下标。栈结构。ab cd efgh",
        }
    )
    assert "区间合并" in terms
    assert "给定数组" in terms
    assert "efgh" in terms
    assert "栈" not in terms and "ab" not in terms, "短词构造不出 trigram，必须过滤"
    assert len(terms) <= 12


async def test_similar_api_endpoint(auth_client, gallery):
    """API 契约：200 + items 结构 + 404 分支。"""
    r = await auth_client.get(f"/api/problems/{gallery['src']}/similar")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["problem_id"] == gallery["src"]
    assert "rebuilt" in body["retrieval"] and "candidates" in body["retrieval"]
    items = body["items"]
    assert items, "小题库下应至少召回同型题"
    assert {"problem_id", "title", "shared_tags", "score", "reason"} <= set(items[0])

    r = await auth_client.get("/api/problems/not-exist/similar")
    assert r.status_code == 404

    r = await auth_client.get(f"/api/problems/{gallery['src']}/similar?limit=1")
    assert r.status_code == 200 and len(r.json()["items"]) <= 1
