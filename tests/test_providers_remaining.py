"""Providers 剩余分支：mock 内部生成器边界 + chat 流式错误分支。"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from test_providers_full import FakeAsyncClient, sse

from app.core.providers import (
    AnthropicProvider,
    GeminiProvider,
    MockProvider,
    OpenAIProvider,
)

SCHEMA = {"title": "Out", "type": "object"}


@pytest.fixture
def fake_client():
    FakeAsyncClient.post_queue = []
    FakeAsyncClient.stream_events = []
    FakeAsyncClient.stream_fail = None
    with patch("app.core.providers.httpx.AsyncClient", FakeAsyncClient):
        yield FakeAsyncClient


# ================================================================ mock 内部
def test_fixture_quality_fallback_branch():
    """QualityOut 走质量评分的分支（236 行）。"""
    p = MockProvider()
    payload = {
        "rule_baseline": 60.0,
        "allowed_range": [48.0, 72.0],
        "rule_metrics": {},
        "required_dimensions": ["正确性"],
    }
    out = p._fixture_for({"title": "QualityOut"}, user=json.dumps(payload))
    assert out["score"] == 60.0


def test_parse_payload_bad_json_fallback():
    """记忆块截取后仍是坏 JSON -> 返回 {}（277-278 行）。"""
    assert MockProvider._parse_payload('前缀 {"a": } 后缀') == {}
    # 合法 JSON 但不是 dict（list）-> {}（isinstance 分支）
    assert MockProvider._parse_payload("[1, 2]") == {}


def test_judge_wa_verdict_fallback():
    """非法 verdict 回退 WA（361 行）。"""
    p = MockProvider()
    out = p._judge_from_prompt({"verdict": "bogus-verdict", "code": "x"})
    assert out["verdict"] == "WA"


def test_review_skips_empty_candidate_ids():
    """基线顺序里有空 id / 不存在的 id 时跳过（453 行）。"""
    p = MockProvider()
    payload = {
        "candidates": [
            {
                "problem_id": "real",
                "title": "真",
                "tags": ["模拟"],
                "overdue_days": 1,
                "lapses": 0,
                "mastery_scores": {"模拟": 30.0},
            }
        ],
        "rule_baseline_order": ["", "missing", "real"],
    }
    out = p._review_from_prompt(payload)
    assert [i["problem_id"] for i in out["ordered"]] == ["real"]


# ================================================================ gemini 错误分支
async def test_gemini_complete_network_error(fake_client):
    """Gemini complete 的 HTTPError -> network（684/1075 行同型）。"""
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [httpx.ConnectError("c")]
    with pytest.raises(Exception) as ei:
        await p.complete("s", "u", SCHEMA, "gm")
    assert "network" in str(ei.value)


async def test_gemini_chat_timeout_and_network(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.post_queue = [httpx.ConnectError("c")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "network" in str(ei.value)


async def test_openai_chat_timeout_and_stream_chat_timeout(fake_client):
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.stream_fail = httpx.TimeoutException("t")
    with pytest.raises(httpx.TimeoutException):
        got = []
        async for x in p.stream_chat([{"role": "user", "content": "u"}], "m"):
            got.append(x)
    FakeAsyncClient.stream_fail = None


async def test_anthropic_chat_timeout_and_stream_chat_timeout(fake_client):
    p = AnthropicProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.stream_fail = httpx.TimeoutException("t")
    with pytest.raises(httpx.TimeoutException):
        got = []
        async for x in p.stream_chat([{"role": "user", "content": "u"}], "m"):
            got.append(x)
    FakeAsyncClient.stream_fail = None


# ================================================================ 流跳过分支
async def test_stream_skip_branches_all_providers(fake_client):
    """三家 chat 流的坏 JSON / 空字段跳过分支。"""
    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"choices": [{"delta": {}}]}),
            json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
            "[DONE]",
        ]
    )
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    assert (
        "".join([x async for x in p.stream_chat([{"role": "user", "content": "u"}], "m")]) == "ok"
    )

    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"type": "content_block_start"}),
            json.dumps({"type": "content_block_delta", "delta": {}}),
            json.dumps({"type": "content_block_delta", "delta": {"text": "ok"}}),
        ]
    )
    pa = AnthropicProvider(base_url="https://x", api_key="k")
    assert (
        "".join([x async for x in pa.stream_chat([{"role": "user", "content": "u"}], "m")]) == "ok"
    )

    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"candidates": []}),
            json.dumps({"candidates": [{"content": {}}]}),
            json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
        ]
    )
    pg = GeminiProvider(base_url="https://x", api_key="k")
    assert (
        "".join([x async for x in pg.stream_chat([{"role": "user", "content": "u"}], "m")]) == "ok"
    )


# ================================================================ gemini schema 收窄
def test_gemini_schema_narrowing_nested():
    """_to_gemini_schema 递归收窄（958/966 行）与复杂 schema。"""
    p = GeminiProvider(base_url="https://x", api_key="k")
    nested = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "tags": {"type": "array", "items": {"type": "string", "format": "x"}},
            "confidence": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    out = p._to_gemini_schema(nested)
    assert out["type"] == "object"
    assert "minLength" not in out["properties"]["name"]
    assert "exclusiveMinimum" not in out["properties"]["confidence"]
    assert out["properties"]["tags"]["items"]["type"] == "string"


# ================================================================ registry
def test_registry_sorted_and_random():
    """1147/1157 行：registry 列表与随机选择分支。"""
    from app.core.providers import available_providers, random_provider

    names = available_providers()
    assert names == sorted(names)
    assert all(n in ("mock", "openai", "anthropic", "gemini") for n in names)
    p = random_provider()
    assert p.name in names
