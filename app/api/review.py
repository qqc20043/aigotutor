"""⑤ 复习（FSRS 间隔重复） —— 4 个接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from ..core import fsrs
from ..core.memory import memory_block_for
from ..core.pipeline import prioritize_review
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import (
    get_problem_tags,
    get_review_queue,
    mastery_scores_map,
    review_forecast,
    upsert_review_item,
)
from .schemas import ReviewMarkIn

router = APIRouter(prefix="/api/review", tags=["⑤ 复习"])


@router.get("/queue", response_model=dict)
async def review_queue(db: DbSession, user: UserDep, limit: int = 50, due_only: bool = False):
    """20/27 复习队列（含 R 值与逾期天数）。

    due_count 必须按 is_due 统计：overdue_days 已被 max(0, ..) 夹紧，
    拿它判断"是否到期"会恒为真（这是修掉的一个真实 bug）。
    """
    items = await get_review_queue(db, user.id, due_only=due_only, limit=limit)
    return {
        "items": items,
        "total": len(items),
        "due_count": sum(1 for i in items if i["is_due"]),
    }


@router.get("/due", response_model=dict)
async def review_due(db: DbSession, user: UserDep, limit: int = 20):
    """21/27 今日待复习 + 未来 30 天预测。"""
    due = await get_review_queue(db, user.id, due_only=True, limit=limit)
    forecast = await review_forecast(db, user.id, days=30)
    return {"due": due, "due_count": len(due), "forecast": forecast}


@router.post("/mark", response_model=dict)
async def review_mark(body: ReviewMarkIn, db: DbSession, user: UserDep):
    """22/27 标记复习结果，用 FSRS 计算下次到期时间。

    搁置卡联动（Anki 语义补全）：手动重做搁置卡并自评"记得"（rating>=3）
    视为修复有效，自动恢复调度；自评"忘了"（rating=1）保持搁置——
    还在修复中，不该回到每日队列继续打击信心。
    """
    # 题目必须存在：否则 upsert 会 INSERT 一条悬空外键的 review_queue 行
    # （FK 约束 500，覆盖率收尾探针实测抓到）
    from sqlalchemy import select as _select

    from ..models import Problem

    if (
        await db.execute(_select(Problem.id).where(Problem.id == body.problem_id))
    ).scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    plan = await upsert_review_item(db, user.id, body.problem_id, body.rating)
    if body.rating >= 3:
        from sqlalchemy import update

        from ..models import ReviewQueue

        await db.execute(
            update(ReviewQueue)
            .where(ReviewQueue.user_id == user.id, ReviewQueue.problem_id == body.problem_id)
            .values(suspended=False)
        )
    curve = fsrs.forgetting_curve(plan["stability"], days=30, step=1.0)
    return {
        "ok": True,
        "schedule": plan,
        "retention_now": fsrs.retrievability(plan["stability"], 0.0),
        "forgetting_curve": curve,
        "next_interval_days": plan["interval_days"],
    }


@router.get("/priority", response_model=dict)
async def review_priority(
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    limit: int = Query(default=20, ge=1, le=50, description="参与排序的到期项上限"),
    time_budget_minutes: int = Query(
        default=0, ge=0, le=600, description="今日时间预算（分钟）；0 表示不限"
    ),
):
    """复习队列优先 Agent：在 FSRS 已筛出的到期项里决定"先做哪个"。

    与 /queue 的分工：/queue 给的是**按到期时间**排的原始队列（记忆模型视角），
    本接口给的是**按学习收益**重排后的队列 + 每条的排序理由（教练视角）。
    两者数据同源，前端可以并排展示"客观到期"与"推荐顺序"的差异。
    """
    due_items = await get_review_queue(db, user.id, due_only=True, limit=limit)
    mastery = await mastery_scores_map(db, user.id)

    # 补齐每条候选的标签与掌握度：排序信号需要"该题知识点掌握度"
    pids = [it["problem_id"] for it in due_items]
    tag_map = await get_problem_tags(db, pids) if pids else {}
    candidates = [
        {
            **it,
            "tags": [t["name"] for t in tag_map.get(it["problem_id"], [])],
            "mastery_scores": mastery,
        }
        for it in due_items
    ]

    if not candidates:
        return {
            "agent": "review",
            "status": "ok",
            "degraded": False,
            "data": None,
            "message": "当前没有到期的复习项，无需排序。",
            "due_count": 0,
        }

    memory = await memory_block_for(db, user.id)
    result = await prioritize_review(
        user.id,
        cfg,
        candidates=candidates,
        time_budget_minutes=time_budget_minutes,
        memory=memory,
    )
    result["due_count"] = len(candidates)
    return result
