"""⑨ 运营与导入：题面导入、学习报告、打卡、排行榜、通知（批次 C）。

这些接口是"让平台真正能用起来"的部分：把题弄进来、把进步记录下来、
把该复习的提醒到人。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..core.pipeline import annotate_problem
from ..deps import DbSession, ModelConfigDep, UserDep
from ..importer import parse_problem
from ..judge_service import replace_testcases
from ..operations import (
    build_report,
    checkin_history,
    compute_day_stats,
    compute_streak,
    create_notification,
    do_checkin,
    generate_review_reminders,
    leaderboard,
    list_notifications,
    mark_notifications_read,
    render_report_markdown,
)
from ..services import bind_tags, create_problem

router = APIRouter(prefix="/api/import", tags=["⑨ 运营"])


# ---------------------------------------------------------------- 题面导入
class ImportTextIn(BaseModel):
    """从粘贴的题面文本导入。"""

    text: str = Field(min_length=1, description="题面原文（支持 HTML 片段）")
    source: str = "import"
    auto_annotate: bool = True
    tags: list[str] = []
    # 可选：随题面一起提供测试用例
    testcases: list[dict] = []


class ImportBatchIn(BaseModel):
    items: list[ImportTextIn] = []
    auto_annotate: bool = True


@router.post("/preview", response_model=dict)
async def preview_import(body: ImportTextIn, user: UserDep):
    """只解析不落库：让用户先确认解析结果，避免脏数据进库。"""
    parsed = parse_problem(body.text, default_source=body.source)
    data = parsed.to_dict()
    return {
        "parsed": data,
        "warnings": parsed.warnings,
        "preview": {
            "title": parsed.title,
            "statement_preview": parsed.statement[:300],
            "samples_found": len(parsed.samples),
            "tags": parsed.tags,
            "difficulty_hint": parsed.difficulty_hint,
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def import_problem(body: ImportTextIn, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """导入单道题：解析 -> （可选）两阶段标注 -> 落库（含用例）。"""
    parsed = parse_problem(body.text, default_source=body.source)
    if not parsed.statement and not parsed.title:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "题面解析失败：既没识别到标题也没识别到正文，请检查格式",
        )

    tags = body.tags or parsed.tags
    difficulty = parsed.difficulty_hint or "入门"
    difficulty_score = 0.0
    difficulty_source = "rule"

    if body.auto_annotate:
        annotation = await annotate_problem(
            {
                "title": parsed.title,
                "statement": parsed.statement,
                "constraints": parsed.constraints,
                "samples": parsed.samples,
                "tags": tags,
                "statement_len": parsed.statement_len,
                "tag_count": parsed.tag_count,
                "algo_depth": parsed.algo_depth,
                "constraint_tight": parsed.constraint_tight,
                "is_constructive": parsed.is_constructive,
            },
            cfg,
            user_id=user.id,
        )
        difficulty = annotation["difficulty"]
        difficulty_score = annotation["difficulty_score"]
        difficulty_source = annotation["difficulty_source"]
        tags = annotation["tags"]

    problem = await create_problem(
        db,
        {
            "title": parsed.title or "未命名题目",
            "statement": parsed.statement,
            "input_desc": parsed.input_desc,
            "output_desc": parsed.output_desc,
            "samples": parsed.samples,
            "constraints": parsed.constraints,
            "source": parsed.source,
            "source_id": parsed.source_id,
            "difficulty": difficulty,
            "difficulty_score": difficulty_score,
            "difficulty_source": difficulty_source,
        },
        tag_names=tags,
    )
    if tags:
        await bind_tags(db, problem.id, tags)

    saved_cases = 0
    if body.testcases:
        rows = await replace_testcases(db, problem.id, body.testcases)
        saved_cases = len(rows)

    return {
        "id": problem.id,
        "title": problem.title,
        "difficulty": problem.difficulty,
        "difficulty_score": problem.difficulty_score,
        "difficulty_source": difficulty_source,
        "tags": tags,
        "samples": len(parsed.samples),
        "testcases": saved_cases,
        "warnings": parsed.warnings,
    }


@router.post("/batch", response_model=dict, status_code=status.HTTP_201_CREATED)
async def import_batch(body: ImportBatchIn, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """批量导入（每项独立解析，单项失败不影响其他项）。"""
    created: list[dict] = []
    failed: list[dict] = []
    for i, item in enumerate(body.items[:100]):
        try:
            single = ImportTextIn(
                text=item.text,
                source=item.source,
                auto_annotate=body.auto_annotate and item.auto_annotate,
                tags=item.tags,
                testcases=item.testcases,
            )
            result = await import_problem(single, db, user, cfg)
            created.append({"index": i, **result})
        except Exception as e:
            failed.append({"index": i, "error": f"{type(e).__name__}: {e}"})
    return {"imported": len(created), "failed": len(failed), "items": created, "errors": failed}


# ---------------------------------------------------------------- 学习报告
report_router = APIRouter(prefix="/api/report", tags=["⑨ 运营"])


@report_router.get("", response_model=dict)
async def get_report(db: DbSession, user: UserDep, days: int = Query(default=7, ge=1, le=365)):
    """学习报告数据（JSON）。"""
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return await build_report(db, row, days=days)


# ---------------------------------------------------------------- 成长体系
gamification_router = APIRouter(prefix="/api/gamification", tags=["⑨ 运营"])


@gamification_router.get("", response_model=dict)
async def gamification_summary(db: DbSession, user: UserDep):
    """成长体系：XP（事实派生，含逐项明细）+ 等级 + 成就徽章 + 断签保护状态。

    XP 不建账本：全部由 submissions/checkins/interview_reports/review_queue
    等事实表实时派生（公式见 core/gamification.py），调整公式即全量生效。
    """
    from ..core.gamification import compute_xp, evaluate_achievements
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    xp = await compute_xp(db, user.id)
    streak = await compute_streak(db, user.id, row.timezone)
    achievements = await evaluate_achievements(db, user.id, streak)
    return {
        **xp,
        "achievements": achievements,
        "unlocked_count": sum(1 for a in achievements if a["unlocked"]),
        "streak": streak,
    }


@report_router.get("/markdown", response_class=PlainTextResponse)
async def get_report_markdown(
    db: DbSession, user: UserDep, days: int = Query(default=7, ge=1, le=365)
):
    """学习报告（Markdown，可直接保存或分享）。"""
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    report = await build_report(db, row, days=days)
    md = render_report_markdown(report)
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{days}d.md"'},
    )


# ---------------------------------------------------------------- 打卡
checkin_router = APIRouter(prefix="/api/checkin", tags=["⑨ 运营"])


class CheckinIn(BaseModel):
    note: str = Field(default="", max_length=200)


@checkin_router.post("", response_model=dict)
async def check_in(body: CheckinIn, db: DbSession, user: UserDep):
    """打卡（按用户本地时区判定"今天"，同一天重复打卡幂等）。"""
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    result = await do_checkin(db, row, note=body.note)
    if result["goal_met"]:
        await create_notification(
            db,
            user.id,
            kind="goal",
            title=f"今日目标达成（{result['ac_count']}/{result['daily_goal']}）",
            body=f"连续打卡 {result['streak']['current']} 天，继续保持。",
            link="/dashboard",
            dedupe_key=f"goal-{result['date']}",
        )
    return result


@checkin_router.get("/status", response_model=dict)
async def checkin_status(db: DbSession, user: UserDep):
    """当前打卡状态：今日统计、连续天数、历史。"""
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    stats = await compute_day_stats(db, row.id, row.timezone)
    streak = await compute_streak(db, row.id, row.timezone)
    history = await checkin_history(
        db, row.id, tz_name=row.timezone, days=60, daily_goal=row.daily_goal
    )
    return {
        "today": stats,
        "daily_goal": row.daily_goal,
        "streak": streak,
        "history": history,
        "timezone": row.timezone,
    }


# ---------------------------------------------------------------- 排行榜
rank_router = APIRouter(prefix="/api/leaderboard", tags=["⑨ 运营"])


@rank_router.get("", response_model=dict)
async def get_leaderboard(
    db: DbSession,
    user: UserDep,
    metric: str = Query(default="ac", pattern="^(ac|submit)$"),
    window: str = Query(default="all", pattern="^(all|week|month)$"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """排行榜。

    注意：窗口类排行（week/month）用 UTC 日界统计，跨时区用户严格来说
    不完全可比 —— 这是分布式排行榜的固有问题，已在响应里标注。
    """
    items = await leaderboard(db, metric=metric, window=window, limit=limit)

    # 标出当前用户的名次（若在前 limit 内）
    my_rank = next((i["rank"] for i in items if i["user_id"] == user.id), None)
    return {
        "metric": metric,
        "window": window,
        "items": items,
        "my_rank": my_rank,
        "note": "窗口类排行按 UTC 日界统计，跨时区用户不完全可比" if window != "all" else "",
    }


# ---------------------------------------------------------------- 通知
notif_router = APIRouter(prefix="/api/notifications", tags=["⑨ 运营"])


class MarkReadIn(BaseModel):
    ids: list[str] = []


@notif_router.get("", response_model=dict)
async def get_notifications(
    db: DbSession,
    user: UserDep,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
):
    """通知列表。"""
    return await list_notifications(db, user.id, unread_only=unread_only, limit=limit)


@notif_router.post("/read", response_model=dict)
async def read_notifications(body: MarkReadIn, db: DbSession, user: UserDep):
    """标记已读（ids 为空表示全部已读）。"""
    n = await mark_notifications_read(db, user.id, body.ids or None)
    return {"ok": True, "marked": n}


@notif_router.post("/reminders", response_model=dict)
async def trigger_reminders(db: DbSession, user: UserDep):
    """手动触发复习提醒生成（生产环境应由定时任务调用）。"""
    from ..models import User as UserModel

    row = await db.get(UserModel, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    n = await generate_review_reminders(db, row)
    return {"ok": True, "due": n}


# ---------------------------------------------------------------- 导出
export_router = APIRouter(prefix="/api/data", tags=["⑨ 运营"])


@export_router.get("/report/markdown", response_class=PlainTextResponse, include_in_schema=False)
async def report_alias(db: DbSession, user: UserDep, days: int = 7):
    """兼容别名：/api/data/report/markdown -> /api/report/markdown。"""
    return await get_report_markdown(db, user, days)


def _json_dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)
