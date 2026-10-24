"""AIgoTutor MCP Server：把教练能力暴露为 MCP 工具（stdio 本地宿主）。

设计文档：docs/PRD-2026-10-24-MCP服务与模拟面试.md（Feature A）。

架构要点：
  * 独立进程与 Web 共享同一 SQLite 库（WAL 支持多进程读写），全部逻辑
    复用 service/pipeline 层 —— 两条路径行为一致，不出现"API 一个口径、
    MCP 另一个口径"；
  * 身份：AIGOTUTOR_MCP_USER 指定用户（默认 demo），启动时校验存在，
    不存在则启动失败并给出可读指引（明确报错优于静默用错人数据）；
  * 治理不变：提示强度仍由规则层按掌握度算定（generate_hint），宿主 AI
    无权升降档 —— MCP 只是新的调用通道，不改变策略归属。

运行（Claude Desktop / Cursor 等 stdio 宿主配置）：
    python -m app.mcp_server
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import User, UserModelConfig
from .security import decrypt_secret

_MCP_USER_ID: str | None = None


async def _resolve_user() -> str:
    """启动时把 AIGOTUTOR_MCP_USER 解析成 user_id（全局缓存）。"""
    global _MCP_USER_ID
    if _MCP_USER_ID:
        return _MCP_USER_ID
    username = os.environ.get("AIGOTUTOR_MCP_USER", "demo")
    async with session_scope() as db:
        row = (
            await db.execute(select(User.id).where(User.username == username))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(
            f"[mcp] AIGOTUTOR_MCP_USER 指定的用户 {username!r} 不存在。"
            f"请先在 Web 端注册，或改为已有用户名。"
        )
    _MCP_USER_ID = row
    return row


async def _llm_config(user_id: str) -> dict[str, Any]:
    """该用户的模型配置（与 deps.get_model_config 同语义，缺省回落全局默认）。"""
    async with session_scope() as db:
        cfg = (
            await db.execute(
                select(UserModelConfig).where(
                    UserModelConfig.user_id == user_id,
                    UserModelConfig.purpose == "default",
                    UserModelConfig.enabled == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if cfg is None:
            return {
                "provider": settings.DEFAULT_PROVIDER,
                "model": settings.DEFAULT_MODEL,
                "base_url": settings.DEFAULT_BASE_URL,
                "api_key": settings.DEFAULT_API_KEY,
                "temperature": 0.2,
            }
        return {
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "api_key": decrypt_secret(cfg.api_key_ref),
            "temperature": cfg.temperature,
        }


def _slim_problem(p: Any, tags: list[str]) -> dict[str, Any]:
    return {
        "problem_id": p.id,
        "title": p.title,
        "difficulty": p.difficulty,
        "difficulty_score": p.difficulty_score,
        "tags": tags,
        "time_limit_ms": p.time_limit_ms,
    }


async def _problem_with_tags(db, problem_id: str):
    from .services import get_problem, get_problem_tags

    p = await get_problem(db, problem_id)
    if p is None:
        return None, []
    tag_map = await get_problem_tags(db, [p.id])
    return p, [t["name"] for t in tag_map.get(p.id, [])]


# ------------------------------------------------------------------ 工具实现
async def _tool_next_problem(target_score: float | None) -> dict[str, Any]:
    from .core.pipeline import recommend_next
    from .services import get_problem, get_problem_tags, list_problems, mastery_scores_map

    user_id = await _resolve_user()
    cfg = await _llm_config(user_id)
    async with session_scope() as db:
        rows, _total = await list_problems(db, offset=0, limit=20)
        if not rows:
            return {"recommendation": None, "reason": "题库为空"}
        tag_map = await get_problem_tags(db, [p.id for p in rows])
        candidates = [
            {
                "id": p.id,
                "title": p.title,
                "difficulty": p.difficulty,
                "difficulty_score": p.difficulty_score,
                "tags": [t["name"] for t in tag_map.get(p.id, [])],
            }
            for p in rows
        ]
        mastery = await mastery_scores_map(db, user_id)
        result = await recommend_next(
            user_id,
            cfg,
            candidates=candidates,
            mastery=mastery,
            target_score=target_score if target_score is not None else 55.0,
        )
        data = result.get("data") or {}
        pid = str(data.get("problem_id", ""))
        p = await get_problem(db, pid) if pid else None
        return {
            "recommendation": _slim_problem(p, data.get("tags", [])) if p else None,
            "reason": data.get("reason", ""),
            "expected_gain": data.get("expected_gain", 0.0),
        }


async def _tool_get_hint(problem_id: str, attempts: int) -> dict[str, Any]:
    from .core.pipeline import generate_hint

    user_id = await _resolve_user()
    cfg = await _llm_config(user_id)
    async with session_scope() as db:
        p, tags = await _problem_with_tags(db, problem_id)
        if p is None:
            return {"error": f"题目不存在: {problem_id}"}
        from .services import mastery_scores_map, problem_attempt_stats

        mastery = await mastery_scores_map(db, user_id)
        weakest = min(float(mastery.get(t, 50.0)) for t in tags) if tags else 50.0
        stats = await problem_attempt_stats(db, user_id, problem_id)
        result = await generate_hint(
            user_id,
            cfg,
            problem={
                "title": p.title,
                "statement": p.statement,
                "tags": tags,
                "difficulty": p.difficulty,
            },
            mastery_score=weakest,
            attempts=attempts if attempts > 0 else stats["attempts"],
        )
        payload = {
            "strength": result["data"]["strength"],
            "hints": result["data"]["hints"],
            "key_insight": result["data"]["key_insight"],
            "next_step": result["data"]["next_step"],
            "strength_reason": result["data"]["strength_reason"],
            "degraded": bool(result.get("degraded")),
        }
        payload["note"] = "强度由掌握度规则层算定，不可要求升档或降档"
        return payload


async def _tool_similar(problem_id: str, limit: int) -> dict[str, Any]:
    from .core.similar import find_similar

    async with session_scope() as db:
        p, tags = await _problem_with_tags(db, problem_id)
        if p is None:
            return {"error": f"题目不存在: {problem_id}"}
        result = await find_similar(
            db,
            {
                "id": p.id,
                "title": p.title,
                "statement": p.statement,
                "tags": tags,
                "difficulty_score": p.difficulty_score,
            },
            limit=limit,
        )
        return {"items": [i.__dict__ for i in result["items"]], "retrieval": result["retrieval"]}


async def _tool_review_queue(time_budget_minutes: int) -> dict[str, Any]:
    from .core.pipeline import prioritize_review
    from .services import get_review_queue

    user_id = await _resolve_user()
    cfg = await _llm_config(user_id)
    async with session_scope() as db:
        items = await get_review_queue(db, user_id, due_only=True, limit=30)
        if not items:
            return {"items": [], "reason": "当前没有到期的复习项"}
        candidates = [
            {
                "problem_id": i["problem_id"],
                "title": i.get("title", ""),
                "difficulty": i.get("difficulty", ""),
                "tags": i.get("tags", []),
                "overdue_days": i.get("overdue_days", 0.0),
                "retrievability": i.get("retrievability"),
                "lapses": i.get("lapses", 0),
                "mastery_scores": i.get("mastery_scores", {}),
            }
            for i in items
        ]
        result = await prioritize_review(
            user_id, cfg, candidates=candidates, time_budget_minutes=time_budget_minutes
        )
        return {
            "items": [o.model_dump() for o in result["data"]["ordered"]],
            "strategy": result["data"].get("strategy", ""),
        }


async def _tool_mastery() -> dict[str, Any]:
    from .core.pipeline import analyze_mastery
    from .services import build_tag_stats

    user_id = await _resolve_user()
    cfg = await _llm_config(user_id)
    async with session_scope() as db:
        tag_stats = await build_tag_stats(db, user_id, window_days=30)
        if not tag_stats:
            return {"score": 50.0, "level": "L2", "note": "样本不足：先做几道题建立基线"}
        result = await analyze_mastery(user_id, cfg, tag_stats=tag_stats)
        return result["data"]


async def _tool_roadmap(weeks: int) -> dict[str, Any]:
    from .core.pipeline import build_roadmap
    from .services import get_mastery, list_tags

    user_id = await _resolve_user()
    cfg = await _llm_config(user_id)
    async with session_scope() as db:
        tag_rows = await list_tags(db)
        tags = [
            {"id": t.id, "name": t.name, "prereq_ids": getattr(t, "prereq_ids", "[]") or "[]"}
            for t in tag_rows
        ]
        mastery = {row["tag"]: float(row["score"]) for row in await get_mastery(db, user_id)}
        result = await build_roadmap(user_id, cfg, tags=tags, mastery=mastery, weeks=weeks)
        return result["data"]


async def _tool_search(keyword: str, difficulty: str | None, limit: int) -> dict[str, Any]:
    from .services import get_problem_tags, list_problems

    async with session_scope() as db:
        rows, _total = await list_problems(db, difficulty=difficulty, limit=min(limit * 3, 50))
        tag_map = await get_problem_tags(db, [p.id for p in rows])
        out = []
        for p in rows:
            tags = [t["name"] for t in tag_map.get(p.id, [])]
            hay = f"{p.title}{' '.join(tags)}"
            if keyword and keyword.lower() not in hay.lower():
                continue
            out.append(_slim_problem(p, tags))
            if len(out) >= limit:
                break
        return {"items": out, "total_matched": len(out)}


async def _tool_get_problem(problem_id: str) -> dict[str, Any]:
    async with session_scope() as db:
        p, tags = await _problem_with_tags(db, problem_id)
        if p is None:
            return {"error": f"题目不存在: {problem_id}"}
        return {
            "problem_id": p.id,
            "title": p.title,
            "statement": p.statement,
            "input_desc": p.input_desc,
            "output_desc": p.output_desc,
            "samples": json.loads(p.samples or "[]"),
            "constraints": p.constraints,
            "tags": tags,
            "difficulty": p.difficulty,
            "time_limit_ms": p.time_limit_ms,
            "memory_limit_mb": p.memory_limit_mb,
            "note": "隐藏测试用例不通过 MCP 暴露",
        }


async def _tool_wrong_book(limit: int) -> dict[str, Any]:
    from .services import cluster_errors, list_wrong_book

    user_id = await _resolve_user()
    async with session_scope() as db:
        items = await list_wrong_book(db, user_id, limit=limit)
        clusters = await cluster_errors(db, user_id)
        return {
            "items": [
                {
                    "problem_id": i["problem_id"],
                    "title": i.get("title", ""),
                    "last_verdict": i.get("last_verdict", ""),
                    "fail_count": i.get("fail_count", 0),
                }
                for i in items
            ],
            "error_clusters": clusters,
        }


async def _tool_submit(problem_id: str, language: str, code: str) -> dict[str, Any]:
    from .judge_service import judge_submission_in_sandbox
    from .services import create_submission, get_problem

    user_id = await _resolve_user()
    async with session_scope() as db:
        p = await get_problem(db, problem_id)
        if p is None:
            return {"error": f"题目不存在: {problem_id}"}
        fields = await judge_submission_in_sandbox(db, p, language, code)
        sub = await create_submission(
            db,
            {
                "user_id": user_id,
                "problem_id": problem_id,
                "language": language,
                "code": code,
                "verdict": fields["verdict"],
                "passed_cases": fields["passed_cases"],
                "total_cases": fields["total_cases"],
                "runtime_ms": fields["runtime_ms"],
                "memory_kb": fields["memory_kb"],
                "failed_case": fields.get("failed_case", ""),
                "judge_detail": fields.get("judge_detail", {}),
            },
        )
        return {
            "submission_id": sub.id,
            "verdict": fields["verdict"],
            "passed": fields["passed_cases"],
            "total": fields["total_cases"],
            "time_ms": fields["runtime_ms"],
            "message": (fields.get("judge_detail") or {}).get("message", ""),
            "failed_case": fields.get("failed_case") or None,
        }


async def _tool_mark_review(problem_id: str, rating: int) -> dict[str, Any]:
    from .services import upsert_review_item

    user_id = await _resolve_user()
    if not 1 <= rating <= 4:
        return {"error": "rating 取值 1-4（1=完全忘了 … 4=轻松记住）"}
    async with session_scope() as db:
        plan = await upsert_review_item(db, user_id, problem_id, rating)
        due = plan.get("due_at")
        return {
            "ok": True,
            "next_interval_days": plan["interval_days"],
            "next_due_at": due.isoformat() if hasattr(due, "isoformat") else str(due or ""),
        }


# ------------------------------------------------------------------ 服务器组装
def build_mcp_server():  # pragma: no cover - 组装函数，工具逻辑已单测覆盖
    """构建 MCPServer 实例（独立函数便于测试注入与宿主启动）。"""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(
        "aigotutor",
        instructions=(
            "AIgoTutor 算法刷题教练。可用工具覆盖：推荐下一题、渐进式解题提示"
            "（强度由系统按掌握度决定，不要尝试要求更强提示）、同型题检索、"
            "复习队列、掌握度画像、学习路线、题库检索、提交判题、FSRS 复习打分。"
            "推荐先用 aigotutor_mastery_profile 了解用户水平，再决定教学节奏。"
        ),
    )

    @mcp.tool()
    async def aigotutor_next_problem(target_score: float | None = None) -> dict:
        """推荐下一道最该做的题（含掌握度依据与预期提升）。"""
        return await _tool_next_problem(target_score)

    @mcp.tool()
    async def aigotutor_get_hint(problem_id: str, attempts: int = 0) -> dict:
        """获取解题提示。强度（direction/key_observation/pseudocode）由系统按
        该知识点掌握度算定；attempts 不传则使用用户在该题的真实提交次数。"""
        return await _tool_get_hint(problem_id, attempts)

    @mcp.tool()
    async def aigotutor_similar_problems(problem_id: str, limit: int = 5) -> dict:
        """检索同型题（举一反三）：返回带共享标签与难度相近度的可解释结果。"""
        return await _tool_similar(problem_id, max(1, min(limit, 10)))

    @mcp.tool()
    async def aigotutor_review_queue(time_budget_minutes: int = 30) -> dict:
        """今日到期复习队列（FSRS 调度 + 优先级排序与理由），遵守时间预算。"""
        return await _tool_review_queue(max(0, min(time_budget_minutes, 240)))

    @mcp.tool()
    async def aigotutor_mastery_profile() -> dict:
        """用户的知识点掌握度画像：0-100 分、五级、强弱项与置信度。"""
        return await _tool_mastery()

    @mcp.tool()
    async def aigotutor_roadmap(weeks: int = 6) -> dict:
        """分阶段学习路线图（按先修关系拓扑分层 + 掌握度缺口）。"""
        return await _tool_roadmap(max(1, min(weeks, 26)))

    @mcp.tool()
    async def aigotutor_search_problems(
        keyword: str, difficulty: str | None = None, limit: int = 10
    ) -> dict:
        """按关键词/难度检索题库（标题与标签匹配）。"""
        return await _tool_search(keyword, difficulty, max(1, min(limit, 20)))

    @mcp.tool()
    async def aigotutor_get_problem(problem_id: str) -> dict:
        """获取题目详情（题面/样例/约束；隐藏测试用例不暴露）。"""
        return await _tool_get_problem(problem_id)

    @mcp.tool()
    async def aigotutor_wrong_book(limit: int = 20) -> dict:
        """错题本与易错题聚类（按错因）。"""
        return await _tool_wrong_book(max(1, min(limit, 50)))

    @mcp.tool()
    async def aigotutor_submit_solution(problem_id: str, language: str, code: str) -> dict:
        """提交代码并在沙箱真实判题（python/cpp/java），返回 verdict 与失败用例摘要。"""
        return await _tool_submit(problem_id, language, code)

    @mcp.tool()
    async def aigotutor_mark_review(problem_id: str, rating: int) -> dict:
        """标记复习结果（FSRS）：rating 1-4（1=完全忘了，4=轻松记住），
        返回下次复习间隔。"""
        return await _tool_mark_review(problem_id, rating)

    return mcp


def main() -> None:  # pragma: no cover - stdio 宿主入口
    import asyncio

    mcp = build_mcp_server()
    # 启动即校验身份：用户不存在时快速失败并给出可读指引
    asyncio.run(_resolve_user())
    mcp.run()


if __name__ == "__main__":
    main()
