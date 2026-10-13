"""token 级流式与增量 JSON 解析测试（F02）。"""

from __future__ import annotations

import json

from app.agents import get_agent
from app.core.agent import AgentContext, IncrementalJSON
from app.core.providers import MockProvider


# ====================================================== IncrementalJSON
def test_incremental_parses_fields_as_they_close():
    p = IncrementalJSON()
    assert p.feed('{"summary": "abc"') == [("summary", "abc")]
    assert p.feed(', "language": "py"') == [("language", "py")]
    # 重复字段不重复推送
    assert p.feed(', "language": "py"') == []


def test_incremental_handles_char_by_char():
    """逐字符喂入也必须能得到全部字段（模拟最细粒度的 token 流）。"""
    text = json.dumps(
        {
            "problem_id": "p1",
            "title": "T",
            "difficulty": "普及-",
            "tags": ["排序"],
            "reason": "r",
            "expected_gain": 1.5,
        },
        ensure_ascii=False,
    )
    p = IncrementalJSON()
    seen: dict[str, object] = {}
    for ch in text:
        for k, v in p.feed(ch):
            seen[k] = v
    assert seen["problem_id"] == "p1"
    assert seen["tags"] == ["排序"]
    assert seen["expected_gain"] == 1.5
    assert len(seen) == 6


def test_incremental_does_not_confuse_value_text_with_key():
    """字段值里出现同名字符串时不能错位（旧实现的正则搜索会踩这个坑）。"""
    payload = {
        "summary": '注意 "summary" 这个词出现在值里',
        "language": "python",
    }
    p = IncrementalJSON()
    seen = {}
    for ch in json.dumps(payload, ensure_ascii=False):
        for k, v in p.feed(ch):
            seen[k] = v
    assert seen["summary"] == payload["summary"]
    assert seen["language"] == "python"


def test_incremental_buffer_guard():
    """超长输出应触发截断保护，避免内存被吃光。"""
    p = IncrementalJSON(max_buffer=100)
    p.feed('{"a": "')
    p.feed("x" * 200)
    assert p.truncated is True
    assert p.feed('"}') == []


def test_incremental_nested_objects():
    p = IncrementalJSON()
    seen = {}
    for ch in json.dumps({"complexity": {"time": "O(n)", "space": "O(1)"}, "n": 3}):
        for k, v in p.feed(ch):
            seen[k] = v
    assert seen["complexity"] == {"time": "O(n)", "space": "O(1)"}
    assert seen["n"] == 3


# ========================================================== 流式执行
async def test_run_stream_event_sequence():
    agent = get_agent("judge")
    ctx = AgentContext(provider="mock", model="mock-1", data={"verdict": "TLE", "code": "x"})
    events = [evt async for evt in agent.run_stream(ctx)]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start"
    assert "token" in kinds
    assert "field" in kinds
    assert kinds[-1] == "done"

    done = next(e for e in events if e["event"] == "done")
    assert done["result"]["status"] == "ok"
    assert done["result"]["data"]["root_cause"] == "复杂度"


async def test_run_stream_field_events_cover_schema():
    """结构化字段应随 token 闭合逐个推出，而不是等最后一次性给。"""
    agent = get_agent("mastery")
    ctx = AgentContext(
        provider="mock",
        model="mock-1",
        data={"tag_stats": {"排序": {"attempts": 10, "ac_count": 8}}},
    )
    events = [evt async for evt in agent.run_stream(ctx)]
    fields = [e["key"] for e in events if e["event"] == "field"]
    assert "score" in fields
    assert "level" in fields
    assert len(fields) >= 3


async def test_run_stream_degrades_when_provider_fails(monkeypatch):
    """流式路径失败也必须降级收尾，不能把异常抛给前端。"""
    from app.core.providers import LLMError

    class BoomProvider:
        name = "boom"

        async def complete(self, *a, **kw):
            raise LLMError("provider", "挂了")

        async def stream(self, *a, **kw):
            raise LLMError("provider", "流也挂了")
            yield ""  # pragma: no cover

    import app.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "build_provider", lambda *a, **kw: BoomProvider())
    agent = get_agent("judge")
    ctx = AgentContext(provider="mock", model="mock-1", data={"verdict": "WA", "code": "x"})
    events = [evt async for evt in agent.run_stream(ctx)]
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "done"
    assert "degraded" in kinds
    done = next(e for e in events if e["event"] == "done")
    assert done["result"]["degraded"] is True
    assert done["result"]["status"] == "degraded"


# ================================================== mock 键名兼容性
async def test_mock_recommend_accepts_both_key_names():
    """候选键名 id / problem_id 都要认（真实链路里踩过返回 "None" 的坑）。"""
    provider = MockProvider()
    schema = get_agent("recommend").json_schema()

    for key in ("id", "problem_id"):
        payload = {
            "candidates": [
                {
                    "problem_id": "p1",
                    "title": "A",
                    "difficulty": "普及-",
                    "difficulty_score": 45.0,
                    "tags": ["排序"],
                },
            ],
            "mastery": {"排序": 30.0},
            "recent_problem_ids": [],
            "target_score": 55.0,
        }
        # 把候选键名换成待测的形式
        cand = dict(payload["candidates"][0])
        if key == "id":
            cand = {
                "id": cand.pop("problem_id"),
                **{k: v for k, v in cand.items() if k != "problem_id"},
            }
        payload["candidates"] = [cand]

        result = await provider.complete(
            "s", json.dumps(payload, ensure_ascii=False), schema, "mock-1"
        )
        data = json.loads(result.text)
        assert data["problem_id"] == "p1", f"键名 {key} 未被识别，得到 {data['problem_id']!r}"


async def test_mock_recommend_empty_candidates_is_honest():
    provider = MockProvider()
    schema = get_agent("recommend").json_schema()
    payload = {"candidates": [], "mastery": {}, "target_score": 55.0}
    result = await provider.complete("s", json.dumps(payload), schema, "mock-1")
    data = json.loads(result.text)
    assert data["problem_id"] == ""
    assert "无法推荐" in data["reason"]


# ========================================================== 流式接口
async def test_stream_endpoint_emits_sse(auth_client):
    """SSE 端点：事件类型齐全，且以 closed 收尾。"""
    events: list[str] = []
    async with auth_client.stream(
        "POST",
        "/api/agent/stream",
        json={"agent": "judge", "payload": {"verdict": "WA", "code": "print(1)"}},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
    assert events[0] == "open"
    assert "token" in events
    assert "field" in events
    assert "done" in events
    assert events[-1] == "closed"


async def test_stream_endpoint_unknown_agent_404(auth_client):
    r = await auth_client.post("/api/agent/stream", json={"agent": "nope", "payload": {}})
    assert r.status_code == 404
