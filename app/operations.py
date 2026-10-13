"""运营功能服务（批次 C）：打卡、排行榜、通知、学习报告。

时区处理原则（清单 N6）：
  所有时间在数据库里都是 UTC，但"哪一天"必须按**用户本地时区**算。
  否则东八区用户凌晨打卡会被算进前一天，连续天数莫名中断。
  因此这里统一用 user_today() 取用户本地日期。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    DailyCheckin,
    LearningState,
    MasteryProfile,
    Notification,
    Problem,
    Submission,
    Tag,
    User,
)

DEFAULT_TZ = "Asia/Shanghai"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def user_timezone(name: str | None) -> ZoneInfo:
    """取用户时区；无效或缺失时回落到默认值（不抛异常，保证功能可用）。"""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            return ZoneInfo("UTC")


def user_today(tz_name: str | None, now: datetime | None = None) -> str:
    """用户本地日期（YYYY-MM-DD）。"""
    now = now or _now()
    return now.astimezone(user_timezone(tz_name)).date().isoformat()


def user_day_bounds(tz_name: str | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    """用户本地"今天"对应的 UTC 时间区间 [start, end)。

    用于把"当日提交"这类查询翻译成 UTC 范围条件。
    """
    now = now or _now()
    tz = user_timezone(tz_name)
    local_now = now.astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


# ================================================================ 打卡
async def compute_day_stats(
    db: AsyncSession, user_id: str, tz_name: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """统计用户"今天"（本地时区）的提交与通过数。"""
    start, end = user_day_bounds(tz_name, now)
    stmt = select(Submission).where(
        Submission.user_id == user_id,
        Submission.created_at >= start,
        Submission.created_at < end,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    ac = sum(1 for r in rows if r.verdict == "AC")
    return {
        "date": user_today(tz_name, now),
        "submit_count": len(rows),
        "ac_count": ac,
        "distinct_problems": len({r.problem_id for r in rows}),
    }


async def do_checkin(
    db: AsyncSession, user: User, note: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """执行打卡（幂等：同一天重复调用返回既有记录）。"""
    today = user_today(user.timezone, now)
    stats = await compute_day_stats(db, user.id, user.timezone, now)
    goal_met = stats["ac_count"] >= max(1, user.daily_goal)

    stmt = select(DailyCheckin).where(
        DailyCheckin.user_id == user.id, DailyCheckin.checkin_date == today
    )
    row = (await db.execute(stmt)).scalars().first()

    if row is None:
        row = DailyCheckin(
            user_id=user.id,
            checkin_date=today,
            ac_count=stats["ac_count"],
            submit_count=stats["submit_count"],
            goal_met=goal_met,
            note=note[:200],
        )
        db.add(row)
    else:
        # 已打卡则刷新当日统计（用户可能又做了几道题）
        row.ac_count = stats["ac_count"]
        row.submit_count = stats["submit_count"]
        row.goal_met = goal_met
        if note:
            row.note = note[:200]
    await db.flush()

    streak = await compute_streak(db, user.id, user.timezone, now)
    return {
        "date": today,
        "ac_count": stats["ac_count"],
        "submit_count": stats["submit_count"],
        "goal_met": goal_met,
        "daily_goal": user.daily_goal,
        "streak": streak,
        "already": row is not None,
    }


async def compute_streak(
    db: AsyncSession, user_id: str, tz_name: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """计算连续打卡天数（当前连击 + 历史最长）。

    从今天往前逐日检查。今天未打卡时从昨天开始算，这样用户当天还没打卡
    也不会看到连击归零（体验上更合理）。
    """
    now = now or _now()
    tz = user_timezone(tz_name)
    local_today = now.astimezone(tz).date()

    stmt = select(DailyCheckin.checkin_date).where(DailyCheckin.user_id == user_id)
    dates = {d for (d,) in (await db.execute(stmt)).all()}
    if not dates:
        return {"current": 0, "longest": 0, "total_days": 0, "checked_today": False}

    today_str = local_today.isoformat()
    checked_today = today_str in dates

    # 当前连击：从今天（或昨天）往前数
    cursor = local_today if checked_today else local_today - timedelta(days=1)
    current = 0
    while cursor.isoformat() in dates:
        current += 1
        cursor -= timedelta(days=1)

    # 历史最长：把所有日期排序后扫描
    ordered = sorted(dates)
    longest = 0
    run = 0
    prev = None
    for d in ordered:
        cur = datetime.fromisoformat(d).date()
        if prev is not None and (cur - prev).days == 1:
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        prev = cur

    return {
        "current": current,
        "longest": longest,
        "total_days": len(dates),
        "checked_today": checked_today,
    }


async def checkin_history(
    db: AsyncSession, user_id: str, days: int = 30, now: datetime | None = None
) -> list[dict[str, Any]]:
    """最近 N 天的打卡日历（含未打卡的空白日）。"""
    now = now or _now()
    cutoff = (now - timedelta(days=days)).date().isoformat()
    stmt = (
        select(DailyCheckin)
        .where(DailyCheckin.user_id == user_id, DailyCheckin.checkin_date >= cutoff)
        .order_by(DailyCheckin.checkin_date.desc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "date": r.checkin_date,
            "ac_count": r.ac_count,
            "submit_count": r.submit_count,
            "goal_met": r.goal_met,
            "note": r.note,
        }
        for r in rows
    ]


# ============================================================== 排行榜
async def leaderboard(
    db: AsyncSession,
    *,
    metric: str = "ac",
    window: str = "all",
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """排行榜。

    metric: ac=通过题数 | submit=提交数 | streak=连续打卡天数
    window: all=全部 | week=近 7 天 | month=近 30 天

    注意：跨时区用户用各自的"本地日"边界并不严格可比（这是分布式排行榜的
    固有问题）。这里对窗口类排行统一用 UTC 边界并在响应里标注，
    避免给出"看起来精确但其实不公平"的结果。
    """
    now = now or _now()
    conditions = []
    if window == "week":
        conditions.append(Submission.created_at >= now - timedelta(days=7))
    elif window == "month":
        conditions.append(Submission.created_at >= now - timedelta(days=30))

    if metric == "submit":
        stmt = (
            select(Submission.user_id, func.count(Submission.id).label("value"))
            .where(*conditions)
            .group_by(Submission.user_id)
            .order_by(func.count(Submission.id).desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
    else:
        stmt = (
            select(Submission.user_id, func.count(Submission.id).label("value"))
            .where(Submission.verdict == "AC", *conditions)
            .group_by(Submission.user_id)
            .order_by(func.count(Submission.id).desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()

    user_ids = [r[0] for r in rows]
    users = {}
    if user_ids:
        ustmt = select(User).where(User.id.in_(user_ids))
        users = {u.id: u for u in (await db.execute(ustmt)).scalars().all()}

    out: list[dict[str, Any]] = []
    for rank, (uid, value) in enumerate(rows, start=1):
        u = users.get(uid)
        out.append(
            {
                "rank": rank,
                "user_id": uid,
                "nickname": (u.nickname or u.username) if u else "未知用户",
                "username": u.username if u else "",
                "value": int(value),
            }
        )
    return out


# ============================================================== 通知
async def create_notification(
    db: AsyncSession,
    user_id: str,
    *,
    kind: str = "review",
    title: str = "",
    body: str = "",
    link: str = "",
    dedupe_key: str = "",
) -> Notification | None:
    """创建站内通知。

    dedupe_key 非空时做去重：同用户同 key 且未读的通知只保留一条 ——
    否则"今天有 20 题待复习"这类提醒会每天堆一条，很快就刷满列表。
    """
    if dedupe_key:
        stmt = (
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.link == link,
                Notification.kind == kind,
            )
            .limit(1)
        )
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            existing.title = title or existing.title
            existing.body = body or existing.body
            await db.flush()
            return existing

    row = Notification(user_id=user_id, kind=kind, title=title[:120], body=body, link=link[:200])
    db.add(row)
    await db.flush()
    return row


async def list_notifications(
    db: AsyncSession, user_id: str, *, unread_only: bool = False, limit: int = 50
) -> dict[str, Any]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())

    count_stmt = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    unread = int((await db.execute(count_stmt)).scalar_one())

    return {
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "link": r.link,
                "read": r.read_at is not None,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "unread_count": unread,
    }


async def mark_notifications_read(
    db: AsyncSession, user_id: str, ids: list[str] | None = None
) -> int:
    """标记已读；ids 为空表示全部标记已读。"""
    stmt = select(Notification).where(
        Notification.user_id == user_id, Notification.read_at.is_(None)
    )
    if ids:
        stmt = stmt.where(Notification.id.in_(ids))
    rows = list((await db.execute(stmt)).scalars().all())
    now = _now()
    for r in rows:
        r.read_at = now
    await db.flush()
    return len(rows)


async def generate_review_reminders(db: AsyncSession, user: User) -> int:
    """根据复习队列生成提醒通知。返回生成/更新的条数。"""
    from .core import fsrs
    from .models import ReviewQueue

    now = _now()
    stmt = select(ReviewQueue).where(ReviewQueue.user_id == user.id)
    rows = list((await db.execute(stmt)).scalars().all())
    due = [r for r in rows if r.due_at is not None and fsrs.is_due(r.due_at, now)]
    if not due:
        return 0

    await create_notification(
        db,
        user.id,
        kind="review",
        title=f"有 {len(due)} 道题待复习",
        body="间隔重复提醒：这些题的记忆保持率已低于目标值，建议今天复习。",
        link="/review",
        dedupe_key=f"review-{user_today(user.timezone, now)}",
    )
    return len(due)


# ============================================================== 学习报告
async def build_report(
    db: AsyncSession, user: User, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    """生成学习报告数据（供 Markdown 导出与前端展示）。"""
    now = now or _now()
    since = now - timedelta(days=days)

    # 提交与通过
    sub_stmt = select(Submission).where(
        Submission.user_id == user.id, Submission.created_at >= since
    )
    subs = list((await db.execute(sub_stmt)).scalars().all())
    total = len(subs)
    ac = sum(1 for s in subs if s.verdict == "AC")

    # 按判定结果分布
    verdicts: dict[str, int] = {}
    for s in subs:
        verdicts[s.verdict] = verdicts.get(s.verdict, 0) + 1

    # 按日统计（用用户本地时区划分日界）
    tz = user_timezone(user.timezone)
    by_day: dict[str, dict[str, int]] = {}
    for i in range(days):
        d = (now.astimezone(tz).date() - timedelta(days=i)).isoformat()
        by_day[d] = {"submit": 0, "ac": 0}
    for s in subs:
        created = s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc)
        d = created.astimezone(tz).date().isoformat()
        if d in by_day:
            by_day[d]["submit"] += 1
            if s.verdict == "AC":
                by_day[d]["ac"] += 1

    # 掌握度变化
    state_stmt = (
        select(LearningState)
        .where(LearningState.user_id == user.id, LearningState.created_at >= since)
        .order_by(LearningState.created_at)
    )
    states = list((await db.execute(state_stmt)).scalars().all())
    score_start = states[0].score if states else None
    score_end = states[-1].score if states else None

    # 最弱知识点
    prof_stmt = (
        select(MasteryProfile, Tag)
        .join(Tag, Tag.id == MasteryProfile.tag_id)
        .where(MasteryProfile.user_id == user.id)
        .order_by(MasteryProfile.score)
        .limit(5)
    )
    weak = [
        {"tag": tag.name, "score": round(p.score, 1)}
        for p, tag in (await db.execute(prof_stmt)).all()
    ]

    # 错题
    from .models import WrongBook

    wrong_stmt = (
        select(func.count())
        .select_from(WrongBook)
        .where(
            WrongBook.user_id == user.id,
            WrongBook.mastered == False,  # noqa: E712
        )
    )
    wrong_count = int((await db.execute(wrong_stmt)).scalar_one())

    streak = await compute_streak(db, user.id, user.timezone, now)

    return {
        "user": {"nickname": user.nickname or user.username, "level": user.level},
        "window_days": days,
        "generated_at": now,
        "summary": {
            "submissions": total,
            "ac": ac,
            "ac_rate": round(ac / total, 4) if total else 0.0,
            "wrong_pending": wrong_count,
            "score_start": score_start,
            "score_end": score_end,
            "score_delta": round((score_end - score_start), 2)
            if score_start is not None and score_end is not None
            else None,
        },
        "verdicts": verdicts,
        "daily": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "weak_tags": weak,
        "streak": streak,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    """把报告数据渲染成 Markdown（可直接保存/分享）。"""
    s = report["summary"]
    lines: list[str] = []
    lines.append(f"# {report['user']['nickname']} 的学习报告")
    lines.append("")
    lines.append(f"统计窗口：近 {report['window_days']} 天")
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 提交次数 | {s['submissions']} |")
    lines.append(f"| 通过次数 | {s['ac']} |")
    lines.append(f"| 通过率 | {s['ac_rate'] * 100:.1f}% |")
    lines.append(f"| 待攻克错题 | {s['wrong_pending']} |")
    if s["score_start"] is not None and s["score_end"] is not None:
        delta = s["score_delta"] or 0
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(
            f"| 学习状态 | {s['score_start']:.1f} → {s['score_end']:.1f}（{arrow}{abs(delta):.1f}） |"
        )
    streak = report.get("streak") or {}
    lines.append(
        f"| 连续打卡 | {streak.get('current', 0)} 天（最长 {streak.get('longest', 0)} 天） |"
    )
    lines.append("")

    # 每日趋势
    daily = report.get("daily") or []
    if daily:
        lines.append("## 每日趋势")
        lines.append("")
        lines.append("| 日期 | 提交 | 通过 |")
        lines.append("|---|---|---|")
        for d in daily:
            lines.append(f"| {d['date']} | {d['submit']} | {d['ac']} |")
        lines.append("")

    # 判定分布
    verdicts = report.get("verdicts") or {}
    if verdicts:
        lines.append("## 判定分布")
        lines.append("")
        lines.append("| 结果 | 次数 |")
        lines.append("|---|---|")
        for v, c in sorted(verdicts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {v} | {c} |")
        lines.append("")

    # 薄弱知识点
    weak = report.get("weak_tags") or []
    if weak:
        lines.append("## 待补强知识点")
        lines.append("")
        for w in weak:
            lines.append(f"- **{w['tag']}**：掌握度 {w['score']}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*由 AIgoTutor 生成于 {report['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}*")
    return "\n".join(lines)


# ============================================================== 管理后台
async def admin_overview(
    db: AsyncSession, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    """管理看板：用户、题库、提交、任务、Agent 的全局概览。"""
    from .models import AgentRun, BackgroundTask, Problem, TestCase

    now = now or _now()
    since = now - timedelta(days=days)

    async def count(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        if where:
            stmt = stmt.where(*where)
        return int((await db.execute(stmt)).scalar_one())

    total_users = await count(User)
    active_users = await count(User, User.is_active == True)  # noqa: E712
    admins = await count(User, User.role == "admin")
    total_problems = await count(Problem)
    total_testcases = await count(TestCase)
    problems_without_cases = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Problem)
                .where(~Problem.id.in_(select(TestCase.problem_id)))
            )
        ).scalar_one()
    )
    total_submissions = await count(Submission)
    recent_submissions = await count(Submission, Submission.created_at >= since)

    agent_runs = list(
        (await db.execute(select(AgentRun).where(AgentRun.created_at >= since))).scalars().all()
    )
    degraded = sum(1 for r in agent_runs if r.degraded)
    failed = sum(1 for r in agent_runs if r.status == "failed")
    tokens = sum(r.prompt_tokens + r.completion_tokens for r in agent_runs)

    task_stmt = select(BackgroundTask.status, func.count(BackgroundTask.id)).group_by(
        BackgroundTask.status
    )
    tasks = {status: int(c) for status, c in (await db.execute(task_stmt)).all()}

    return {
        "window_days": days,
        "users": {"total": total_users, "active": active_users, "admins": admins},
        "problems": {
            "total": total_problems,
            "testcases": total_testcases,
            "without_testcases": problems_without_cases,
        },
        "submissions": {"total": total_submissions, "recent": recent_submissions},
        "agents": {
            "calls": len(agent_runs),
            "degraded": degraded,
            "failed": failed,
            "tokens": tokens,
            "degraded_rate": round(degraded / len(agent_runs), 4) if agent_runs else 0.0,
        },
        "tasks": tasks,
    }


async def admin_list_users(
    db: AsyncSession, *, keyword: str = "", limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """用户列表（支持按用户名/昵称搜索）。"""
    stmt = select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where((User.username.like(like)) | (User.nickname.like(like)))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = list(
        (await db.execute(stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": u.id,
                "username": u.username,
                "nickname": u.nickname,
                "role": u.role,
                "level": u.level,
                "is_active": u.is_active,
                "timezone": u.timezone,
                "last_login_at": u.last_login_at,
                "created_at": u.created_at,
            }
            for u in rows
        ],
    }


async def admin_set_user(
    db: AsyncSession,
    user_id: str,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    daily_goal: int | None = None,
) -> User | None:
    """修改用户角色/启停/目标。"""
    user = await db.get(User, user_id)
    if user is None:
        return None
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active
        if not is_active:
            # 停用即踢下线：递增 token 版本，避免已签发的 token 继续可用
            user.token_version += 1
    if daily_goal is not None:
        user.daily_goal = daily_goal
    await db.flush()
    return user


async def admin_list_problems_without_cases(
    db: AsyncSession, limit: int = 50
) -> list[dict[str, Any]]:
    """列出缺测试用例的题目（这些题无法真实判题，需要补齐）。"""
    from .models import Problem, TestCase

    stmt = (
        select(Problem)
        .where(~Problem.id.in_(select(TestCase.problem_id)))
        .order_by(Problem.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "id": p.id,
            "title": p.title,
            "difficulty": p.difficulty,
            "source": p.source,
            "created_at": p.created_at,
        }
        for p in rows
    ]


async def admin_delete_problem(db: AsyncSession, problem_id: str) -> bool:
    """删除题目（连带用例与标签关联由外键 CASCADE 处理）。"""
    problem = await db.get(Problem, problem_id)
    if problem is None:
        return False
    await db.delete(problem)
    await db.flush()
    return True


async def admin_cleanup_orphan_data(db: AsyncSession) -> dict[str, int]:
    """清理无主数据：已删题目的提交、过期的未读通知等。"""
    # 已删题目的提交（理论上 CASCADE 会处理，这里兜底）
    orphan_subs = await db.execute(
        delete(Submission).where(~Submission.problem_id.in_(select(Problem.id)))
    )
    # 30 天前的已读通知
    cutoff = _now() - timedelta(days=30)
    old_notifs = await db.execute(
        delete(Notification).where(
            Notification.read_at.is_not(None), Notification.created_at < cutoff
        )
    )
    await db.flush()
    return {
        "orphan_submissions": orphan_subs.rowcount or 0,
        "old_notifications": old_notifs.rowcount or 0,
    }
