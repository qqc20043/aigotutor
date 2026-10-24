"""模拟面试的回归测试：评分 Rubric（纯函数）+ API 主流程 + 属主校验。"""

from __future__ import annotations

import uuid

import pytest

from app.core.chat import ChatTurn
from app.core.interview import (
    communication_turn_scores,
    compute_scores,
    hint_request_flag,
    interview_messages,
    narrative_fallback,
    score_correctness,
    score_hint_dependency,
    score_time_efficiency,
)


# ==================================================== 评分纯函数
def test_score_correctness():
    assert score_correctness("AC", 4, 4) == 100.0
    assert score_correctness("WA", 3, 4) == 75.0
    assert score_correctness("TLE", 0, 4) == 0.0
    assert score_correctness("IE", 0, 0) == 0.0, "无从判题不得给分"
    assert score_correctness("", 0, 0) == 0.0


def test_score_hint_dependency():
    assert score_hint_dependency(0) == 100.0
    assert score_hint_dependency(2) == 50.0
    assert score_hint_dependency(10) == 0.0, "下限 0"


def test_score_time_efficiency():
    assert score_time_efficiency(10 * 60, 30) == 100.0, "≤ 时限一半满分"
    assert score_time_efficiency(30 * 60, 30) == 20.0, "到限时地板分"
    assert score_time_efficiency(45 * 60, 30) == 20.0, "超时钳在地板"
    mid = score_time_efficiency(22.5 * 60, 30)
    assert 20.0 < mid < 100.0, "中间线性衰减"
    assert score_time_efficiency(100, 0) == 60.0, "未限时不奖惩"


def test_communication_turn_scores():
    history = [
        ChatTurn(role="user", content="我觉得可以用哈希表存每个数出现的下标，然后遍历一遍找互补值"),
        ChatTurn(role="assistant", content="好"),
        ChatTurn(role="user", content="嗯"),  # 过短不算
        ChatTurn(role="user", content="复杂度是 O(n)，因为只扫了一遍"),
    ]
    valid, total, score = communication_turn_scores(history)
    assert (valid, total) == (2, 3)
    assert score == round(2 / 3 * 100, 2)


def test_compute_scores_weighted_total():
    scores = compute_scores(
        verdict="AC",
        passed=4,
        total=4,
        quality_baseline=80.0,
        hint_requests=1,
        elapsed_seconds=10 * 60,
        limit_minutes=30,
        history=[
            ChatTurn(role="user", content="我的思路是排序后双指针，因为这样能把问题降为线性扫描")
        ],
    )
    assert scores["correctness"] == 100.0
    assert scores["code_quality"] == 80.0
    assert scores["hint_dependency"] == 75.0
    assert scores["time_efficiency"] == 100.0
    assert scores["communication"] == 100.0
    expected = round(100 * 0.35 + 80 * 0.20 + 75 * 0.15 + 100 * 0.15 + 100 * 0.15, 2)
    assert scores["total"] == expected
    assert abs(sum(scores["weights"].values()) - 1.0) < 1e-9


def test_hint_flag_and_narrative_fallback():
    assert hint_request_flag("(提示) 可以想想单调性")
    assert hint_request_flag("（提示）边界怎么处理")
    assert not hint_request_flag("可以，那我们看代码")
    scores = {
        "total": 61.5,
        "correctness": 100.0,
        "code_quality": 50.0,
        "hint_dependency": 50.0,
        "time_efficiency": 60.0,
        "communication": 0.0,
    }
    text = narrative_fallback(scores, "WA")
    assert "61.5" in text and "WA" in text and "规则模板" in text


def test_interview_messages_shape():
    from app.core.chat import CoachContext

    ctx = CoachContext(
        problem={"title": "T", "statement": "S", "tags": ["贪心"], "difficulty": "普及-"}
    )
    msgs = interview_messages(ctx, "我的思路是……", "direction")
    assert msgs[0]["role"] == "system" and "面试官" in msgs[0]["content"]
    assert "绝不给出解题步骤" in msgs[0]["content"], "禁答案硬规则必须在 system prompt"


# ==================================================== API 主流程
async def _register(client) -> str:
    r = await client.post(
        "/api/auth/register",
        json={"username": f"iv{uuid.uuid4().hex[:8]}", "password": "test123456", "nickname": "iv"},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def _problem(client, token) -> str:
    r = await client.post(
        "/api/problems",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "面试题", "statement": "两数之和类题面", "tags": ["模拟"]},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.put(
        f"/api/problems/{pid}/testcases",
        headers={"Authorization": f"Bearer {token}"},
        json={"replace": True, "cases": [{"input": "1 2\n", "output": "3\n", "hidden": True}]},
    )
    assert r.status_code == 200, r.text
    return pid


@pytest.fixture
async def iv_user(client):
    token = await _register(client)
    return client, token


async def test_full_interview_flow(iv_user):
    """开始 → 对话 → 提交 WA → 索要提示（计数）→ 结束 → 报告五维齐全。"""
    client, token = iv_user
    H = {"Authorization": f"Bearer {token}"}
    pid = await _problem(client, token)

    r = await client.post(
        "/api/interview/start", json={"problem_id": pid, "time_limit_minutes": 5}, headers=H
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["status"] == "ongoing"

    r = await client.post(f"/api/interview/{sid}/message", json={"content": "还没想好"}, headers=H)
    assert r.status_code == 200
    assert "思路" in r.json()["reply"], "mock 考官必须追问思路"

    r = await client.post(
        f"/api/interview/{sid}/submit",
        json={"language": "python", "code": "print(0)"},
        headers=H,
    )
    assert r.status_code == 200 and r.json()["verdict"] == "WA"

    r = await client.post(
        f"/api/interview/{sid}/message",
        json={"content": "再提示一下吧", "ask_hint": True},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["hint_requests"] == 1, "索要提示必须计数"

    r = await client.post(f"/api/interview/{sid}/finish", headers=H)
    assert r.status_code == 200, r.text
    scores = r.json()["scores"]
    assert {
        "correctness",
        "code_quality",
        "hint_dependency",
        "time_efficiency",
        "communication",
        "total",
    } <= set(scores)
    assert scores["hint_dependency"] == 75.0, "1 次索要 → 75 分"
    assert scores["correctness"] == 0.0, "WA 且 0 通过 → 0 分"
    assert r.json()["degraded"] is True, "mock 下叙事走模板"

    r = await client.get(f"/api/interview/{sid}/report", headers=H)
    assert r.status_code == 200 and r.json()["report"]["verdict"] == "WA"

    # 幂等：再 finish 返回 already
    r = await client.post(f"/api/interview/{sid}/finish", headers=H)
    assert r.json().get("already") is True

    # 列表
    r = await client.get("/api/interview", headers=H)
    assert any(i["id"] == sid for i in r.json()["items"])


async def test_interview_message_after_finish_conflict(iv_user):
    client, token = iv_user
    H = {"Authorization": f"Bearer {token}"}
    pid = await _problem(client, token)
    sid = (await client.post("/api/interview/start", json={"problem_id": pid}, headers=H)).json()[
        "id"
    ]
    await client.post(f"/api/interview/{sid}/finish", headers=H)
    r = await client.post(
        f"/api/interview/{sid}/message", json={"content": "还能说话吗"}, headers=H
    )
    assert r.status_code == 409


async def test_interview_ownership(iv_user):
    client, ta = iv_user
    tb = await _register(client)
    pid = await _problem(client, ta)
    sid = (
        await client.post(
            "/api/interview/start",
            json={"problem_id": pid},
            headers={"Authorization": f"Bearer {ta}"},
        )
    ).json()["id"]
    for path in (f"/api/interview/{sid}", f"/api/interview/{sid}/report"):
        r = await client.get(path, headers={"Authorization": f"Bearer {tb}"})
        assert r.status_code == 404, "他人访问必须 404"
    r = await client.post(f"/api/interview/{sid}/finish", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404


async def test_real_sandbox_ac_scores_full_correctness(iv_user):
    """真实沙箱 AC → correctness=100（走 subprocess 判题而非自报）。"""
    client, token = iv_user
    H = {"Authorization": f"Bearer {token}"}
    pid = await _problem(client, token)
    sid = (await client.post("/api/interview/start", json={"problem_id": pid}, headers=H)).json()[
        "id"
    ]
    r = await client.post(
        f"/api/interview/{sid}/submit",
        json={"language": "python", "code": "a,b=map(int,input().split());print(a+b)"},
        headers=H,
    )
    assert r.json()["verdict"] == "AC"
    r = await client.post(f"/api/interview/{sid}/finish", headers=H)
    assert r.json()["scores"]["correctness"] == 100.0
