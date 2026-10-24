"""对话式辅导（多轮）的回归测试：会话治理、强度升降、上下文组装、SSE。"""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.chat import (
    ChatTurn,
    CoachContext,
    build_messages,
    initial_strength,
    next_strength,
    rule_reply,
)


@pytest.fixture
async def two_chat_users(client):
    tokens = []
    for _ in range(2):
        name = f"chat{uuid.uuid4().hex[:8]}"
        r = await client.post(
            "/api/auth/register",
            json={"username": name, "password": "test123456", "nickname": name},
        )
        assert r.status_code == 201, r.text
        tokens.append(r.json()["access_token"])
    return client, tokens[0], tokens[1]


async def _make_problem(client, token) -> str:
    r = await client.post(
        "/api/problems",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "对话辅导测试题", "statement": "求区间的最大重叠数。", "tags": ["贪心"]},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ==================================================== 强度治理（纯函数）
def test_strength_governance():
    """要更多 → 升一档封顶；想通了 → 回最弱档；中性消息 → 不变。"""
    assert next_strength("direction", "再提示一下") == "key_observation"
    assert next_strength("key_observation", "还是不懂") == "pseudocode"
    assert next_strength("pseudocode", "再直接一点") == "pseudocode", "封顶 pseudocode"
    assert next_strength("pseudocode", "我想到了！") == "direction", "想通后降回最弱档"
    assert next_strength("key_observation", "样例是 3 5 和 1 2，然后呢") == "key_observation"


def test_initial_strength_follows_hint_policy():
    """会话初始强度与单发 hint 同一口径：0 次提交给最强档。"""
    assert initial_strength(40.0, 0) == "pseudocode"
    assert initial_strength(85.0, 10) == "direction"


# ==================================================== 会话 API 与属主
async def test_session_crud_and_ownership(two_chat_users):
    client, ta, tb = two_chat_users
    r = await client.post("/api/chat/sessions", json={}, headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["title"] == "自由辅导"

    # 属主可见；B 访问 -> 404（不泄露存在性）
    assert (
        await client.get("/api/chat/sessions", headers={"Authorization": f"Bearer {ta}"})
    ).json()["items"]
    r = await client.get(f"/api/chat/sessions/{sid}", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404
    r = await client.delete(f"/api/chat/sessions/{sid}", headers={"Authorization": f"Bearer {tb}"})
    assert r.status_code == 404

    r = await client.delete(f"/api/chat/sessions/{sid}", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200


async def test_bound_session_initial_strength(two_chat_users):
    """绑题会话：新用户 0 次提交 → 初始强度 pseudocode（与 hint 口径一致）。"""
    client, ta, _ = two_chat_users
    pid = await _make_problem(client, ta)
    r = await client.post(
        "/api/chat/sessions", json={"problem_id": pid}, headers={"Authorization": f"Bearer {ta}"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["problem_id"] == pid
    assert body["title"] == "对话辅导测试题"
    assert body["strength"] == "pseudocode", "首次接触（0 次提交）应给最强档"


async def test_message_flow_updates_session(two_chat_users):
    """非流式：user+assistant 双双落库，强度随用户表态升降。"""
    client, ta, _ = two_chat_users
    pid = await _make_problem(client, ta)
    sid = (
        await client.post(
            "/api/chat/sessions",
            json={"problem_id": pid},
            headers={"Authorization": f"Bearer {ta}"},
        )
    ).json()["id"]

    r = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"content": "这题完全没思路"},
        headers={"Authorization": f"Bearer {ta}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["role"] == "assistant"
    assert body["strength"] in ("direction", "key_observation", "pseudocode")
    assert body["message"]["content"], "mock 下也必须有规则引擎回复（降级即离线路径）"

    detail = (
        await client.get(f"/api/chat/sessions/{sid}", headers={"Authorization": f"Bearer {ta}"})
    ).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["session"]["message_count"] == 2

    # 升档语义：会话强度随"再提示"上升一档
    before = detail["session"]["strength"]
    r = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"content": "再提示一下，还是不懂"},
        headers={"Authorization": f"Bearer {ta}"},
    )
    after = r.json()["strength"]
    assert after >= before or (before == "pseudocode" and after == "pseudocode")


# ==================================================== 流式 SSE
async def test_message_stream_events(two_chat_users):
    client, ta, _ = two_chat_users
    sid = (
        await client.post("/api/chat/sessions", json={}, headers={"Authorization": f"Bearer {ta}"})
    ).json()["id"]

    events: list[str] = []
    done_data = None
    async with client.stream(
        "POST",
        f"/api/chat/sessions/{sid}/messages?stream=true",
        json={"content": "怎么分析这道题"},
        headers={"Authorization": f"Bearer {ta}"},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if events and events[-1] == "done":
                    done_data = data
    assert events[0] == "open"
    assert "token" in events, "流式必须产出 token 事件"
    assert events[-1] == "done", "必须以 done 终态收尾"
    assert done_data and done_data["reply"], "done 必须携带完整回复"
    assert "strength" in done_data

    # 流式完成后消息落库
    detail = (
        await client.get(f"/api/chat/sessions/{sid}", headers={"Authorization": f"Bearer {ta}"})
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


# ==================================================== 上下文组装（纯函数）
def test_build_messages_budget_and_content():
    ctx = CoachContext(
        problem={"title": "T", "statement": "S" * 5000, "tags": ["贪心"], "difficulty": "普及-"},
        memory="画像：贪心薄弱",
        history=[ChatTurn(role="user", content="x" * 2000) for _ in range(15)],
    )
    msgs = build_messages(ctx, "y" * 5000, "key_observation")
    assert msgs[0]["role"] == "system" and "key_observation" in msgs[0]["content"]
    assert len(msgs) == 1 + 1 + 10 + 1, "近 10 轮 + 题面/记忆两个 system 块 + 本轮"
    assert len(msgs[2]["content"]) <= 600, "历史轮次必须裁剪"
    assert len(msgs[-1]["content"]) <= 2000, "本轮输入必须裁剪"
    assert "贪心薄弱" in msgs[1]["content"]


def test_rule_reply_honesty():
    """规则回复：不假装给解法、想通时鼓励自己写、要更多时给建议。"""
    ctx = CoachContext(problem={"tags": ["动态规划"]})
    r1 = rule_reply(ctx, "没思路", "direction")
    assert "规则引擎回复" in r1 and "动态规划" in r1
    r2 = rule_reply(ctx, "我想到了", "key_observation")
    assert "写成代码" in r2
    r3 = rule_reply(ctx, "再直接一点", "key_observation")
    assert "当前的代码" in r3
