"""LangGraph 编排：用 StateGraph 表达刷题闭环的依赖与条件分支。

对应简历主张："多 Agent 协作：按职责划分……统一基于 LangGraph 编排"。

为什么用 StateGraph 而不是手写顺序调用（面试会被问到）：

  1. **依赖关系显式化**：谁依赖谁、哪些节点可以并行，写在图结构里而不是
     藏在函数调用顺序里。看 add_edge 一眼就知道链路形状；
  2. **条件分支是一等公民**：本链路里"有候选才推荐"、"有标签才画路线图"
     是真实的条件逻辑。手写实现是一串 if，图实现是 add_conditional_edges ——
     后者在加节点时不需要改动控制流代码；
  3. **状态收敛**：每个节点只返回自己产出的那部分，由图的 reducer 负责合并
     （见 PipelineState 的 Annotated）。手写实现要自己维护累加逻辑。

与降级机制的关系（关键）：
    **降级仍然在 Agent 层完成**（BaseAgent.run 内部重试→校验→规则兜底），
    图这一层只负责"编排"。这是有意的分层 —— 降级是"单个 Agent 如何自救"，
    编排是"整体流程如何推进"，两者混在一起会让两边都难以独立测试。

    图节点里另有一层 try/except 兜底：不是替代 Agent 的降级，而是防止
    "Agent 层意外抛出"（比如 bug）把整张图打断 —— 双保险。
"""

from __future__ import annotations

import asyncio
import operator
from contextlib import suppress
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents import CORE_AGENTS
from .tasks import _advance, _emit


class PipelineState(TypedDict, total=False):
    """图状态。

    输入字段由调用方注入；输出字段用 Annotated 声明累加语义：
      * agents   用 operator.or_  —— 各节点贡献自己的 {name: result}
      * degraded 用 operator.add  —— 各节点追加自己降级的 Agent 名
      * failed   同上

    为什么必须用 reducer：LangGraph 默认是"后写覆盖"。若用默认语义，
    第二个节点返回的 degraded 会覆盖第一个的，导致汇总统计丢失。
    这类 bug 不会报错、只会让数字变少，很难察觉 —— 因此显式声明 reducer。
    """

    # ---- 输入 ----
    submission: dict[str, Any]
    problem: dict[str, Any]
    tag_stats: dict[str, Any]
    candidates: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    mastery_scores: dict[str, float]
    weeks: int

    # ---- 输出（累加语义）----
    agents: Annotated[dict[str, Any], operator.or_]
    degraded: Annotated[list[str], operator.add]
    failed: Annotated[list[str], operator.add]


def build_practice_graph(
    task: Any,
    *,
    user_id: str,
    config: dict[str, Any],
    on_result: Any = None,
) -> Any:
    """构建"提交后刷题闭环"图并编译。

    链路形状：
        START -> judge -> mastery ─┬─> recommend ─┬─> roadmap -> END
                                   │              └─> END
                                   └─> roadmap ────> END
                                   └─> END

    即：判错归因与学习状态是必经环节；推荐与路线图按数据可用性条件执行。
    """
    from .pipeline import (
        analyze_mastery,
        build_roadmap,
        judge_submission,
        recommend_next,
    )

    async def _step(name: str, progress: float, coro: Any) -> dict[str, Any]:
        """推进度 + 检查中断 + 执行 + 记录结果。异常一律不外抛。"""
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
        if on_result is not None:
            with suppress(Exception):  # 落库失败不能拖垮链路
                await on_result(name, result)
        return result

    def _collect(name: str, result: dict[str, Any]) -> dict[str, Any]:
        """把节点结果转成状态增量（agents/degraded/failed 三件套）。"""
        out: dict[str, Any] = {"agents": {name: result}}
        status = result.get("status")
        if status == "degraded":
            out["degraded"] = [name]
        elif status == "failed":
            out["failed"] = [name]
        return out

    # ------------------------------------------------------------ 节点定义
    async def node_judge(state: PipelineState) -> dict[str, Any]:
        result = await _step(
            "judge",
            0.15,
            judge_submission(user_id, config, state["submission"], state["problem"]),
        )
        return _collect("judge", result)

    async def node_mastery(state: PipelineState) -> dict[str, Any]:
        sub = state["submission"]
        result = await _step(
            "mastery",
            0.40,
            analyze_mastery(
                user_id,
                config,
                tag_stats=state["tag_stats"],
                total_submissions=sub.get("total_submissions", 0),
                ac_count=sub.get("ac_count", 0),
                avg_difficulty_score=state["problem"].get("difficulty_score", 0.0),
                recent_verdicts=sub.get("recent_verdicts", []),
            ),
        )
        return _collect("mastery", result)

    async def node_recommend(state: PipelineState) -> dict[str, Any]:
        sub = state["submission"]
        result = await _step(
            "recommend",
            0.65,
            recommend_next(
                user_id,
                config,
                candidates=state["candidates"],
                mastery=state.get("mastery_scores") or {},
                recent_problem_ids=sub.get("recent_problem_ids", []),
            ),
        )
        return _collect("recommend", result)

    async def node_roadmap(state: PipelineState) -> dict[str, Any]:
        result = await _step(
            "roadmap",
            0.88,
            build_roadmap(
                user_id,
                config,
                tags=state["tags"],
                mastery=state.get("mastery_scores") or {},
                weeks=state.get("weeks", 6),
            ),
        )
        return _collect("roadmap", result)

    # ------------------------------------------------------------ 条件路由
    def after_mastery(state: PipelineState) -> str:
        """掌握度之后：有候选就推荐，否则直接看能否画路线图。"""
        if state.get("candidates"):
            return "recommend"
        if state.get("tags"):
            return "roadmap"
        return END

    def after_recommend(state: PipelineState) -> str:
        return "roadmap" if state.get("tags") else END

    # ------------------------------------------------------------ 组装图
    graph = StateGraph(PipelineState)
    graph.add_node("judge", node_judge)
    graph.add_node("mastery", node_mastery)
    graph.add_node("recommend", node_recommend)
    graph.add_node("roadmap", node_roadmap)

    graph.add_edge(START, "judge")
    graph.add_edge("judge", "mastery")
    graph.add_conditional_edges(
        "mastery", after_mastery, {"recommend": "recommend", "roadmap": "roadmap", END: END}
    )
    graph.add_conditional_edges("recommend", after_recommend, {"roadmap": "roadmap", END: END})
    graph.add_edge("roadmap", END)

    return graph.compile()


async def run_practice_graph(
    task: Any,
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
    on_result: Any = None,
) -> dict[str, Any]:
    """执行图并汇总结果（返回结构与手写版完全一致，调用方无感）。"""
    import time

    from .cache import cache_stats

    started = time.perf_counter()
    graph = build_practice_graph(task, user_id=user_id, config=config, on_result=on_result)

    initial: PipelineState = {
        "submission": submission,
        "problem": problem,
        "tag_stats": tag_stats,
        "candidates": list(candidates or []),
        "tags": list(tags or []),
        "mastery_scores": dict(mastery_scores or {}),
        "weeks": weeks,
        "agents": {},
        "degraded": [],
        "failed": [],
    }

    final = await graph.ainvoke(initial)

    _advance(task, 1.0, "finished")
    agents = final.get("agents") or {}
    degraded = final.get("degraded") or []
    failed = final.get("failed") or []
    summary = {
        "agents": agents,
        "degraded": degraded,
        "failed": failed,
        "degraded_rate": round(len(degraded) / max(1, len(agents)), 4),
        "failure_rate": round(len(failed) / max(1, len(agents)), 4),
        "cache": cache_stats(),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "agent_count": len(agents),
        "core_agent_total": len(CORE_AGENTS),
        "engine": "langgraph",
    }
    _emit(task, "summary", summary)
    return summary
