"""Providers 全路径测试：三大 Provider 的 HTTP 成功/错误分支 + 对话方法。

策略：mock httpx.AsyncClient（providers 模块命名空间内），喂入 canned
响应对象，覆盖 complete/stream/complete_chat/stream_chat 的成功与
401/403/429/4xx/超时/网络错误分支。真模型行为另有行为评估覆盖。
"""

from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import patch

import httpx
import pytest

from app.core.providers import (
    AnthropicProvider,
    GeminiProvider,
    MockProvider,
    OpenAIProvider,
)

SCHEMA = {"title": "Out", "type": "object"}


# ------------------------------------------------------------- httpx 打桩
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    """AsyncClient 打桩：post 队列 / stream 事件队列。"""

    post_queue: ClassVar[list] = []
    stream_events: ClassVar[list] = []
    stream_fail: ClassVar[Exception | None] = None

    def __init__(self, *a, **kw): ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        item = type(self).post_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, method, url, json=None, headers=None):
        outer = self
        if type(outer).stream_fail is not None:
            raise type(outer).stream_fail

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_lines(self):
                for ev in type(self).__mro__ and outer.stream_events:
                    yield ev

        # stream_events 存在类上供 aiter_lines 读取
        _Ctx.stream_events = type(outer).stream_events
        return _Ctx()


def sse(lines: list[str]) -> list[str]:
    return [f"data: {line}" for line in lines]


@pytest.fixture
def fake_client():
    FakeAsyncClient.post_queue = []
    FakeAsyncClient.stream_events = []
    with patch("app.core.providers.httpx.AsyncClient", FakeAsyncClient):
        yield FakeAsyncClient


# ================================================================ OpenAI
async def test_openai_complete_success(fake_client):
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                "model": "gpt-x",
            },
        )
    ]
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    r = await p.complete("sys", "user", SCHEMA, "gpt-x")
    assert r.text == '{"ok": true}'
    assert r.prompt_tokens == 5 and r.completion_tokens == 7
    assert r.provider == "openai"


async def test_openai_complete_error_branches(fake_client):
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    for status, kind in ((401, "auth"), (429, "rate_limit"), (500, "provider")):
        FakeAsyncClient.post_queue = [FakeResponse(status, {"detail": "x"})]
        with pytest.raises(Exception) as ei:
            await p.complete("s", "u", SCHEMA, "m")
        assert kind in str(ei.value)


async def test_openai_complete_timeout_and_network(fake_client):
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete("s", "u", SCHEMA, "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.post_queue = [httpx.ConnectError("c")]
    with pytest.raises(Exception) as ei:
        await p.complete("s", "u", SCHEMA, "m")
    assert "network" in str(ei.value)


async def test_openai_stream_success_and_malformed(fake_client):
    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"choices": [{"delta": {"content": "你"}}]}),
            "{bad json",  # 会被跳过
            json.dumps({"choices": [{"delta": {"content": "好"}}]}),
            "[DONE]",
            "data: junk",
        ]
    )
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    got = [piece async for piece in p.stream("s", "u", "m")]
    assert "".join(got) == "你好"


async def test_openai_complete_chat_and_stream_chat(fake_client):
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "回复"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )
    ]
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    r = await p.complete_chat(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], "m"
    )
    assert r.text == "回复"

    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"choices": [{"delta": {"content": "A"}}]}),
            "[DONE]",
        ]
    )
    got = [x async for x in p.stream_chat([{"role": "user", "content": "hi"}], "m")]
    assert "".join(got) == "A"


async def test_openai_chat_error_branches(fake_client):
    p = OpenAIProvider(base_url="https://x/v1", api_key="sk")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.post_queue = [FakeResponse(429, {})]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "rate_limit" in str(ei.value)


# ================================================================ Anthropic
async def test_anthropic_complete_success_and_missing_tool_use(fake_client):
    p = AnthropicProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "content": [{"type": "tool_use", "input": {"a": 1}}],
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "model": "claude",
            },
        )
    ]
    r = await p.complete("sys", "user", SCHEMA, "claude")
    assert json.loads(r.text) == {"a": 1}

    FakeAsyncClient.post_queue = [
        FakeResponse(200, {"content": [{"type": "text", "text": "no tool"}]})
    ]
    with pytest.raises(Exception) as ei:
        await p.complete("sys", "user", SCHEMA, "claude")
    assert "bad_json" in str(ei.value)


async def test_anthropic_complete_error_branches_and_stream(fake_client):
    p = AnthropicProvider(base_url="https://x", api_key="k")
    for status, kind in ((403, "auth"), (429, "rate_limit"), (400, "provider")):
        FakeAsyncClient.post_queue = [FakeResponse(status, {})]
        with pytest.raises(Exception) as ei:
            await p.complete("s", "u", SCHEMA, "m")
        assert kind in str(ei.value)

    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"type": "content_block_delta", "delta": {"text": "嗨"}}),
            "{bad",
            json.dumps({"type": "content_block_delta", "delta": {"text": "呀"}}),
        ]
    )
    got = [x async for x in p.stream("s", "u", "m")]
    assert "".join(got) == "嗨呀"


async def test_anthropic_chat_methods(fake_client):
    p = AnthropicProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "content": [{"type": "text", "text": "聊天回复"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )
    ]
    r = await p.complete_chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}], "m"
    )
    assert r.text == "聊天回复"

    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"type": "content_block_delta", "delta": {"text": "流"}}),
        ]
    )
    got = [x async for x in p.stream_chat([{"role": "user", "content": "u"}], "m")]
    assert "".join(got) == "流"


async def test_anthropic_chat_errors(fake_client):
    p = AnthropicProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.post_queue = [FakeResponse(403, {})]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "auth" in str(ei.value)


# ================================================================ Gemini
async def test_gemini_complete_success_and_missing_content(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "gm 回复"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
                "modelVersion": "gm",
            },
        )
    ]
    r = await p.complete("sys", "user", SCHEMA, "gm")
    assert r.text == "gm 回复"

    FakeAsyncClient.post_queue = [FakeResponse(200, {"candidates": []})]
    with pytest.raises(Exception) as ei:
        await p.complete("sys", "user", SCHEMA, "gm")
    assert "候选内容" in str(ei.value)


async def test_gemini_complete_error_branches(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    for status, kind in ((403, "auth"), (429, "rate_limit"), (500, "provider")):
        FakeAsyncClient.post_queue = [FakeResponse(status, {})]
        with pytest.raises(Exception) as ei:
            await p.complete("s", "u", SCHEMA, "m")
        assert kind in str(ei.value)


async def test_gemini_stream(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"candidates": [{"content": {"parts": [{"text": "g"}]}}]}),
            "{bad",
            json.dumps({"candidates": [{"content": {"parts": [{"text": "m"}]}}]}),
        ]
    )
    got = [x async for x in p.stream("s", "u", "m")]
    assert "".join(got) == "gm"


async def test_gemini_chat_methods(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [
        FakeResponse(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "gm 聊天"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
            },
        )
    ]
    r = await p.complete_chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}], "m"
    )
    assert r.text == "gm 聊天"

    FakeAsyncClient.stream_events = sse(
        [
            json.dumps({"candidates": [{"content": {"parts": [{"text": "g流"}]}}]}),
        ]
    )
    got = [x async for x in p.stream_chat([{"role": "user", "content": "u"}], "m")]
    assert "".join(got) == "g流"


async def test_gemini_chat_errors(fake_client):
    p = GeminiProvider(base_url="https://x", api_key="k")
    FakeAsyncClient.post_queue = [httpx.TimeoutException("t")]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "timeout" in str(ei.value)
    FakeAsyncClient.post_queue = [FakeResponse(429, {})]
    with pytest.raises(Exception) as ei:
        await p.complete_chat([], "m")
    assert "rate_limit" in str(ei.value)


# ================================================================ Mock 对话契约
async def test_mock_chat_contract():
    """MockProvider 也必须实现对话方法（Protocol 契约，防 AttributeError）。"""
    p = MockProvider()
    r = await p.complete_chat([{"role": "user", "content": "你好"}], "mock-1")
    assert "Mock" in r.text
    got = [x async for x in p.stream_chat([{"role": "user", "content": "hi"}], "mock-1")]
    assert "".join(got) == "Mock 回复：已收到「hi」。离线模式下请使用规则引擎路径查看结构化辅导。"


# ================================================================ 网关错误映射（judge 无关，纯构造）
def test_build_provider_registry_complete():
    from app.core.providers import build_provider

    for name, cls in (
        ("mock", MockProvider),
        ("openai", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("gemini", GeminiProvider),
    ):
        assert isinstance(build_provider(name, "https://x", "k"), cls)


# ================================================================ 流错误分支
async def test_stream_network_error_branches(fake_client):
    """三家 stream 的网络错误向上传播（由 BaseAgent.run_stream 统一捕获降级）。"""
    FakeAsyncClient.stream_fail = httpx.HTTPError("boom")
    for provider in (
        OpenAIProvider(base_url="https://x", api_key="k"),
        AnthropicProvider(base_url="https://x", api_key="k"),
        GeminiProvider(base_url="https://x", api_key="k"),
    ):
        with pytest.raises(httpx.HTTPError):
            got = []
            async for x in provider.stream("s", "u", "m"):
                got.append(x)
    FakeAsyncClient.stream_fail = None


async def test_stream_chat_network_error_branches(fake_client):
    FakeAsyncClient.stream_fail = httpx.HTTPError("boom")
    for provider in (
        OpenAIProvider(base_url="https://x", api_key="k"),
        AnthropicProvider(base_url="https://x", api_key="k"),
        GeminiProvider(base_url="https://x", api_key="k"),
    ):
        with pytest.raises(httpx.HTTPError):
            got = []
            async for x in provider.stream_chat([{"role": "user", "content": "u"}], "m"):
                got.append(x)
    FakeAsyncClient.stream_fail = None


async def test_stream_skips_malformed_and_empty_deltas(fake_client):
    """三家 stream 都要跳过坏 JSON / 空 delta / 非目标事件。"""
    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"choices": [{"delta": {}}]}),  # 空 delta
            "junk-line",
            json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
            "[DONE]",
        ]
    )
    p = OpenAIProvider(base_url="https://x/v1", api_key="k")
    assert "".join([x async for x in p.stream("s", "u", "m")]) == "ok"

    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"type": "content_block_start"}),  # 非目标事件
            json.dumps({"type": "content_block_delta", "delta": {}}),  # 空 text
            json.dumps({"type": "content_block_delta", "delta": {"text": "ok"}}),
        ]
    )
    pa = AnthropicProvider(base_url="https://x", api_key="k")
    assert "".join([x async for x in pa.stream("s", "u", "m")]) == "ok"

    FakeAsyncClient.stream_events = sse(
        [
            "{bad",
            json.dumps({"candidates": []}),
            json.dumps({"candidates": [{"content": {}}]}),
            json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
        ]
    )
    pg = GeminiProvider(base_url="https://x", api_key="k")
    assert "".join([x async for x in pg.stream("s", "u", "m")]) == "ok"


# ================================================================ mock 内部剩余分支
async def test_mock_internal_remaining_branches():
    p = MockProvider()
    # judge WA 根因分支（361）
    out = p._judge_from_prompt({"verdict": "WA", "code": "x"})
    assert out["verdict"] == "WA"
    # hint 强度回退（407）：无效 strength 字符串 -> key_observation
    out = p._hint_from_prompt({"strength": "bogus", "tags": ["模拟"]})
    assert out["strength"] == "key_observation"
    # recommend 循环 continue（453）：候选缺 id
    out = p._recommend_from_prompt({"candidates": [{"title": "无id"}], "mastery": {}})
    assert out["problem_id"] == ""
    # review 无候选 -> None（475）
    assert p._review_from_prompt({"candidates": [], "rule_baseline_order": []}) is None
    # 解析容错（277-278）：payload 非 dict
    assert MockProvider._parse_payload("[1, 2]") == {}
