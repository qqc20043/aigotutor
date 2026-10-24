"""⑥ 掌握度画像 + 错题本 —— 5 个接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..core.pipeline import analyze_mastery
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import (
    build_tag_stats,
    build_tag_stats_by_id,
    cluster_errors,
    compute_and_store_state,
    get_mastery,
    list_clusters,
    list_wrong_book,
    log_agent_run,
    mark_wrong_mastered,
    state_timeline,
    upsert_mastery_profiles,
)
from .schemas import MasteryOut, MasteryProfileOut

router = APIRouter(prefix="/api/mastery", tags=["⑥ 掌握度"])


@router.get("/profile", response_model=list[MasteryProfileOut])
async def mastery_profile(db: DbSession, user: UserDep):
    """23/27 掌握度画像（按标签，分数升序 = 最弱在前）。"""
    rows = await get_mastery(db, user.id)
    return [MasteryProfileOut(**r) for r in rows]


@router.get("/overview", response_model=MasteryOut)
async def mastery_overview(
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    window_days: int = Query(default=30, ge=1, le=365),
):
    """24/27 学习状态总览：0-100 分 + 五级 + 置信度（Agent 生成，规则兜底）。"""
    tag_stats = await build_tag_stats(db, user.id, window_days)
    total = sum(int(v.get("attempts", 0)) for v in tag_stats.values())
    ac = sum(int(v.get("ac_count", 0)) for v in tag_stats.values())
    avg_diff = sum(
        float(v.get("avg_difficulty", 40.0)) * float(v.get("attempts", 0))
        for v in tag_stats.values()
    ) / max(1, total)
    result = await analyze_mastery(
        user.id,
        cfg,
        tag_stats=tag_stats,
        window_days=window_days,
        total_submissions=total,
        ac_count=ac,
        avg_difficulty_score=round(avg_diff, 2),
        active_days=window_days,
    )
    await log_agent_run(db, {**result, "user_id": user.id, "agent": "mastery"})
    data = result.get("data") or {}
    # 画像落库必须按 tag_id（真外键），不能用标签名
    await upsert_mastery_profiles(
        db, user.id, await build_tag_stats_by_id(db, user.id, window_days)
    )
    return MasteryOut(
        score=float(data.get("score", 0.0)),
        level=data.get("level", "L1"),
        confidence=float(data.get("confidence", 0.0)),
        weak_tags=data.get("weak_tags", []),
        strong_tags=data.get("strong_tags", []),
        suggestion=data.get("suggestion", ""),
        metrics={
            "total_submissions": total,
            "ac_count": ac,
            "tag_count": len(tag_stats),
            "degraded": result.get("degraded", False),
            "rule_baseline": result.get("rule_baseline", {}),
        },
    )


@router.get("/timeline", response_model=dict)
async def mastery_timeline(
    db: DbSession, user: UserDep, limit: int = Query(default=60, ge=1, le=200)
):
    """25/27 学习状态时间序列。"""
    return {"items": await state_timeline(db, user.id, limit)}


@router.post("/recalc", response_model=dict)
async def mastery_recalc(
    db: DbSession, user: UserDep, window_days: int = Query(default=30, ge=1, le=365)
):
    """26/27 重算掌握度并写入状态快照。"""
    rows = await upsert_mastery_profiles(
        db, user.id, await build_tag_stats_by_id(db, user.id, window_days)
    )
    state = await compute_and_store_state(db, user.id, window_days=window_days, source="manual")
    clusters = await cluster_errors(db, user.id)
    return {"profile_rows": rows, "state": state, "clusters": clusters}


wrong_router = APIRouter(prefix="/api/wrong-book", tags=["⑥ 掌握度"])


@wrong_router.get("", response_model=dict)
async def wrong_book_list(
    db: DbSession, user: UserDep, mastered: bool | None = None, limit: int = 50
):
    """27/27 错题本 + 易错题聚类。"""
    items = await list_wrong_book(db, user.id, mastered=mastered, limit=limit)
    clusters = await list_clusters(db, user.id)
    return {
        "items": items,
        "total": len(items),
        "clusters": clusters,
        "cluster_count": len(clusters),
    }


@wrong_router.post("/{problem_id}/mastered", response_model=dict)
async def wrong_mastered(problem_id: str, db: DbSession, user: UserDep, mastered: bool = True):
    ok = await mark_wrong_mastered(db, user.id, problem_id, mastered)
    if mastered:
        # 标记掌握时自动恢复搁置的顽固卡（Anki 语义：修复完成后回到队列）
        from sqlalchemy import update

        from ..models import ReviewQueue

        await db.execute(
            update(ReviewQueue)
            .where(ReviewQueue.user_id == user.id, ReviewQueue.problem_id == problem_id)
            .values(suspended=False)
        )
    return {"ok": ok, "problem_id": problem_id, "mastered": mastered}


@wrong_router.post("/cluster", response_model=dict)
async def recluster(db: DbSession, user: UserDep):
    clusters = await cluster_errors(db, user.id)
    return {"clusters": clusters, "count": len(clusters)}
