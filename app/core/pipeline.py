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

import asyncio
import time
from contextlib import suppress
from typing import Any

from ..agents import CORE_AGENTS, get_agent
from ..config import settings
from ..schemas import SCHEMA_BY_AGENT
from .agent import AgentContext, AgentResult
from .cache import cache_stats
from .rules import rule_difficulty, rule_judge, rule_mastery, rule_recommend, rule_roadmap

# 这些键属于 AgentContext 自身的字段，不能落进 data，否则会被 Agent 当成业务数据
_RESERVED_KEYS = ("user_id", "provider", "model", "base_url", "api_key", "temperature")


def _ctx(
    user_id: str,
    config: dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    task_id: str = "",
    use_cache: bool = True,
) -> AgentContext:
    """构造 AgentContext。

    payload 用显式 dict 而不是 **kwargs：调用方会把整个 submission / problem
    字典传进来，其中常含 user_id / provider / model 等同名字段；用 ** 展开会在
    调用时立刻抛 "got multiple values for argument 'user_id'"，函数体内的 pop
    根本来不及执行。这里统一剥离保留键，其余进 data。
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

    返回结构：
        {"agents": {name: result_dict}, "degraded": [...], "failed": [...],
         "degraded_rate": float, "failure_rate": float, "cache": {...},
         "elapsed_ms": int, "agent_count": int}
    """
    from .tasks import _advance, _emit

    started = time.perf_counter()
    agents: dict[str, Any] = {}
    degraded: list[str] = []
    failed: list[str] = []

    async def step(name: str, progress: float, coro):
        """推进度 + 检查中断 + 记录结果；异常一律不向外抛。"""
        _advance(task, progress, name)
        if getattr(task, "cancel_requested", False):
            raise asyncio.CancelledError()
        try:
            result = await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 双保险：Agent 层已兜底，这里再兜一次
            result = {
                "agent": name,
                "status": "failed",
                "degraded": True,
                "confidence": 0.0,
                "reason": f"{type(e).__name__}: {e}",
                "data": None,
                "tokens": 0,
                "latency_ms": 0,
            }
        agents[name] = result
        if result.get("status") == "degraded":
            degraded.append(name)
        elif result.get("status") == "failed":
            failed.append(name)
        if on_result is not None:
            with suppress(Exception):  # 落库失败不能拖垮链路
                await on_result(name, result)
        return result

    # 1) 判错归因
    await step("judge", 0.15, judge_submission(user_id, config, submission, problem))
    # 2) 学习状态
    await step(
        "mastery",
        0.40,
        analyze_mastery(
            user_id,
            config,
            tag_stats=tag_stats,
            total_submissions=submission.get("total_submissions", 0),
            ac_count=submission.get("ac_count", 0),
            avg_difficulty_score=problem.get("difficulty_score", 0.0),
            recent_verdicts=submission.get("recent_verdicts", []),
        ),
    )
    # 3) 下一题推荐
    if candidates:
        await step(
            "recommend",
            0.65,
            recommend_next(
                user_id,
                config,
                candidates=candidates,
                mastery=mastery_scores or {},
                recent_problem_ids=submission.get("recent_problem_ids", []),
            ),
        )
    # 4) 路线图（按需）
    if tags:
        await step(
            "roadmap",
            0.88,
            build_roadmap(
                user_id,
                config,
                tags=tags,
                mastery=mastery_scores or {},
                weeks=weeks,
            ),
        )

    _advance(task, 1.0, "finished")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    summary = {
        "agents": agents,
        "degraded": degraded,
        "failed": failed,
        "degraded_rate": round(len(degraded) / max(1, len(agents)), 4),
        "failure_rate": round(len(failed) / max(1, len(agents)), 4),
        "cache": cache_stats(),
        "elapsed_ms": elapsed_ms,
        "agent_count": len(CORE_AGENTS),
    }
    _emit(task, "summary", summary)
    return summary


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
    "judge_submission",
    "practice_pipeline",
    "recommend_next",
    "route",
    "run_agent",
    "solve_problem",
]
