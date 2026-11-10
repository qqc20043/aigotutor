"""⑭ 学习计划 —— LeetCode Study Plan 的对标实现（列表 + 详情，进度派生）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..deps import DbSession, UserDep
from ..services import get_study_plan_detail, list_study_plans

router = APIRouter(prefix="/api/study-plans", tags=["⑭ 学习计划"])


@router.get("", response_model=dict)
async def list_plans(db: DbSession, user: UserDep):
    """计划列表 + 我的进度（零报名：某题有 AC 即视为完成）。"""
    items = await list_study_plans(db, user.id)
    return {"items": items, "in_progress": [i for i in items if i["in_progress"]]}


@router.get("/{plan_id}", response_model=dict)
async def plan_detail(plan_id: str, db: DbSession, user: UserDep):
    """计划详情：题目清单 + 每题完成状态。"""
    data = await get_study_plan_detail(db, plan_id, user.id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学习计划不存在")
    return data
