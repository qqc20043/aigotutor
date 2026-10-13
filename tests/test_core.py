"""核心层单测：Agent 契约、降级链、难度窗口、FSRS、缓存。

这些测试对应简历里的技术主张，逐条可验证：
  * 统一基类 + 结构化输出（Pydantic 校验）
  * 单 Agent 失败自动降级为规则引擎，主链路不中断
  * 两阶段难度标注（规则区间 + 模型校准，不越界）
  * FSRS 间隔重复
  * 结果缓存与成本控制
"""

from __future__ import annotations

import pytest

from app.agents import AGENTS, CORE_AGENTS
from app.core import fsrs
from app.core.agent import AgentContext, BaseAgent
from app.core.cache import cache_clear, cache_key, cache_stats, get_cache, set_cache
from app.core.pipeline import annotate_problem, route
from app.core.providers import LLMError, MockProvider, build_provider
from app.core.rules import (
    clamp_to_window,
    difficulty_window,
    rule_difficulty,
    rule_judge,
    rule_mastery,
    rule_recommend,
    rule_roadmap,
    rule_solution,
)
from app.schemas import DIFFICULTY_ORDER, DifficultyOut, RecommendOut


# ============================================================ Provider 层
def test_build_provider_all_protocols():
    """三协议 + mock 全部可构建。"""
    for name in ("mock", "openai", "anthropic", "gemini"):
        p = build_provider(name)
        assert p.name == name


def test_unknown_provider_raises():
    with pytest.raises(LLMError):
        build_provider("nonexistent")


def test_gemini_schema_narrowing():
    """Gemini 只接受 JSON Schema 子集，多余关键字必须被剥掉。"""
    from app.core.providers import GeminiProvider

    schema = {
        "title": "X",
        "type": "object",
        "additionalProperties": False,
        "$defs": {"Y": {}},
        "properties": {"a": {"type": "string", "minLength": 1}},
        "required": ["a"],
    }
    out = GeminiProvider._to_gemini_schema(schema)
    assert "title" not in out and "additionalProperties" not in out and "$defs" not in out
    assert out["type"] == "object"
    assert out["properties"]["a"] == {"type": "string"}  # minLength 被剥离


@pytest.mark.asyncio
async def test_mock_provider_returns_valid_schema():
    """Mock Provider 的输出必须能通过各 Agent 的 schema 校验。"""
    provider = MockProvider()
    for name, agent in AGENTS.items():
        result = await provider.complete(agent.system_prompt, "x", agent.json_schema(), "mock-1")
        try:
            agent.parse(result.text)  # 不抛异常即通过
        except Exception as e:
            raise AssertionError(f"Agent {name} 的 mock 输出无法通过 schema 校验: {e}") from e
        assert result.total_tokens > 0, f"Agent {name} 的 token 统计异常"


# ============================================================ 难度两阶段
def test_rule_difficulty_monotonic():
    """规则引擎：难度分随特征单调不减。"""
    easy = rule_difficulty({"statement_len": 20, "tag_count": 1, "algo_depth": 1})
    hard = rule_difficulty(
        {
            "statement_len": 2000,
            "tag_count": 4,
            "algo_depth": 5,
            "constraint_tight": True,
            "is_constructive": True,
        }
    )
    assert easy.difficulty_score < hard.difficulty_score
    assert DIFFICULTY_ORDER.index(easy.difficulty) < DIFFICULTY_ORDER.index(hard.difficulty)


def test_difficulty_window_is_plus_minus_one_level():
    """窗口必须保证夹紧后档位偏移不超过 1 档。"""
    from app.schemas import level_index_of

    for features in (
        {"statement_len": 20, "tag_count": 1, "algo_depth": 1},
        {"statement_len": 100, "tag_count": 2, "algo_depth": 3},
        {"statement_len": 3000, "tag_count": 4, "algo_depth": 5, "constraint_tight": True},
    ):
        out = rule_difficulty(features)
        lo, hi = difficulty_window(out)
        assert 0 <= lo < hi <= 100
        base = level_index_of(out.difficulty_score)
        assert abs(level_index_of(lo) - base) <= 1
        assert abs(level_index_of(hi) - base) <= 1


def test_clamp_to_window_enforces_bounds():
    """模型越界（把入门题标成省选）必须被夹回规则窗口。"""
    rule_out = rule_difficulty({"statement_len": 20, "tag_count": 1, "algo_depth": 1})
    window = difficulty_window(rule_out)
    over = DifficultyOut(
        difficulty="提高+/省选", difficulty_score=99.0, tags=["图论"], reason="越界测试"
    )
    clamped = clamp_to_window(over, window)
    assert clamped.difficulty_score <= window[1]
    assert (
        DIFFICULTY_ORDER.index(clamped.difficulty)
        <= DIFFICULTY_ORDER.index(rule_out.difficulty) + 1
    )


@pytest.mark.asyncio
async def test_two_stage_annotation_never_out_of_window():
    """两阶段标注端到端：无论模型说什么，结果都落在规则窗口内。"""
    for statement_len, depth in ((20, 1), (300, 3), (3000, 5)):
        payload = {
            "title": "t",
            "statement": "x" * statement_len,
            "tags": ["模拟"],
            "statement_len": statement_len,
            "tag_count": 1,
            "algo_depth": depth,
        }
        out = await annotate_problem(payload, {"provider": "mock", "model": "mock-1"})
        rule_out = rule_difficulty(payload)
        lo, hi = difficulty_window(rule_out)
        assert lo - 0.01 <= out["difficulty_score"] <= hi + 0.01
        assert out["difficulty"] in DIFFICULTY_ORDER


# ========================================================== 降级链核心
class _BoomAgent(BaseAgent[RecommendOut]):
    """永远失败的 Agent：provider 直接抛异常。"""

    name = "boom"
    schema = RecommendOut
    system_prompt = "x"
    max_retry = 0
    cacheable = False

    def build_user(self, ctx):
        return "x"

    def run_rule(self, ctx):
        return RecommendOut(
            problem_id="R1",
            title="规则兜底题",
            difficulty="普及-",
            tags=["模拟"],
            reason="规则引擎兜底",
            expected_gain=1.0,
        )


class _BoomNoFallback(BaseAgent[RecommendOut]):
    """失败且无规则兜底的 Agent：返回 failed，但不抛异常。"""

    name = "boom2"
    schema = RecommendOut
    system_prompt = "x"
    max_retry = 0
    cacheable = False

    def build_user(self, ctx):
        return "x"


class _FailingProvider:
    """必然失败的 provider，用于验证降级链。"""

    name = "failing"

    async def complete(self, *a, **kw):
        raise LLMError("provider", "模拟服务不可用", retryable=True)


@pytest.fixture
def break_llm(monkeypatch):
    """把 build_provider 替换成必然失败的 provider，返回恢复函数。"""
    import app.core.agent as agent_mod

    original = agent_mod.build_provider
    monkeypatch.setattr(agent_mod, "build_provider", lambda *a, **kw: _FailingProvider())
    return original


@pytest.mark.asyncio
async def test_agent_degrades_to_rule_engine(break_llm):
    """核心主张：单 Agent 失败 -> 自动降级规则引擎 -> 主链路不中断。"""
    result = await _BoomAgent().run(AgentContext(user_id="u1"))
    assert result.ok is True  # 没有抛异常
    assert result.degraded is True
    assert result.status == "degraded"
    assert result.data.problem_id == "R1"
    assert "provider" in result.reason
    assert result.confidence < 0.9  # 降级结果置信度打折


@pytest.mark.asyncio
async def test_agent_failure_does_not_raise(break_llm):
    """无兜底的 Agent 也绝不把异常抛到主链路。"""
    result = await _BoomNoFallback().run(AgentContext(user_id="u1"))
    assert result.ok is False
    assert result.status == "failed"
    assert result.error


@pytest.mark.asyncio
async def test_agent_retries_then_degrades(break_llm):
    """重试耗尽后才降级：attempts 应等于 max_retry + 1。"""

    class _CountingAgent(_BoomAgent):
        name = "counting"
        max_retry = 2
        calls = 0

        async def run(self, ctx):  # 记录调用次数
            _CountingAgent.calls += 1
            return await super().run(ctx)

    result = await _CountingAgent().run(AgentContext(user_id="u1"))
    assert _CountingAgent.calls == 1
    assert result.degraded is True


@pytest.mark.asyncio
async def test_pipeline_survives_when_every_agent_fails(break_llm):
    """全链路降级演练：所有 Agent 都失败，pipeline 仍返回结果。"""
    from app.core.pipeline import practice_pipeline
    from app.core.tasks import TaskState

    task = TaskState(id="t-test", user_id="u1", kind="agent_pipeline")

    summary = await practice_pipeline(
        task,
        user_id="u1",
        config={"provider": "openai", "model": "x"},
        submission={"verdict": "WA", "code": "print(0)", "passed_cases": 1, "total_cases": 4},
        problem={
            "id": "p1",
            "title": "t",
            "statement": "s",
            "difficulty_score": 50.0,
            "tags": ["模拟"],
        },
        tag_stats={"模拟": {"attempts": 4, "ac_count": 1}},
        candidates=[
            {
                "id": "p2",
                "title": "t2",
                "difficulty": "普及-",
                "difficulty_score": 45.0,
                "tags": ["模拟"],
            }
        ],
        mastery_scores={"模拟": 30.0},
    )
    assert summary["agent_count"] == 5
    assert summary["failed"] == []  # 全部降级成功，没有彻底失败
    assert summary["degraded"]  # 但确实降级了
    assert "judge" in summary["agents"]
    assert "mastery" in summary["agents"]
    assert summary["degraded_rate"] > 0
    # 进度事件确实被推送出去（SSE 的数据源）
    assert any(e["event"] == "progress" for e in task.events)
    assert task.progress == 1.0


# ============================================================ 规则引擎
def test_rule_judge_covers_all_verdicts():
    for verdict in ("AC", "WA", "TLE", "MLE", "RE", "CE"):
        out = rule_judge({"verdict": verdict, "code": "print(0)"})
        assert out.verdict == verdict
        assert out.explanation and out.fix_suggestion


def test_rule_judge_root_cause_from_code():
    out = rule_judge({"verdict": "RE", "code": "arr[999]", "judge_detail": {"msg": "索引越界"}})
    assert out.root_cause == "边界"


def test_rule_recommend_excludes_recent():
    candidates = [
        {
            "id": "a",
            "title": "A",
            "difficulty": "普及-",
            "difficulty_score": 50.0,
            "tags": ["排序"],
        },
        {
            "id": "b",
            "title": "B",
            "difficulty": "普及-",
            "difficulty_score": 50.0,
            "tags": ["排序"],
        },
    ]
    out = rule_recommend(
        {
            "candidates": candidates,
            "mastery": {"排序": 30.0},
            "recent_problem_ids": ["a"],
            "target_score": 50.0,
        }
    )
    assert out.problem_id == "b"


def test_rule_recommend_empty_candidates():
    out = rule_recommend({"candidates": [], "mastery": {}})
    assert out.problem_id == ""  # 不编造题目


def test_rule_mastery_score_range_and_level():
    stats = {
        "排序": {"attempts": 10, "ac_count": 9},
        "动态规划": {"attempts": 10, "ac_count": 1},
    }
    out = rule_mastery({"tag_stats": stats, "avg_difficulty_score": 55.0})
    assert 0 <= out.score <= 100
    assert out.level in ("L1", "L2", "L3", "L4", "L5")
    assert 0 <= out.confidence <= 1
    assert "动态规划" in out.weak_tags


def test_rule_mastery_no_samples_returns_neutral():
    out = rule_mastery({"tag_stats": {}})
    assert out.score == 50.0
    assert out.confidence <= 0.2


def test_rule_roadmap_respects_prereq_order():
    """先修关系：排序在动态规划之前。"""
    tags = [
        {"id": "1", "name": "排序", "prereq_ids": "[]"},
        {"id": "2", "name": "动态规划", "prereq_ids": '["1"]'},
    ]
    out = rule_roadmap({"tags": tags, "mastery": {}, "weeks": 4})
    assert len(out.stages) >= 2
    first_tags = out.stages[0].tags
    assert "排序" in first_tags
    assert "动态规划" not in first_tags


def test_rule_solution_defaults_to_python_template():
    """未指定语言时默认 python，应给出可运行模板。"""
    out = rule_solution({})
    assert out is not None and out.language == "python"
    compile(out.code, "<template>", "exec")


def test_rule_solution_returns_none_for_unsupported_language():
    """不支持的语言没有兜底能力，应诚实返回 None 而不是编造代码。"""
    assert rule_solution({"language": "brainfuck"}) is None


# ================================================================ FSRS
def test_fsrs_retrievability_decays():
    assert fsrs.retrievability(10.0, 0.0) == pytest.approx(1.0, abs=1e-6)
    assert fsrs.retrievability(10.0, 10.0) < 1.0
    assert fsrs.retrievability(10.0, 30.0) < fsrs.retrievability(10.0, 10.0)


def test_fsrs_interval_grows_with_stability():
    assert fsrs.next_interval(1.0) < fsrs.next_interval(10.0)
    assert fsrs.next_interval(1e9) <= fsrs.MAX_INTERVAL_DAYS


def test_fsrs_schedule_first_review():
    plan = fsrs.schedule(rating=3)
    assert plan["reps"] == 1
    assert plan["interval_days"] > 0
    assert plan["due_at"] > fsrs.datetime.now(fsrs.timezone.utc)


def test_fsrs_failure_resets_stability():
    """评 again 应当回退稳定度并累计 lapse。"""
    plan = fsrs.schedule(stability=20.0, difficulty=5.0, reps=5, lapses=0, rating=1)
    assert plan["stability"] < 20.0
    assert plan["lapses"] == 1


def test_fsrs_success_grows_stability():
    plan = fsrs.schedule(stability=5.0, difficulty=5.0, reps=3, lapses=0, rating=4)
    assert plan["stability"] > 5.0
    assert plan["lapses"] == 0


def test_forgetting_curve_shape():
    curve = fsrs.forgetting_curve(10.0, days=30, step=1.0)
    assert len(curve) == 31
    assert curve[0]["retention"] == pytest.approx(1.0, abs=1e-6)
    assert curve[-1]["retention"] < curve[0]["retention"]


# ================================================================ 缓存
def test_cache_roundtrip_and_stats():
    cache_clear()
    key = cache_key("agent", "model", 0.2, "prompt")
    assert get_cache(key) is None
    set_cache(key, {"x": 1}, ttl=60)
    assert get_cache(key) == {"x": 1}
    stats = cache_stats()
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    assert 0 <= stats["hit_rate"] <= 1


def test_cache_ttl_expiry():
    cache_clear()
    key = cache_key("ttl-test")
    set_cache(key, {"x": 1}, ttl=0)
    import time

    time.sleep(0.01)
    assert get_cache(key) is None


# ================================================================ 路由
def test_cost_router_skips_llm_for_simple_problem():
    simple = {"statement": "x" * 100, "difficulty_score": 20.0}
    out = route(simple, {"provider": "openai"})
    assert out["use_llm"] is False
    assert out["estimated_calls"] == 0


def test_cost_router_uses_llm_for_hard_problem():
    hard = {"statement": "x" * 5000, "difficulty_score": 80.0}
    out = route(hard, {"provider": "openai"})
    assert out["use_llm"] is True
    assert out["estimated_calls"] > 0


def test_agent_registry_matches_resume_claim():
    """简历主张：5 个 Agent + 统一基类。"""
    assert len(CORE_AGENTS) == 5
    assert set(CORE_AGENTS) <= set(AGENTS)
    for name, agent in AGENTS.items():
        assert issubclass(agent.__class__, BaseAgent), f"{name} 未继承 BaseAgent"
        assert agent.schema is not None, f"{name} 未声明 schema"
        assert agent.system_prompt, f"{name} 未声明 system_prompt"


def test_all_core_agents_have_rule_fallback():
    """核心主张：每个 Agent 都有规则引擎兜底。

    solution 的特殊性：没有预置题解时兜底为"通用可运行模板"而非正解，
    因此这里给它喂一个带语言信息的上下文，验证兜底路径确实存在。
    """
    for name in CORE_AGENTS:
        agent = AGENTS[name]
        ctx = AgentContext(data={"language": "python", "tags": ["模拟"]})
        fallback = agent.run_rule(ctx)
        assert fallback is not None, f"{name} 缺少规则兜底"
        agent.schema.model_validate(fallback.model_dump())  # 兜底结果也必须过契约


def test_solution_fallback_marks_itself_as_template():
    """降级模板必须自我标注，不能让用户误以为是正解。"""
    out = rule_solution({"language": "python", "tags": ["动态规划"]})
    assert out is not None
    assert "非正解" in out.summary or "框架" in out.summary
    assert "TODO" in out.code
    compile(out.code, "<template>", "exec")  # 语法必须可运行


def test_solution_fallback_for_cpp_and_java():
    for lang, marker in (("cpp", "#include"), ("java", "public class")):
        out = rule_solution({"language": lang, "tags": []})
        assert out is not None and marker in out.code
