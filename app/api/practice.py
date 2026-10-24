"""⑬ 智能练习：顽固错题雷达 + 混排练习 + AI 干预方案。

设计见 core/practice.py 模块注释（Anki leech / Custom Study / interleaving
的对标映射）。全部只读 + 按需生成，零新表。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..core.practice import build_interleaved, find_leeches, intervention_plan
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import get_problem_tags, list_wrong_book, mastery_scores_map

router = APIRouter(prefix="/api/practice", tags=["⑬ 智能练习"])


class InterleavedIn(BaseModel):
    size: int = Field(default=6, ge=1, le=20)
    include_review: bool = True
    include_leech: bool = True
    include_fresh: bool = True


@router.get("/leech")
async def leech_radar(db: DbSession, user: UserDep, limit: int = Query(default=20, ge=1, le=50)):
    """顽固错题雷达：遗忘/连错达阈值的题 + 掌握度缺口与理由。"""
    return await find_leeches(db, user.id, limit=limit)


@router.post("/interleaved")
async def interleaved_set(body: InterleavedIn, db: DbSession, user: UserDep):
    """生成混排练习集：FSRS 到期 / 顽固错题 / 新题 三路采题，相邻不同知识点。"""
    return await build_interleaved(
        db,
        user.id,
        body.size,
        include_review=body.include_review,
        include_leech=body.include_leech,
        include_fresh=body.include_fresh,
    )


@router.post("/leech/{problem_id}/plan")
async def leech_plan(problem_id: str, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """为顽固错题生成 AI 干预方案（为什么反复卡 + 子问题拆解 + 先修建议）。"""
    radar = await find_leeches(db, user.id, limit=50)
    item = next((i for i in radar["items"] if i["problem_id"] == problem_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该题不在顽固错题雷达中（可能尚未达到阈值）")
    tag_map = await get_problem_tags(db, [problem_id])
    item["tags"] = [t["name"] for t in tag_map.get(problem_id, [])]

    # 历史摘要：错题本的最近状态 + 各标签掌握度，给诊断师当事实输入
    wrongs = await list_wrong_book(db, user.id, limit=50)
    wb = next((w for w in wrongs if w["problem_id"] == problem_id), None)
    mastery = await mastery_scores_map(db, user.id)
    history_summary = (
        f"错题记录：错误 {wb['wrong_count']} 次，最近一次 {wb.get('last_verdict', '未知')}"
        if wb
        else "错题本无记录（以 FSRS 遗忘为准）"
    )
    history_summary += "；标签掌握度：" + "、".join(
        f"{t} {float(mastery.get(t, 50.0)):.0f}" for t in item["tags"][:5]
    )
    return await intervention_plan(cfg, item, history_summary)
