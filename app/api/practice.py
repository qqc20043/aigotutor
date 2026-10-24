"""⑬ 智能练习：顽固错题雷达 + 混排练习 + AI 干预方案 + 练习配方。

设计见 core/practice.py 模块注释（Anki leech / Custom Study / interleaving
的对标映射）。搁置/恢复是 review_queue 上的写操作；配方
（practice_sets，Anki 筛选牌组的对应物）保存条件、按需重新生成。
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
    tags: list[str] = Field(
        default_factory=list, max_length=8, description="知识点筛选（Custom Study 条件）"
    )


class SuspendIn(BaseModel):
    suspended: bool = True


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    size: int = Field(default=6, ge=1, le=20)
    include_review: bool = True
    include_leech: bool = True
    include_fresh: bool = True
    tags: list[str] = Field(default_factory=list, max_length=8)


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
        tags_filter=body.tags or None,
    )


# --------------------------------------------------------------- 搁置/恢复
@router.post("/leech/{problem_id}/suspend")
async def suspend_leech(problem_id: str, body: SuspendIn, db: DbSession, user: UserDep):
    """搁置/恢复顽固错题（Anki leech 语义）。

    搁置：移出每日队列与混排（雷达仍可见），适合"先修复别的，回头再来"；
    恢复：回到正常调度。标记掌握时也会自动恢复（见 mastery 接口）。
    无 FSRS 记录的顽固卡（只连错、从未复习）会补建一条搁置记录——
    否则这类题无法搁置，且它本来就该有调度条目（重做后要 FSRS 跟踪）。
    """
    from sqlalchemy import update

    from ..models import ReviewQueue
    from ..services import upsert_review_item

    result = await db.execute(
        update(ReviewQueue)
        .where(ReviewQueue.user_id == user.id, ReviewQueue.problem_id == problem_id)
        .values(suspended=body.suspended)
    )
    if result.rowcount == 0:
        if not body.suspended:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "该题没有复习记录，无需恢复")
        # 补建一条已搁置的调度条目（rating=4 的中性初始化，不制造遗忘事实）
        await upsert_review_item(db, user.id, problem_id, 4)
        await db.execute(
            update(ReviewQueue)
            .where(ReviewQueue.user_id == user.id, ReviewQueue.problem_id == problem_id)
            .values(suspended=True)
        )
    return {"ok": True, "problem_id": problem_id, "suspended": body.suspended}


# --------------------------------------------------------------- 练习配方
def _recipe_out(row) -> dict:
    import json as _json

    return {
        "id": row.id,
        "name": row.name,
        "recipe": _json.loads(row.recipe or "{}"),
        "created_at": row.created_at.isoformat(),
    }


@router.get("/recipes", response_model=dict)
async def list_recipes(db: DbSession, user: UserDep):
    from sqlalchemy import select

    from ..models import PracticeSet

    rows = (
        (
            await db.execute(
                select(PracticeSet)
                .where(PracticeSet.user_id == user.id)
                .order_by(PracticeSet.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_recipe_out(r) for r in rows]}


@router.post("/recipes", status_code=status.HTTP_201_CREATED)
async def create_recipe(body: RecipeIn, db: DbSession, user: UserDep):
    """保存练习配方（Anki 筛选牌组）：存条件不存快照，生成时按当前状态重抽。"""
    import json as _json

    from ..models import PracticeSet

    recipe = {
        "size": body.size,
        "include_review": body.include_review,
        "include_leech": body.include_leech,
        "include_fresh": body.include_fresh,
        "tags": body.tags,
    }
    row = PracticeSet(
        user_id=user.id, name=body.name, recipe=_json.dumps(recipe, ensure_ascii=False)
    )
    db.add(row)
    await db.flush()
    return _recipe_out(row)


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str, db: DbSession, user: UserDep):
    from ..models import PracticeSet

    row = await db.get(PracticeSet, recipe_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配方不存在")
    await db.delete(row)
    await db.flush()
    return {"ok": True}


@router.post("/recipes/{recipe_id}/generate")
async def generate_from_recipe(recipe_id: str, db: DbSession, user: UserDep):
    """按配方生成一次混排练习（动态语义：用当前到期/顽固/新题状态重抽）。"""
    import json as _json

    from ..models import PracticeSet

    row = await db.get(PracticeSet, recipe_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配方不存在")
    recipe = _json.loads(row.recipe or "{}")
    out = await build_interleaved(
        db,
        user.id,
        int(recipe.get("size", 6)),
        include_review=bool(recipe.get("include_review", True)),
        include_leech=bool(recipe.get("include_leech", True)),
        include_fresh=bool(recipe.get("include_fresh", True)),
        tags_filter=recipe.get("tags") or None,
    )
    out["recipe"] = {"id": row.id, "name": row.name, **recipe}
    return out


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
