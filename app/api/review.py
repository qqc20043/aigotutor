"""⑤ 复习（FSRS 间隔重复） —— 3 个接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ..core import fsrs
from ..deps import DbSession, UserDep
from ..services import get_review_queue, review_forecast, upsert_review_item
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
    """22/27 标记复习结果，用 FSRS 计算下次到期时间。"""
    plan = await upsert_review_item(db, user.id, body.problem_id, body.rating)
    curve = fsrs.forgetting_curve(plan["stability"], days=30, step=1.0)
    return {
        "ok": True,
        "schedule": plan,
        "retention_now": fsrs.retrievability(plan["stability"], 0.0),
        "forgetting_curve": curve,
        "next_interval_days": plan["interval_days"],
    }
