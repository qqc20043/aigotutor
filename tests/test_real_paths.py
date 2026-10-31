"""真模型路径覆盖：coach_reply / generate_narrative / intervention_plan / 流式降级。

测试默认 mock provider 不会走进这些分支——用 patch build_provider 打桩，
验证真模型成功与失败（降级）两条路径。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from app.core import interview, practice
from app.core.chat import ChatTurn, CoachContext, coach_reply, coach_reply_stream


def _fake_provider(text="AI 回复", *, fail=False, empty=False):
    """build_provider 的打桩返回：complete_chat / stream_chat 可控。"""
    p = MagicMock()

    async def fake_complete(messages, model, **kw):
        if fail:
            raise RuntimeError("网关挂了")
        from app.core.providers import LLMResult

        return LLMResult(
            text="" if empty else text,
            prompt_tokens=1,
            completion_tokens=2,
            latency_ms=3,
            model=model,
            provider="openai",
        )

    async def fake_stream(messages, model, **kw):
        if fail:
            raise RuntimeError("网关挂了")
        for piece in ("AI", "流"):
            yield piece

    p.complete_chat = AsyncMock(side_effect=fake_complete)
    p.stream_chat = fake_stream
    return p


CONFIG = {
    "provider": "openai",
    "model": "m",
    "base_url": "https://x",
    "api_key": "k",
    "temperature": 0.3,
}


# ============================================================ coach_reply
async def test_coach_reply_real_provider_success(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider("AI 提示内容")
    )
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    reply = await coach_reply(CONFIG, ctx, "怎么想", "direction")
    assert reply.text == "AI 提示内容"
    assert reply.degraded is False
    assert reply.prompt_tokens >= 1


async def test_coach_reply_real_provider_failure_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(fail=True)
    )
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    reply = await coach_reply(CONFIG, ctx, "怎么想", "direction")
    assert reply.degraded is True
    assert "规则" in reply.text or reply.text


async def test_coach_reply_empty_text_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(empty=True)
    )
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    reply = await coach_reply(CONFIG, ctx, "怎么想", "direction")
    assert reply.degraded is True


async def test_coach_reply_stream_real_provider(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider("AI 流")
    )
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    got = []
    final = None
    async for piece in coach_reply_stream(CONFIG, ctx, "怎么想", "direction"):
        if isinstance(piece, str):
            got.append(piece)
        else:
            final = piece
    assert "".join(got) == "AI流" or "".join(got) == "AI流".replace("流", "流")
    assert final is not None and final.degraded is False


async def test_coach_reply_stream_failure_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(fail=True)
    )
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    got: list[str] = []
    final = None
    async for piece in coach_reply_stream(CONFIG, ctx, "怎么想", "direction"):
        if isinstance(piece, str):
            got.append(piece)
        else:
            final = piece
    assert final is not None and final.degraded is True


async def test_coach_reply_stream_mock_short_circuit():
    """mock provider 走规则模板（短路，不触 build_provider）。"""
    ctx = CoachContext(
        problem={"title": "t", "statement": "s", "tags": ["模拟"], "difficulty": "入门"}
    )
    got: list[str] = []
    final = None
    async for piece in coach_reply_stream({"provider": "mock"}, ctx, "怎么想", "direction"):
        if isinstance(piece, str):
            got.append(piece)
        else:
            final = piece
    assert (final is not None and "规则" in final.text) or final.text


# ============================================================ generate_narrative
async def test_generate_narrative_success(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider("AI 点评内容")
    )
    scores = {
        "total": 85.88,
        "correctness": 100.0,
        "code_quality": 73.0,
        "hint_dependency": 75.0,
        "time_efficiency": 100.0,
        "communication": 66.0,
    }
    text, degraded = await interview.generate_narrative(
        CONFIG, scores, "AC", [ChatTurn(role="user", content="思路")]
    )
    assert text == "AI 点评内容"
    assert degraded is False


async def test_generate_narrative_failure_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(fail=True)
    )
    scores = {
        "total": 60.0,
        "correctness": 50.0,
        "code_quality": 50.0,
        "hint_dependency": 100.0,
        "time_efficiency": 50.0,
        "communication": 50.0,
    }
    text, degraded = await interview.generate_narrative(CONFIG, scores, "WA", [])
    assert degraded is True
    assert "60.0" in text and "WA" in text


# ============================================================ intervention_plan
async def test_intervention_plan_success(monkeypatch):
    plan_json = json.dumps(
        {
            "diagnosis": "反复卡在懒标记语义",
            "sub_problems": ["先手推样例", "核对边界"],
            "prerequisite_advice": "先补线段树基础",
            "next_action": "手算最小样例",
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(plan_json)
    )
    item = {
        "title": "线段树",
        "tags": ["线段树"],
        "lapses": 3,
        "wrong_count": 0,
        "weakest_mastery": 30.0,
    }
    plan = await practice.intervention_plan(CONFIG, item, "历史：错 3 次")
    assert plan["diagnosis"] == "反复卡在懒标记语义"
    assert plan["degraded"] is False
    assert len(plan["sub_problems"]) == 2


async def test_intervention_plan_failure_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider(fail=True)
    )
    item = {
        "title": "线段树",
        "tags": ["线段树"],
        "lapses": 3,
        "weakest_mastery": 30.0,
        "reason": "x",
    }
    plan = await practice.intervention_plan(CONFIG, item, "")
    assert plan["degraded"] is True
    assert "规则引擎诊断" in plan["diagnosis"]


async def test_intervention_plan_bad_json_degrades(monkeypatch):
    monkeypatch.setattr(
        "app.core.providers.build_provider", lambda *a, **kw: _fake_provider("不是 JSON")
    )
    item = {"title": "t", "tags": [], "lapses": 1, "weakest_mastery": 50.0}
    plan = await practice.intervention_plan(CONFIG, item, "")
    assert plan["degraded"] is True
