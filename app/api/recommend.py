"""④ 推荐与路线图 —— 3 个接口。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from ..core.memory import memory_block_for
from ..core.pipeline import build_roadmap, recommend_next
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import (
    get_mastery,
    get_problem_tags,
    get_review_queue,
    list_problems,
    list_tags,
    log_agent_run,
    mastery_scores_map,
    recent_submissions,
)
from .schemas import RecommendResult, RoadmapResult

router = APIRouter(prefix="/api/recommend", tags=["④ 推荐与路线图"])


@router.get("/next", response_model=RecommendResult)
async def recommend_next_api(
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    target_score: float = 55.0,
    limit: int = 20,
    exclude_recent: int = 20,
):
    """17/27 下一题推荐（掌握度缺口 × 难度贴合 × 遗忘紧迫度）。"""
    problems, _ = await list_problems(db, offset=0, limit=max(1, min(limit, 50)))
    tag_map = await get_problem_tags(db, [p.id for p in problems])
    recent = await recent_submissions(db, user.id, limit=max(1, exclude_recent))
    recent_ids = list(dict.fromkeys(s.problem_id for s in recent))

    # 复习队列里逾期的题优先级更高，这里把 due 信息塞进候选。
    # overdue 直接用队列里的量化值（整天）——内联重算连续值会让
    # recommend Agent 的缓存键每 ~15 分钟漂移一次（time_decay 同类根因）。
    queue = await get_review_queue(db, user.id, limit=200)
    due_map = {q["problem_id"]: (q["due_at"], q["overdue_days"]) for q in queue}

    candidates = []
    for p in problems:
        due_info = due_map.get(p.id)
        due = due_info[0] if due_info else None
        candidates.append(
            {
                "id": p.id,
                "title": p.title,
                "difficulty": p.difficulty,
                "difficulty_score": p.difficulty_score,
                "tags": [t["name"] for t in tag_map.get(p.id, [])],
                "due_at": due.isoformat() if isinstance(due, datetime) else None,
                "overdue_days": due_info[1] if due_info else 0.0,
            }
        )

    mastery = await mastery_scores_map(db, user.id)
    weak = [m["tag"] for m in (await get_mastery(db, user.id))[:3]]
    memory = await memory_block_for(db, user.id)
    result = await recommend_next(
        user.id,
        {**cfg, "timeout": 20},
        candidates=candidates,
        mastery=mastery,
        recent_problem_ids=recent_ids,
        target_score=target_score,
        weak_tags=weak,
        user_level=user.level,
        memory=memory,
    )
    await log_agent_run(db, {**result, "user_id": user.id, "agent": "recommend"})
    data = result.get("data") or {}
    return RecommendResult(
        problem_id=data.get("problem_id", ""),
        title=data.get("title", ""),
        difficulty=data.get("difficulty", "入门"),
        tags=data.get("tags", []),
        reason=data.get("reason", ""),
        expected_gain=float(data.get("expected_gain", 0.0)),
        degraded=bool(result.get("degraded", False)),
        provider=result.get("provider", ""),
    )


@router.get("/explain", response_model=dict)
async def recommend_explain(db: DbSession, user: UserDep):
    """18/27 推荐可解释性：给出打分因子明细（消融实验用）。"""
    problems, _ = await list_problems(db, offset=0, limit=20)
    tag_map = await get_problem_tags(db, [p.id for p in problems])
    mastery = await mastery_scores_map(db, user.id)
    recent = await recent_submissions(db, user.id, limit=20)
    recent_ids = {s.problem_id for s in recent}

    rows = []
    for p in problems:
        tags = [t["name"] for t in tag_map.get(p.id, [])]
        gap = sum(max(0.0, 55.0 - mastery.get(t, 50.0)) for t in tags) / max(1, len(tags))
        fit = 100.0 - abs(p.difficulty_score - 55.0)
        rows.append(
            {
                "problem_id": p.id,
                "title": p.title,
                "excluded_recent": p.id in recent_ids,
                "gap_score": round(gap, 2),
                "fit_score": round(fit, 2),
                "total": round(gap * 1.2 + fit * 0.8, 2),
                "tags": tags,
            }
        )
    rows.sort(key=lambda r: (r["excluded_recent"], -r["total"]))
    return {
        "factors": ["gap_score(掌握度缺口×1.2)", "fit_score(难度贴合×0.8)", "urgency(遗忘紧迫度)"],
        "recent_excluded": sorted(recent_ids),
        "ranking": rows,
    }


@router.get("/roadmap", response_model=RoadmapResult)
async def roadmap_api(
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    weeks: int = 6,
    target_level: str = "L4",
    daily_hours: float = 2.0,
):
    """19/27 分阶段学习路线图（按标签先修关系拓扑分层）。"""
    tags = await list_tags(db)
    mastery = await mastery_scores_map(db, user.id)
    profiles = await get_mastery(db, user.id)
    result = await build_roadmap(
        user.id,
        {**cfg, "timeout": 20},  # 概览页在线等：LLM 慢时 20s 内降级规则引擎
        tags=[
            {
                "id": t.id,
                "name": t.name,
                "prereq_ids": t.prereq_ids,
                "category": t.category,
            }
            for t in tags
        ],
        mastery=mastery,
        weeks=weeks,
        target_level=target_level,
        daily_hours=daily_hours,
        weak_tags=[m["tag"] for m in profiles[:3]],
    )
    await log_agent_run(db, {**result, "user_id": user.id, "agent": "roadmap"})
    data = result.get("data") or {}
    return RoadmapResult(
        stages=data.get("stages", []),
        rationale=data.get("rationale", ""),
        degraded=bool(result.get("degraded", False)),
    )
