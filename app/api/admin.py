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


# ================================================ 提交管理（全平台视角）
class AdminVerdictIn(BaseModel):
    pass


class AnnounceIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=2000)
    link: str = Field(default="", max_length=200)
    username: str = Field(default="", description="留空=全员广播")


@router.get("/submissions", response_model=dict)
async def admin_list_submissions(
    db: DbSession,
    admin: AdminDep,
    page: PageDep,
    username: str = "",
    verdict: str = "",
    problem_id: str = "",
):
    """全平台提交管理：按用户/判定/题目筛选（管理员视角，含代码）。"""
    from sqlalchemy import func, select

    from ..models import Problem, Submission, User

    stmt = (
        select(Submission, User.username, Problem.title)
        .join(User, User.id == Submission.user_id)
        .join(Problem, Problem.id == Submission.problem_id)
    )
    count_stmt = (
        select(func.count()).select_from(Submission).join(User, User.id == Submission.user_id)
    )
    if username:
        stmt = stmt.where(User.username.contains(username))
        count_stmt = count_stmt.where(User.username.contains(username))
    if verdict:
        stmt = stmt.where(Submission.verdict == verdict)
        count_stmt = count_stmt.where(Submission.verdict == verdict)
    if problem_id:
        stmt = stmt.where(Submission.problem_id == problem_id)
        count_stmt = count_stmt.where(Submission.problem_id == problem_id)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        await db.execute(
            stmt.order_by(Submission.created_at.desc()).offset(page.offset).limit(page.size)
        )
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": sub.id,
                "username": uname,
                "problem_id": sub.problem_id,
                "problem_title": ptitle,
                "language": sub.language,
                "verdict": sub.verdict,
                "passed_cases": sub.passed_cases,
                "total_cases": sub.total_cases,
                "runtime_ms": sub.runtime_ms,
                "judged_by": sub.judged_by,
                "created_at": sub.created_at,
                "code_preview": (sub.code or "")[:300],
            }
            for sub, uname, ptitle in rows
        ],
    }


@router.post("/submissions/{submission_id}/rejudge", response_model=dict)
async def admin_rejudge(submission_id: str, db: DbSession, admin: AdminDep):
    """管理员重判（题库用例更新后，历史提交结果可能过期）。"""
    from ..judge_service import judge_submission_in_sandbox
    from ..models import Submission

    sub = await db.get(Submission, submission_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提交不存在")
    problem = await get_problem(db, sub.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    fields = await judge_submission_in_sandbox(db, problem, sub.language, sub.code)
    old = sub.verdict
    sub.verdict = fields["verdict"]
    sub.passed_cases = fields["passed_cases"]
    sub.total_cases = fields["total_cases"]
    sub.runtime_ms = fields["runtime_ms"]
    sub.memory_kb = fields["memory_kb"]
    sub.failed_case = fields.get("failed_case", "")
    import json as _json

    sub.judge_detail = _json.dumps(fields.get("judge_detail", {}), ensure_ascii=False)
    sub.judged_by = "sandbox"
    await db.flush()
    return {
        "ok": True,
        "submission_id": submission_id,
        "old_verdict": old,
        "new_verdict": sub.verdict,
    }


# ================================================ 用户详情
@router.get("/users/{user_id}", response_model=dict)
async def admin_user_detail(user_id: str, db: DbSession, admin: AdminDep):
    """单用户档案：基础信息 + 学习数据 + 最近提交 + 会话统计。"""
    from sqlalchemy import Integer, func, select

    from ..models import ChatSession, ReviewQueue, Submission, User

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    sub_row = (
        await db.execute(
            select(
                func.count(Submission.id),
                func.sum(func.cast(Submission.verdict == "AC", Integer)),
            ).where(Submission.user_id == user_id)
        )
    ).one()
    recent = (
        (
            await db.execute(
                select(Submission)
                .where(Submission.user_id == user_id)
                .order_by(Submission.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    review_count = int(
        (await db.execute(select(func.count()).where(ReviewQueue.user_id == user_id))).scalar() or 0
    )
    chat_count = int(
        (await db.execute(select(func.count()).where(ChatSession.user_id == user_id))).scalar() or 0
    )
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "role": user.role,
        "is_active": user.is_active,
        "level": user.level,
        "daily_goal": user.daily_goal,
        "timezone": user.timezone,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "stats": {
            "submissions": int(sub_row[0] or 0),
            "ac_count": int(sub_row[1] or 0),
            "review_items": review_count,
            "chat_sessions": chat_count,
        },
        "recent_submissions": [
            {
                "id": s.id,
                "problem_id": s.problem_id,
                "verdict": s.verdict,
                "language": s.language,
                "created_at": s.created_at,
            }
            for s in recent
        ],
    }


@router.post("/users/{user_id}/reset-password", response_model=dict)
async def admin_reset_password(user_id: str, db: DbSession, admin: AdminDep):
    """管理员重置用户密码：返回一次性临时密码，并踢下线（token_version+1）。"""
    import secrets

    from ..models import User
    from ..security import hash_password

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    temp = f"tmp{secrets.token_urlsafe(6)}"
    user.password_hash = hash_password(temp)
    user.token_version = int(user.token_version or 0) + 1
    await db.flush()
    return {
        "ok": True,
        "username": user.username,
        "temp_password": temp,
        "note": "请通过安全渠道转交用户；旧 token 已失效，需重新登录",
    }


# ================================================ 公告推送
@router.post("/announce", response_model=dict)
async def admin_announce(body: AnnounceIn, db: DbSession, admin: AdminDep):
    """站内公告/通知：留空 username 时全员广播，否则定向单人。"""
    from sqlalchemy import select

    from ..models import User
    from ..operations import create_notification

    if body.username:
        target = (
            await db.execute(select(User).where(User.username == body.username))
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
        targets = [target.id]
    else:
        targets = list((await db.execute(select(User.id).where(User.is_active))).scalars().all())

    sent = 0
    for uid in targets:
        await create_notification(
            db, uid, kind="system", title=body.title, body=body.body, link=body.link
        )
        sent += 1
    return {"ok": True, "sent": sent, "scope": "single" if body.username else "broadcast"}
