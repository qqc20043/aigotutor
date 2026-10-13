"""多 Agent 链路编排：主链路不中断。

一次"刷题闭环"的链路：
    submit -> 评测(judge) -> 掌握度(mastery) -> 复习调度(FSRS)
                              └-> 推荐(recommend) -> 路线图(roadmap)（按需）

链路容错规则：
  * 每个 Agent 独立降级，任何一个失败都不抛到主链路；
  * 降级结果同样落库，并标记 agent_runs.degraded = True；
  * 链路整体成功率 = 1 - (所有 Agent 都彻底失败的组合)，实践中接近 100%。
"""

from __future__ import annotations

from typing import Any

from ..agents import get_agent
from ..config import settings
from ..schemas import SCHEMA_BY_AGENT
from .agent import AgentContext, AgentResult
from .rules import (
    quality_grade_of,
    rule_difficulty,
    rule_hint,
    rule_judge,
    rule_mastery,
    rule_quality,
    rule_recommend,
    rule_review_priority,
    rule_roadmap,
)

# 这些键属于 AgentContext 自身的字段，不能落进 data，否则会被 Agent 当成业务数据
_RESERVED_KEYS = ("user_id", "provider", "model", "base_url", "api_key", "temperature")


def _ctx(
    user_id: str,
    config: dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    task_id: str = "",
    use_cache: bool = True,
    memory: str = "",
) -> AgentContext:
    """构造 AgentContext。

    payload 用显式 dict 而不是 **kwargs：调用方会把整个 submission / problem
    字典传进来，其中常含 user_id / provider / model 等同名字段；用 ** 展开会在
    调用时立刻抛 "got multiple values for argument 'user_id'"，函数体内的 pop
    根本来不及执行。这里统一剥离保留键，其余进 data。

    memory 是**可选**的记忆清单：由调用方（API 层）从 core.memory 预聚合后传入。
    pipeline 层不主动查库 —— 它应当保持"纯编排"职责，数据库访问留在 service
    与 API 层，这样单测可以直接构造 ctx 而不需要数据库。
    """
    data = dict(payload or {})
    for reserved in _RESERVED_KEYS:
        data.pop(reserved, None)
    return AgentContext(
        user_id=user_id,
        provider=config.get("provider", "mock"),
        model=config.get("model", "mock-1"),
        base_url=config.get("base_url", ""),
        api_key=config.get("api_key", ""),
        temperature=float(config.get("temperature", 0.2)),
        task_id=task_id,
        use_cache=use_cache,
        memory=memory,
        data=data,
    )


async def run_agent(name: str, ctx: AgentContext) -> AgentResult:
    agent = get_agent(name)
    return await agent.run(ctx)


# ------------------------------------------------------------------ 单 Agent
async def annotate_problem(
    problem: dict[str, Any], config: dict[str, Any], user_id: str = ""
) -> dict[str, Any]:
    """两阶段难度标注：规则引擎给区间 -> LLM 在区间内校准。"""
    ctx = _ctx(user_id, config, problem)
    rule_out = rule_difficulty(ctx.data)
    ctx.data["rule_out"] = rule_out
    # 缓存盐：规则结果相同才允许命中缓存，避免跨难度档位串味
    ctx.data["cache_salt"] = f"{rule_out.difficulty}:{rule_out.difficulty_score}"

    result = await run_agent("difficulty", ctx)
    out = result.data or rule_out
    return {
        "difficulty": out.difficulty,
        "difficulty_score": out.difficulty_score,
        "tags": out.tags,
        "difficulty_source": "rule" if result.degraded else "llm",
        "run": result.to_dict(),
        "rule_baseline": rule_out.model_dump(),
    }


async def recommend_next(
    user_id: str,
    config: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    mastery: dict[str, float],
    recent_problem_ids: list[str] | None = None,
    target_score: float = 55.0,
    weak_tags: list[str] | None = None,
    user_level: int = 3,
    memory: str = "",
) -> dict[str, Any]:
    ctx = _ctx(
        user_id,
        config,
        {
            "candidates": candidates,
            "mastery": mastery,
            "recent_problem_ids": recent_problem_ids or [],
            "target_score": target_score,
            "weak_tags": weak_tags or [],
            "user_level": user_level,
        },
        memory=memory,
    )
    result = await run_agent("recommend", ctx)
    out = result.data or rule_recommend(ctx.data)
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    return payload


async def solve_problem(
    user_id: str, config: dict[str, Any], problem: dict[str, Any], language: str = "python"
) -> dict[str, Any]:
    ctx = _ctx(user_id, config, {**problem, "language": language})
    result = await run_agent("solution", ctx)
    return result.to_dict()


async def judge_submission(
    user_id: str, config: dict[str, Any], submission: dict[str, Any], problem: dict[str, Any]
) -> dict[str, Any]:
    ctx = _ctx(
        user_id,
        config,
        {
            "title": problem.get("title", ""),
            "statement": problem.get("statement", ""),
            "tags": problem.get("tags", []),
            **submission,
        },
    )
    result = await run_agent("judge", ctx)
    out = result.data or rule_judge(ctx.data)
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    return payload


async def analyze_mastery(
    user_id: str, config: dict[str, Any], *, tag_stats: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    baseline = rule_mastery({"tag_stats": tag_stats, **extra})
    ctx = _ctx(
        user_id,
        config,
        {
            "tag_stats": tag_stats,
            "rule_baseline": baseline.model_dump(),
            **extra,
        },
    )
    # 规则基线分数进缓存盐：分数变了说明数据变了，必须重算
    ctx.data["cache_salt"] = str(round(baseline.score, 1))
    result = await run_agent("mastery", ctx)
    out = result.data or baseline
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    payload["rule_baseline"] = baseline.model_dump()
    return payload


async def build_roadmap(
    user_id: str,
    config: dict[str, Any],
    *,
    tags: list[dict[str, Any]],
    mastery: dict[str, float],
    **extra: Any,
) -> dict[str, Any]:
    baseline = rule_roadmap({"tags": tags, "mastery": mastery, **extra})
    ctx = _ctx(
        user_id,
        config,
        {
            "tags": tags,
            "mastery": mastery,
            "rule_baseline": baseline.model_dump(),
            **extra,
        },
    )
    result = await run_agent("roadmap", ctx)
    out = result.data or baseline
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    payload["rule_baseline"] = baseline.model_dump()
    return payload


async def generate_hint(
    user_id: str,
    config: dict[str, Any],
    *,
    problem: dict[str, Any],
    mastery_score: float,
    attempts: int = 0,
    current_code: str = "",
    verdict: str = "",
    memory: str = "",
) -> dict[str, Any]:
    """渐进式提示：强度由规则层按掌握度算定，模型只负责在该强度下写内容。

    强度算定放在这里（而不是让 Agent 自己判断）是为了让它**可测试**：
    pipeline 层注入 strength / strength_reason，Agent 与 Mock 都只能遵从。
    """
    from ..core.rules import hint_strength_for

    strength, reason = hint_strength_for(mastery_score, attempts)
    ctx = _ctx(
        user_id,
        config,
        {
            "title": problem.get("title", ""),
            "statement": problem.get("statement", ""),
            "tags": problem.get("tags", []),
            "difficulty": problem.get("difficulty", ""),
            "language": problem.get("language", "python"),
            "mastery_score": mastery_score,
            "attempts": attempts,
            "current_code": current_code,
            "verdict": verdict,
            # 强度已算定：写进 prompt 供模型与 mock 遵从
            "strength": strength,
            "strength_reason": reason,
        },
        memory=memory,
    )
    # 缓存盐带强度与掌握度：同一题在不同掌握度下必须给出不同强度的提示
    ctx.data["cache_salt"] = f"{strength}:{round(mastery_score, 1)}:{attempts}"
    result = await run_agent("hint", ctx)
    out = result.data or rule_hint(ctx.data)
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    return payload


async def prioritize_review(
    user_id: str,
    config: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    time_budget_minutes: int = 0,
    daily_goal: int = 3,
    weak_tags: list[str] | None = None,
    memory: str = "",
) -> dict[str, Any]:
    """复习队列优先：在 FSRS 已筛出的到期项里决定"先做哪个"并解释原因。"""
    baseline = rule_review_priority(
        {
            "candidates": candidates,
            "time_budget_minutes": time_budget_minutes,
            "daily_goal": daily_goal,
        }
    )
    ctx = _ctx(
        user_id,
        config,
        {
            "candidates": candidates,
            "time_budget_minutes": time_budget_minutes,
            "daily_goal": daily_goal,
            "weak_tags": weak_tags or [],
            "rule_baseline_order": [i.problem_id for i in baseline.ordered],
        },
        memory=memory,
    )
    # 缓存盐用候选指纹：候选集变了必须重排
    ctx.data["cache_salt"] = str(
        sorted(str(c.get("problem_id") or c.get("id") or "") for c in candidates)
    )[:200]
    result = await run_agent("review", ctx)
    out = result.data or baseline
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    payload["rule_baseline"] = baseline.model_dump()
    return payload


async def score_quality(
    user_id: str,
    config: dict[str, Any],
    *,
    problem: dict[str, Any],
    code: str,
    language: str = "python",
    verdict: str = "AC",
    memory: str = "",
) -> dict[str, Any]:
    """代码质量两阶段评分：规则给窗口 -> LLM 在窗口内做语义修正。

    与 annotate_problem 是同一套机制（规则基线 + 窗口夹紧），只是对象从
    "题面难度"换成了"代码质量"。这里显式返回 rule_baseline 与窗口，
    让前端能展示"规则怎么看"与"模型修正后怎么看"的差异。
    """
    from ..schemas import QUALITY_RADIUS
    from .rules import quality_window

    baseline, metrics = quality_window(code)
    lo = max(0.0, baseline - QUALITY_RADIUS)
    hi = min(100.0, baseline + QUALITY_RADIUS)

    ctx = _ctx(
        user_id,
        config,
        {
            "title": problem.get("title", ""),
            "statement": problem.get("statement", ""),
            "tags": problem.get("tags", []),
            "language": language,
            "code": code,
            "verdict": verdict,
        },
        memory=memory,
    )
    # 缓存盐：同一份代码在同一个规则基线下的评分才可复用
    ctx.data["cache_salt"] = f"{round(baseline, 1)}:{verdict}"

    result = await run_agent("quality", ctx)
    out = result.data or rule_quality(ctx.data)
    payload = result.to_dict()
    payload["data"] = out.model_dump()
    payload["rule_baseline"] = {
        "score": baseline,
        "grade": quality_grade_of(baseline),
        "allowed_range": [round(lo, 2), round(hi, 2)],
        "metrics": {k: v for k, v in metrics.items() if not k.startswith("_")},
    }
    payload["stage"] = "rule" if result.degraded else "llm"
    return payload


# ------------------------------------------------------------- 完整刷题闭环
async def practice_pipeline(
    task,
    *,
    user_id: str,
    config: dict[str, Any],
    submission: dict[str, Any],
    problem: dict[str, Any],
    tag_stats: dict[str, Any],
    tags: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    mastery_scores: dict[str, float] | None = None,
    weeks: int = 6,
    on_result=None,
) -> dict[str, Any]:
    """提交后的一条完整链路，逐步上报进度，任何单点失败都不中断。

    实现已迁移到 LangGraph（见 core/graph.py）：链路形状、条件分支与状态
    合并都由 StateGraph 表达。本函数保留原签名与返回结构，调用方无感。

    返回结构：
        {"agents": {name: result_dict}, "degraded": [...], "failed": [...],
         "degraded_rate": float, "failure_rate": float, "cache": {...},
         "elapsed_ms": int, "agent_count": int, "engine": "langgraph"}
    """
    from .graph import run_practice_graph

    return await run_practice_graph(
        task,
        user_id=user_id,
        config=config,
        submission=submission,
        problem=problem,
        tag_stats=tag_stats,
        tags=tags,
        candidates=candidates,
        mastery_scores=mastery_scores,
        weeks=weeks,
        on_result=on_result,
    )


# ------------------------------------------------------------------ 路由决策
def route(problem: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """成本路由：简单题走规则引擎，复杂题才上模型。

    规则：
      * provider == mock -> 全链路走 mock（离线）
      * 题面 < ROUTER_SIMPLE_TOKEN_LIMIT 且 难度分 < 45 -> 跳过 LLM，纯规则
      * 其余 -> LLM + 规则降级兜底
    """
    provider = config.get("provider", "mock")
    statement = problem.get("statement", "") or ""
    diff = float(problem.get("difficulty_score", 50.0))
    use_llm = True
    reason = "复杂题：使用模型 + 规则兜底"
    if provider == "mock":
        reason = "mock 模式：离线确定性输出"
    elif len(statement) < settings.ROUTER_SIMPLE_TOKEN_LIMIT and diff < 45.0:
        use_llm = False
        reason = "简单题：跳过模型，直接规则引擎，成本为 0"

    return {
        "use_llm": use_llm,
        "provider": provider,
        "reason": reason,
        "statement_len": len(statement),
        "difficulty_score": diff,
        "estimated_calls": 0 if not use_llm else 3,
    }


__all__ = [
    "SCHEMA_BY_AGENT",
    "analyze_mastery",
    "annotate_problem",
    "build_roadmap",
    "generate_hint",
    "judge_submission",
    "practice_pipeline",
    "prioritize_review",
    "recommend_next",
    "route",
    "run_agent",
    "score_quality",
    "solve_problem",
]
