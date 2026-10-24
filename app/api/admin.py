"""⑩ 管理后台（批次 C / F15）。

全部接口都需要管理员权限（AdminDep）。设计原则：
  * 危险操作（删题、停用用户）明确确认参数，避免误触；
  * 停用用户时递增 token_version，立即踢下线（否则已签发 token 仍可用）；
  * 提供"数据体检"接口（缺用例的题、孤儿数据），让管理员能主动发现问题。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import AdminDep, DbSession, PageDep
from ..operations import (
    admin_cleanup_orphan_data,
    admin_delete_problem,
    admin_list_problems_without_cases,
    admin_list_users,
    admin_overview,
    admin_set_user,
)
from ..services import agent_run_stats, get_problem

router = APIRouter(prefix="/api/admin", tags=["⑩ 管理后台"])


@router.get("/overview", response_model=dict)
async def overview(
    db: DbSession,
    admin: AdminDep,
    days: int = Query(default=7, ge=1, le=90),
):
    """管理看板概览：用户/题库/提交/任务/Agent 全局指标。"""
    return await admin_overview(db, days=days)


@router.get("/users", response_model=dict)
async def list_users(
    db: DbSession,
    admin: AdminDep,
    page: PageDep,
    keyword: str = "",
):
    """用户列表（按用户名/昵称搜索）。"""
    return await admin_list_users(db, keyword=keyword, limit=page.size, offset=page.offset)


class UserPatchIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    is_active: bool | None = None
    daily_goal: int | None = Field(default=None, ge=1, le=50)


@router.patch("/users/{user_id}", response_model=dict)
async def patch_user(user_id: str, body: UserPatchIn, db: DbSession, admin: AdminDep):
    """修改用户角色 / 启停 / 每日目标。

    安全护栏：
      * 不能把自己降级或停用（避免管理员把自己锁在门外）；
      * 停用用户会递增 token_version，立即失效其所有 token。
    """
    if user_id == admin.id:
        if body.role is not None and body.role != "admin":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "不能修改自己的管理员角色（会把自己锁在门外）"
            )
        if body.is_active is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己的账号")

    row = await admin_set_user(
        db,
        user_id,
        role=body.role,
        is_active=body.is_active,
        daily_goal=body.daily_goal,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    return {
        "id": row.id,
        "username": row.username,
        "role": row.role,
        "is_active": row.is_active,
        "daily_goal": row.daily_goal,
        "token_version": row.token_version,
        "message": "已停用，该用户所有登录状态已失效" if body.is_active is False else "",
    }


@router.get("/problems/health", response_model=dict)
async def problems_health(
    db: DbSession, admin: AdminDep, limit: int = Query(default=50, ge=1, le=200)
):
    """题库体检：找出无法真实判题的题目（缺测试用例）。"""
    missing = await admin_list_problems_without_cases(db, limit=limit)
    return {
        "without_testcases": len(missing),
        "items": missing,
        "hint": "这些题目提交时会回退为「调用方判定」，建议补齐用例："
        "PUT /api/problems/{id}/testcases",
    }


@router.delete("/problems/{problem_id}", response_model=dict)
async def delete_problem(
    problem_id: str,
    db: DbSession,
    admin: AdminDep,
    confirm: bool = Query(default=False, description="必须显式传 true 才执行删除"),
):
    """删除题目（连带用例与标签关联）。

    要求 confirm=true：删题会连带删除提交记录，属于不可逆操作，
    显式确认可避免脚本或误点造成数据丢失。
    """
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "删除不可逆，请显式传 confirm=true 确认",
        )
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")

    title = problem.title
    ok = await admin_delete_problem(db, problem_id)
    return {"ok": ok, "deleted": problem_id, "title": title}


@router.post("/cleanup", response_model=dict)
async def cleanup(db: DbSession, admin: AdminDep):
    """数据清理：孤儿提交、30 天前的已读通知。"""
    return await admin_cleanup_orphan_data(db)


@router.get("/agents", response_model=dict)
async def agents_stats(
    db: DbSession,
    admin: AdminDep,
    days: int = Query(default=7, ge=1, le=90),
):
    """Agent 运行统计（全局，不限当前用户）。"""
    return await agent_run_stats(db, user_id="", days=days)


# ==================================================== 题目管理（对标 Hydro：管理员的内容工作台）
class AdminProblemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=20000)
    input_desc: str = ""
    output_desc: str = ""
    constraints: str = ""
    samples: list[dict[str, str]] = Field(default_factory=list, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=6)
    difficulty: str = "入门"
    time_limit_ms: int = Field(default=1000, ge=100, le=30000)
    memory_limit_mb: int = Field(default=256, ge=16, le=1024)
    # 建题即配用例（管理台一站式；否则题进"缺用例"不健康清单）
    testcases: list[dict[str, str]] = Field(default_factory=list, max_length=50)


class AdminProblemPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    statement: str | None = Field(default=None, min_length=1, max_length=20000)
    input_desc: str | None = None
    output_desc: str | None = None
    constraints: str | None = None
    difficulty: str | None = None
    time_limit_ms: int | None = Field(default=None, ge=100, le=30000)
    memory_limit_mb: int | None = Field(default=None, ge=16, le=1024)


@router.get("/problems", response_model=dict)
async def admin_list_problems(
    db: DbSession,
    admin: AdminDep,
    page: PageDep,
    keyword: str | None = None,
):
    """题目管理列表：分页 + 关键词 + 健康信息（用例数/提交数/创建者）。"""
    from sqlalchemy import func, select

    from ..models import Submission, TestCase
    from ..services import get_problem_tags, list_problems

    rows, total = await list_problems(db, keyword=keyword, offset=page.offset, limit=page.size)
    ids = [p.id for p in rows]
    tag_map = await get_problem_tags(db, ids) if ids else {}
    case_counts = (
        dict(
            (
                await db.execute(
                    select(TestCase.problem_id, func.count())
                    .where(TestCase.problem_id.in_(ids))
                    .group_by(TestCase.problem_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    sub_counts = (
        dict(
            (
                await db.execute(
                    select(Submission.problem_id, func.count())
                    .where(Submission.problem_id.in_(ids))
                    .group_by(Submission.problem_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    return {
        "total": total,
        "items": [
            {
                "id": p.id,
                "title": p.title,
                "difficulty": p.difficulty,
                "difficulty_score": p.difficulty_score,
                "tags": [t["name"] for t in tag_map.get(p.id, [])],
                "case_count": case_counts.get(p.id, 0),
                "submit_count": sub_counts.get(p.id, 0),
                "accepted_count": p.accepted_count,
                "created_by": p.created_by,
                "time_limit_ms": p.time_limit_ms,
                "memory_limit_mb": p.memory_limit_mb,
            }
            for p in rows
        ],
    }


@router.post("/problems", status_code=status.HTTP_201_CREATED)
async def admin_create_problem(body: AdminProblemCreate, db: DbSession, admin: AdminDep):
    """管理员建题（对标 Hydro 内容工作台）：题面 + 用例一站式，建完即可判题。"""

    from ..services import create_problem

    data = {
        "title": body.title,
        "statement": body.statement,
        "input_desc": body.input_desc,
        "output_desc": body.output_desc,
        "constraints": body.constraints,
        # create_problem 内部会 json.dumps(samples)——这里必须传 list，
        # 传 dumps 过的串会双重序列化（测试实测抓到：详情页 loads 后还是 str）
        "samples": body.samples,
        "difficulty": body.difficulty,
        "time_limit_ms": body.time_limit_ms,
        "memory_limit_mb": body.memory_limit_mb,
        "created_by": admin.id,
    }
    problem = await create_problem(db, data, tag_names=body.tags)
    saved_cases = 0
    if body.testcases:
        from ..judge_service import add_testcase

        for c in body.testcases:
            await add_testcase(
                db,
                problem.id,
                {
                    "input": c.get("input", ""),
                    "output": c.get("output", ""),
                    "hidden": str(c.get("hidden", "true")).lower() == "true",
                },
            )
            saved_cases += 1
    return {
        "id": problem.id,
        "title": problem.title,
        "cases_saved": saved_cases,
        "judgeable": saved_cases > 0,
    }


@router.patch("/problems/{problem_id}", response_model=dict)
async def admin_patch_problem(
    problem_id: str, body: AdminProblemPatch, db: DbSession, admin: AdminDep
):
    """改题面/限制（管理员治理任意题，含他人创建与导入的题）。"""
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    updates = body.model_dump(exclude_none=True)
    for k, v in updates.items():
        setattr(problem, k, v)
    await db.flush()
    return {"ok": True, "problem_id": problem_id, "updated": list(updates)}
