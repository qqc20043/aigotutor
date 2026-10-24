"""服务层：数据库读写 + 业务聚合。接口层只做参数校验与调用编排。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .core import fsrs
from .core.mastery import compute_score, compute_tag_stats, recalc_profile_rows
from .db import session_scope
from .models import (
    AgentRun,
    BackgroundTask,
    ErrorCluster,
    LearningState,
    MasteryProfile,
    Problem,
    ProblemTag,
    Review,
    ReviewQueue,
    Submission,
    Tag,
    User,
    UserModelConfig,
    WrongBook,
)
from .security import encrypt_secret


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ================================================================== 用户
async def create_user(
    db: AsyncSession, username: str, password_hash: str, nickname: str = ""
) -> User:
    user = User(username=username, password_hash=password_hash, nickname=nickname or username)
    db.add(user)
    await db.flush()
    return user


async def get_user_by_name(db: AsyncSession, username: str) -> User | None:
    return (await db.execute(select(User).where(User.username == username))).scalars().first()


async def touch_login(db: AsyncSession, user: User) -> None:
    """记录最近登录时间（用于活跃度统计与"长期未登录"筛选）。"""
    user.last_login_at = _now()
    await db.flush()


async def upsert_model_config(
    db: AsyncSession,
    user_id: str,
    *,
    purpose: str = "default",
    provider: str,
    model: str,
    base_url: str = "",
    api_key_ref: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    enabled: bool = True,
) -> UserModelConfig:
    stmt = select(UserModelConfig).where(
        UserModelConfig.user_id == user_id, UserModelConfig.purpose == purpose
    )
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if cfg is None:
        cfg = UserModelConfig(user_id=user_id, purpose=purpose)
        db.add(cfg)
    cfg.provider = provider
    cfg.model = model
    cfg.base_url = base_url
    # API Key 加密落库（见 security.encrypt_secret）；读路径统一 decrypt_secret，
    # 存量明文行解密失败时原样返回，保持兼容。
    cfg.api_key_ref = encrypt_secret(api_key_ref)
    cfg.temperature = temperature
    cfg.max_tokens = max_tokens
    cfg.enabled = enabled
    await db.flush()
    return cfg


# ================================================================== 题库
async def list_problems(
    db: AsyncSession,
    *,
    difficulty: str | None = None,
    tag_id: str | None = None,
    keyword: str | None = None,
    source: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Problem], int]:
    stmt = select(Problem)
    if difficulty:
        stmt = stmt.where(Problem.difficulty == difficulty)
    if source:
        stmt = stmt.where(Problem.source == source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(Problem.title.like(like), Problem.statement.like(like)))
    if tag_id:
        stmt = stmt.join(ProblemTag, ProblemTag.problem_id == Problem.id).where(
            ProblemTag.tag_id == tag_id
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Problem.created_at.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_problem(db: AsyncSession, problem_id: str) -> Problem | None:
    return await db.get(Problem, problem_id)


async def get_problem_tags(
    db: AsyncSession, problem_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    if not problem_ids:
        return {}
    stmt = (
        select(ProblemTag, Tag)
        .join(Tag, Tag.id == ProblemTag.tag_id)
        .where(ProblemTag.problem_id.in_(list(problem_ids)))
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for pt, tag in (await db.execute(stmt)).all():
        out.setdefault(pt.problem_id, []).append(
            {"id": tag.id, "name": tag.name, "weight": pt.weight, "is_primary": pt.is_primary}
        )
    return out


async def problem_difficulty_map(db: AsyncSession, problem_ids: Sequence[str]) -> dict[str, float]:
    if not problem_ids:
        return {}
    stmt = select(Problem.id, Problem.difficulty_score).where(Problem.id.in_(list(problem_ids)))
    return {pid: float(score) for pid, score in (await db.execute(stmt)).all()}


async def create_problem(
    db: AsyncSession, data: dict[str, Any], tag_names: list[str] | None = None
) -> Problem:
    problem = Problem(
        title=data.get("title", ""),
        statement=data.get("statement", ""),
        input_desc=data.get("input_desc", ""),
        output_desc=data.get("output_desc", ""),
        samples=json.dumps(data.get("samples", []), ensure_ascii=False),
        constraints=data.get("constraints", ""),
        source=data.get("source", "local"),
        source_id=data.get("source_id", ""),
        created_by=str(data.get("created_by", "")),
        difficulty=data.get("difficulty", "入门"),
        difficulty_score=float(data.get("difficulty_score", 0.0)),
        difficulty_source=data.get("difficulty_source", "rule"),
        time_limit_ms=int(data.get("time_limit_ms", 1000)),
        memory_limit_mb=int(data.get("memory_limit_mb", 256)),
    )
    db.add(problem)
    await db.flush()
    if tag_names:
        await bind_tags(db, problem.id, tag_names)
    return problem


async def bind_tags(db: AsyncSession, problem_id: str, tag_names: list[str]) -> list[ProblemTag]:
    rows: list[ProblemTag] = []
    for i, name in enumerate(tag_names[:4]):
        name = name.strip()
        if not name:
            continue
        tag = (await db.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
        exists = (
            await db.execute(
                select(ProblemTag).where(
                    ProblemTag.problem_id == problem_id, ProblemTag.tag_id == tag.id
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        pt = ProblemTag(
            problem_id=problem_id, tag_id=tag.id, weight=1.0 if i == 0 else 0.5, is_primary=(i == 0)
        )
        db.add(pt)
        rows.append(pt)
    await db.flush()
    return rows


async def random_problem(db: AsyncSession, difficulty: str | None = None) -> Problem | None:
    stmt = select(Problem)
    if difficulty:
        stmt = stmt.where(Problem.difficulty == difficulty)
    stmt = stmt.order_by(func.random()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_tags(db: AsyncSession) -> list[Tag]:
    return list((await db.execute(select(Tag).order_by(Tag.name))).scalars().all())


# ============================================================== 提交与评测
async def create_submission(db: AsyncSession, data: dict[str, Any]) -> Submission:
    sub = Submission(
        user_id=data["user_id"],
        problem_id=data["problem_id"],
        language=data.get("language", "python"),
        code=data.get("code", ""),
        verdict=data.get("verdict", "PENDING"),
        passed_cases=int(data.get("passed_cases", 0)),
        total_cases=int(data.get("total_cases", 0)),
        runtime_ms=int(data.get("runtime_ms", 0)),
        memory_kb=int(data.get("memory_kb", 0)),
        failed_case=data.get("failed_case", ""),
        judge_detail=json.dumps(data.get("judge_detail", {}), ensure_ascii=False),
        idempotency_key=data.get("idempotency_key"),
        code_hash=data.get("code_hash", ""),
        judged_by=data.get("judged_by", "caller"),
    )
    db.add(sub)
    await db.flush()
    return sub


async def get_submission(db: AsyncSession, submission_id: str) -> Submission | None:
    return await db.get(Submission, submission_id)


async def list_submissions(
    db: AsyncSession,
    *,
    user_id: str,
    problem_id: str | None = None,
    verdict: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Submission], int]:
    stmt = select(Submission).where(Submission.user_id == user_id)
    if problem_id:
        stmt = stmt.where(Submission.problem_id == problem_id)
    if verdict:
        stmt = stmt.where(Submission.verdict == verdict)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Submission.created_at.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def recent_submissions(db: AsyncSession, user_id: str, limit: int = 200) -> list[Submission]:
    stmt = (
        select(Submission)
        .where(Submission.user_id == user_id)
        .order_by(Submission.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def problem_attempt_stats(db: AsyncSession, user_id: str, problem_id: str) -> dict[str, Any]:
    """该用户在某题上的尝试统计：次数 + 通过数 + 最近判定。

    用途：提示强度的"样本不足时保守升档"需要知道用户做过几次 —— 只做过
    1 次的题，掌握度数字本身不可信，此时给弱提示会让新手卡死。

    用聚合查询而不是取全部记录再数：题目历史提交可能很多，这里只需要标量。
    """
    total = (
        await db.execute(
            select(func.count())
            .select_from(Submission)
            .where(Submission.user_id == user_id, Submission.problem_id == problem_id)
        )
    ).scalar_one()
    ac_count = (
        await db.execute(
            select(func.count())
            .select_from(Submission)
            .where(
                Submission.user_id == user_id,
                Submission.problem_id == problem_id,
                Submission.verdict == "AC",
            )
        )
    ).scalar_one()
    last = (
        (
            await db.execute(
                select(Submission.verdict)
                .where(Submission.user_id == user_id, Submission.problem_id == problem_id)
                .order_by(Submission.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return {
        "attempts": int(total or 0),
        "ac_count": int(ac_count or 0),
        "last_verdict": str(last or ""),
        "solved": bool(ac_count),
    }


async def add_review(db: AsyncSession, data: dict[str, Any]) -> Review:
    review = Review(
        submission_id=data["submission_id"],
        kind=data.get("kind", "solution"),
        content=data.get("content", ""),
        structured=json.dumps(data.get("structured", {}), ensure_ascii=False),
        confidence=float(data.get("confidence", 0.0)),
        degraded=bool(data.get("degraded", False)),
        tokens=int(data.get("tokens", 0)),
        latency_ms=int(data.get("latency_ms", 0)),
    )
    db.add(review)
    await db.flush()
    return review


async def list_reviews(db: AsyncSession, submission_id: str) -> list[Review]:
    stmt = select(Review).where(Review.submission_id == submission_id).order_by(Review.created_at)
    return list((await db.execute(stmt)).scalars().all())


async def log_agent_run(db: AsyncSession, data: dict[str, Any]) -> AgentRun:
    """落库一次 Agent 执行，并同步记录可观测性指标。

    指标放在这里而不是各调用点：全项目有 8 处调用 log_agent_run，
    在此统一埋点可保证"不会漏记"，也避免每个接口各写一遍。
    """
    from .observability import record_agent_metric

    agent = data.get("agent", "unknown")
    status = data.get("status", "ok")
    prompt_tokens = int(data.get("prompt_tokens", 0))
    completion_tokens = int(data.get("completion_tokens", 0))

    run = AgentRun(
        task_id=data.get("task_id", ""),
        user_id=data.get("user_id", ""),
        agent=agent,
        provider=data.get("provider", ""),
        model=data.get("model", ""),
        status=status,
        degraded=bool(data.get("degraded", False)),
        reason=data.get("reason", "")[:120],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=float(data.get("cost_usd", 0.0)),
        latency_ms=int(data.get("latency_ms", 0)),
        cached=bool(data.get("cached", False)),
        output=json.dumps(data.get("output", {}), ensure_ascii=False)[:20000],
    )
    db.add(run)
    await db.flush()

    # 缓存命中不消耗 token，因此不计入 token 指标
    tokens = 0 if data.get("cached") else prompt_tokens + completion_tokens
    record_agent_metric(agent, status, tokens)
    return run


async def agent_run_stats(db: AsyncSession, user_id: str = "", days: int = 7) -> dict[str, Any]:
    cutoff = _now() - timedelta(days=days)
    stmt = select(AgentRun).where(AgentRun.created_at >= cutoff)
    if user_id:
        stmt = stmt.where(AgentRun.user_id == user_id)
    rows = list((await db.execute(stmt)).scalars().all())
    total = len(rows)
    degraded = sum(1 for r in rows if r.degraded)
    failed = sum(1 for r in rows if r.status == "failed")
    tokens = sum(r.prompt_tokens + r.completion_tokens for r in rows)
    latency = [r.latency_ms for r in rows if r.latency_ms > 0]
    latency.sort()
    p95 = latency[int(len(latency) * 0.95) - 1] if latency else 0
    by_agent: dict[str, dict[str, int]] = {}
    for r in rows:
        item = by_agent.setdefault(r.agent, {"calls": 0, "degraded": 0, "failed": 0})
        item["calls"] += 1
        item["degraded"] += int(r.degraded)
        item["failed"] += int(r.status == "failed")
    return {
        "window_days": days,
        "total_calls": total,
        "degraded": degraded,
        "failed": failed,
        "degraded_rate": round(degraded / total, 4) if total else 0.0,
        "failure_rate": round(failed / total, 4) if total else 0.0,
        "total_tokens": tokens,
        "avg_tokens_per_call": round(tokens / total, 1) if total else 0.0,
        "latency_p95_ms": p95,
        "by_agent": by_agent,
    }


# ============================================================== 掌握度画像
async def upsert_mastery_profiles(
    db: AsyncSession, user_id: str, tag_stats: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    existing_stmt = select(MasteryProfile).where(MasteryProfile.user_id == user_id)
    existing = {p.tag_id: p for p in (await db.execute(existing_stmt)).scalars().all()}
    rows = recalc_profile_rows(
        user_id, tag_stats, {k: {"score": v.score} for k, v in existing.items()}
    )
    updated: list[dict[str, Any]] = []
    for row in rows:
        prof = existing.get(row["tag_id"])
        if prof is None:
            prof = MasteryProfile(user_id=user_id, tag_id=row["tag_id"])
            db.add(prof)
        prof.score = row["score"]
        prof.confidence = row["confidence"]
        prof.attempts = row["attempts"]
        prof.ac_count = row["ac_count"]
        prof.last_practiced_at = _now()
        updated.append(row)
    await db.flush()
    return updated


async def get_mastery(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(MasteryProfile, Tag)
        .join(Tag, Tag.id == MasteryProfile.tag_id)
        .where(MasteryProfile.user_id == user_id)
        .order_by(MasteryProfile.score)
    )
    out: list[dict[str, Any]] = []
    for prof, tag in (await db.execute(stmt)).all():
        out.append(
            {
                "tag_id": tag.id,
                "tag": tag.name,
                "score": round(prof.score, 2),
                "confidence": round(prof.confidence, 4),
                "attempts": prof.attempts,
                "ac_count": prof.ac_count,
                "level": _level_of(prof.score),
                "last_practiced_at": _aware(prof.last_practiced_at),
            }
        )
    return out


def _level_of(score: float) -> str:
    for name, threshold in (("L5", 85), ("L4", 70), ("L3", 55), ("L2", 40)):
        if score >= threshold:
            return name
    return "L1"


async def mastery_scores_map(db: AsyncSession, user_id: str) -> dict[str, float]:
    rows = await get_mastery(db, user_id)
    return {r["tag"]: float(r["score"]) for r in rows}


async def _raw_tag_stats(
    db: AsyncSession, user_id: str, window_days: int = 30
) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    """取提交记录并聚合成 tag_id 维度的统计。

    返回 (tag_id -> stats, tag_id -> tag_name)。
    注意：统计必须按 tag_id 聚合（写 mastery_profiles 需要真外键），
    而 Agent 的 prompt 需要可读的标签名，所以这里同时返回映射。
    """
    subs = await recent_submissions(db, user_id, limit=500)
    if not subs:
        return {}, {}
    pids = list({s.problem_id for s in subs})
    pt_map = await get_problem_tags(db, pids)
    diff_map = await problem_difficulty_map(db, pids)
    tag_rows = {pid: [(t["id"], t["weight"]) for t in tags] for pid, tags in pt_map.items()}
    payload = [
        {
            "problem_id": s.problem_id,
            "verdict": s.verdict,
            "created_at": s.created_at,
            "weight": 1.0,
        }
        for s in subs
    ]
    stats = compute_tag_stats(payload, tag_rows, diff_map, window_days=window_days)
    id_to_name = {t.id: t.name for t in await list_tags(db)}
    return stats, id_to_name


async def build_tag_stats(db: AsyncSession, user_id: str, window_days: int = 30) -> dict[str, Any]:
    """标签统计（按标签名索引），供 Agent prompt 与规则引擎使用。"""
    stats, id_to_name = await _raw_tag_stats(db, user_id, window_days)
    return {id_to_name.get(tid, tid): st for tid, st in stats.items()}


async def build_tag_stats_by_id(
    db: AsyncSession, user_id: str, window_days: int = 30
) -> dict[str, dict[str, float]]:
    """标签统计（按 tag_id 索引），供 mastery_profiles 落库使用。"""
    stats, _ = await _raw_tag_stats(db, user_id, window_days)
    return stats


# ============================================================== 复习队列
async def upsert_review_item(
    db: AsyncSession, user_id: str, problem_id: str, rating: int
) -> dict[str, Any]:
    stmt = select(ReviewQueue).where(
        ReviewQueue.user_id == user_id, ReviewQueue.problem_id == problem_id
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        item = ReviewQueue(user_id=user_id, problem_id=problem_id)
        db.add(item)
        await db.flush()

    plan = fsrs.schedule(
        stability=item.stability,
        difficulty=item.difficulty_fsrs,
        reps=item.reps,
        lapses=item.lapses,
        last_review_at=item.last_review_at,
        rating=rating,
    )
    item.stability = plan["stability"]
    item.difficulty_fsrs = plan["difficulty_fsrs"]
    item.retrievability = plan["retrievability"]
    item.interval_days = plan["interval_days"]
    item.reps = plan["reps"]
    item.lapses = plan["lapses"]
    item.last_review_at = plan["last_review_at"]
    item.due_at = plan["due_at"]
    await db.flush()
    return {
        "problem_id": problem_id,
        "rating": rating,
        "stability": item.stability,
        "difficulty": item.difficulty_fsrs,
        "retrievability": item.retrievability,
        "interval_days": item.interval_days,
        "due_at": item.due_at,
        "reps": item.reps,
        "lapses": item.lapses,
    }


async def get_review_queue(
    db: AsyncSession, user_id: str, *, due_only: bool = False, limit: int = 50
) -> list[dict[str, Any]]:
    """复习队列。

    两处口径统一到 core.fsrs，避免多处手写导致漂移（技术债 D4）：
      * 是否到期 -> fsrs.is_due()
      * 逾期天数 -> fsrs.overdue_days()

    due_only 时在 SQL 层过滤 due_at <= now，而不是取出后再筛 —— 否则
    "取前 N 条再过滤"会因为未到期的条目占满 limit 而漏掉真正逾期的题。
    """
    now = _now()
    stmt = select(ReviewQueue, Problem).join(Problem, Problem.id == ReviewQueue.problem_id)
    stmt = stmt.where(ReviewQueue.user_id == user_id)
    if due_only:
        stmt = stmt.where(ReviewQueue.due_at <= now)
    # 逾期最久的排最前
    stmt = stmt.order_by(ReviewQueue.due_at.asc()).limit(limit)

    out: list[dict[str, Any]] = []
    for item, problem in (await db.execute(stmt)).all():
        due = _aware(item.due_at)
        overdue = fsrs.overdue_days(due, now)
        out.append(
            {
                "problem_id": problem.id,
                "title": problem.title,
                "difficulty": problem.difficulty,
                "stability": round(item.stability, 4),
                "difficulty_fsrs": round(item.difficulty_fsrs, 4),
                "retrievability": round(fsrs.retrievability(item.stability, overdue), 4),
                "interval_days": round(item.interval_days, 2),
                "reps": item.reps,
                "lapses": item.lapses,
                "due_at": due,
                "is_due": fsrs.is_due(due, now),
                "overdue_days": round(overdue, 2),
            }
        )
    return out


async def review_forecast(db: AsyncSession, user_id: str, days: int = 30) -> list[dict[str, Any]]:
    """未来 days 天每日待复习数量，供前端画柱状图。"""
    items = await get_review_queue(db, user_id, limit=1000)
    now = _now()
    buckets: dict[str, int] = {}
    for i in range(days + 1):
        buckets[(now + timedelta(days=i)).date().isoformat()] = 0
    for it in items:
        due = it["due_at"]
        if due is None:
            continue
        d = due.date().isoformat()
        if d in buckets:
            buckets[d] += 1
        elif due < now:
            buckets[now.date().isoformat()] += 1
    return [{"date": k, "count": v} for k, v in sorted(buckets.items())]


# ================================================================ 错题本
async def upsert_wrong_book(
    db: AsyncSession, user_id: str, problem_id: str, verdict: str, root_cause: str = ""
) -> WrongBook:
    stmt = select(WrongBook).where(WrongBook.user_id == user_id, WrongBook.problem_id == problem_id)
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        item = WrongBook(user_id=user_id, problem_id=problem_id, wrong_count=0)
        db.add(item)
    item.wrong_count += 1
    item.last_verdict = verdict
    item.last_wrong_at = _now()
    if root_cause:
        item.root_cause = root_cause
    await db.flush()
    return item


async def list_wrong_book(
    db: AsyncSession, user_id: str, *, mastered: bool | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = (
        select(WrongBook, Problem)
        .join(Problem, Problem.id == WrongBook.problem_id)
        .where(WrongBook.user_id == user_id)
        .order_by(WrongBook.wrong_count.desc(), WrongBook.last_wrong_at.desc())
        .limit(limit)
    )
    if mastered is not None:
        stmt = stmt.where(WrongBook.mastered == mastered)
    out: list[dict[str, Any]] = []
    for wb, problem in (await db.execute(stmt)).all():
        out.append(
            {
                "problem_id": problem.id,
                "title": problem.title,
                "difficulty": problem.difficulty,
                "wrong_count": wb.wrong_count,
                "last_verdict": wb.last_verdict,
                "root_cause": wb.root_cause,
                "mastered": wb.mastered,
                "note": wb.note,
                "last_wrong_at": _aware(wb.last_wrong_at),
            }
        )
    return out


async def mark_wrong_mastered(
    db: AsyncSession, user_id: str, problem_id: str, mastered: bool = True
) -> bool:
    stmt = (
        update(WrongBook)
        .where(WrongBook.user_id == user_id, WrongBook.problem_id == problem_id)
        .values(mastered=mastered, updated_at=_now())
    )
    result = await db.execute(stmt)
    return result.rowcount > 0


async def cluster_errors(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    """易错题聚类：按标签组合做指纹聚类，无需向量库。

    指纹 = 排序后的标签名 join('+')，同一组合的错题归为一簇。
    这是 BM25/向量检索之外的第三条路：对"错题模式"这种强结构化数据，
    标签组合指纹比语义相似度更准且零成本。
    """
    wrongs = await list_wrong_book(db, user_id, mastered=False, limit=200)
    if not wrongs:
        return []
    pids = [w["problem_id"] for w in wrongs]
    pt_map = await get_problem_tags(db, pids)
    groups: dict[str, dict[str, Any]] = {}
    for w in wrongs:
        tags = sorted(t["name"] for t in pt_map.get(w["problem_id"], []))
        key = "+".join(tags) if tags else "未分类"
        g = groups.setdefault(
            key,
            {
                "cluster_key": key,
                "tag_ids": tags,
                "problem_ids": [],
                "wrong_count": 0,
                "causes": {},
            },
        )
        g["problem_ids"].append(w["problem_id"])
        g["wrong_count"] += w["wrong_count"]
        cause = w["root_cause"] or "未知"
        g["causes"][cause] = g["causes"].get(cause, 0) + 1

    # 清理旧簇并写入新簇
    await db.execute(delete(ErrorCluster).where(ErrorCluster.user_id == user_id))
    out: list[dict[str, Any]] = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["wrong_count"]):
        top_cause = max(g["causes"].items(), key=lambda kv: kv[1])[0] if g["causes"] else "未知"
        severity = round(g["wrong_count"] * (1.0 + 0.2 * len(g["problem_ids"])), 2)
        suggestion = f"该组合共 {len(g['problem_ids'])} 题、{g['wrong_count']} 次错误，主要归因「{top_cause}」。"
        cluster = ErrorCluster(
            user_id=user_id,
            cluster_key=key,
            label=f"{key} 薄弱簇",
            problem_ids=json.dumps(g["problem_ids"], ensure_ascii=False),
            tag_ids=json.dumps(g["tag_ids"], ensure_ascii=False),
            size=len(g["problem_ids"]),
            severity=severity,
            suggestion=suggestion,
        )
        db.add(cluster)
        out.append(
            {
                "cluster_key": key,
                "label": cluster.label,
                "problem_ids": g["problem_ids"],
                "tags": g["tag_ids"],
                "size": len(g["problem_ids"]),
                "severity": severity,
                "top_cause": top_cause,
                "suggestion": suggestion,
            }
        )
    await db.flush()
    return out


async def list_clusters(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(ErrorCluster)
        .where(ErrorCluster.user_id == user_id)
        .order_by(ErrorCluster.severity.desc())
    )
    return [
        {
            "cluster_key": c.cluster_key,
            "label": c.label,
            "problem_ids": json.loads(c.problem_ids or "[]"),
            "tags": json.loads(c.tag_ids or "[]"),
            "size": c.size,
            "severity": round(c.severity, 2),
            "suggestion": c.suggestion,
        }
        for c in (await db.execute(stmt)).scalars().all()
    ]


# ============================================================ 学习状态快照
async def compute_and_store_state(
    db: AsyncSession, user_id: str, window_days: int = 30, source: str = "auto"
) -> dict[str, Any]:
    tag_stats = await build_tag_stats(db, user_id, window_days)
    summary = compute_score(tag_stats, window_days)
    state = LearningState(
        user_id=user_id,
        score=summary["score"],
        level=summary["level"],
        confidence=summary["confidence"],
        window_days=window_days,
        metrics=json.dumps(summary["metrics"], ensure_ascii=False),
        source=source,
    )
    db.add(state)
    await db.flush()
    return {
        "score": summary["score"],
        "level": summary["level"],
        "confidence": summary["confidence"],
        "weak_tags": summary["weak_tags"],
        "strong_tags": summary["strong_tags"],
        "metrics": summary["metrics"],
        "window_days": window_days,
        "created_at": _aware(state.created_at),
    }


async def state_timeline(db: AsyncSession, user_id: str, limit: int = 60) -> list[dict[str, Any]]:
    stmt = (
        select(LearningState)
        .where(LearningState.user_id == user_id)
        .order_by(LearningState.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "score": round(s.score, 2),
            "level": s.level,
            "confidence": round(s.confidence, 4),
            "source": s.source,
            "created_at": _aware(s.created_at),
            "metrics": json.loads(s.metrics or "{}"),
        }
        for s in reversed(rows)
    ]


# ============================================================ 后台任务表
async def persist_task(db: AsyncSession, data: dict[str, Any]) -> BackgroundTask:
    """写入/更新后台任务记录（幂等 upsert）。

    为什么不能"先 db.get 再决定 insert/update"：
    压测暴露了这个竞态 —— 同一任务的 pending 与 running 两次写入几乎同时到达，
    两个协程都 get 不到记录，于是各自 INSERT，第二个撞上
    "UNIQUE constraint failed: background_tasks.id"。

    正确做法：用数据库的 upsert 语义（INSERT ... ON CONFLICT DO UPDATE），
    把"存在则更新"交给数据库原子完成，而不是应用层先查后写。
    """
    task_id = data.get("id", "")
    if not task_id:
        # 没有 id 时无法 upsert，退回普通插入（调用方应始终传 id）
        task = _build_task(data)
        db.add(task)
        await db.flush()
        return task

    # 取方言名：AsyncSession.bind 可能为 None（连接由 engine 管理），
    # 因此回退到 session 上的 get_bind()，再退到"按 SQLite 处理"。
    dialect = "sqlite"
    try:
        bind = db.get_bind()
        if bind is not None:
            dialect = bind.dialect.name
    except Exception:
        pass

    if dialect == "sqlite":
        await _upsert_task_sqlite(db, task_id, data)
    else:
        # 其他数据库（MySQL/Postgres）：先更新、未命中再插入并容忍并发冲突。
        await _upsert_task_generic(db, task_id, data)

    await db.flush()
    row = await db.get(BackgroundTask, task_id)
    return row if row is not None else _build_task(data)


async def _upsert_task_sqlite(db: AsyncSession, task_id: str, data: dict[str, Any]) -> None:
    """SQLite 原生 upsert：把"存在则更新"交给数据库原子完成。"""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    values = _task_values(task_id, data)
    stmt = sqlite_insert(BackgroundTask).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BackgroundTask.id],
        set_={
            "status": stmt.excluded.status,
            "progress": stmt.excluded.progress,
            "stage": stmt.excluded.stage,
            "cancel_requested": stmt.excluded.cancel_requested,
            "payload": stmt.excluded.payload,
            "result": stmt.excluded.result,
            "error": stmt.excluded.error,
            "updated_at": stmt.excluded.updated_at,
            "finished_at": stmt.excluded.finished_at,
        },
    )
    await db.execute(stmt)


async def _upsert_task_generic(db: AsyncSession, task_id: str, data: dict[str, Any]) -> None:
    """通用路径：先尝试 UPDATE，未命中则 INSERT，并容忍并发冲突。"""
    from sqlalchemy import update

    values = _task_values(task_id, data)
    values.pop("id", None)
    values.pop("created_at", None)

    result = await db.execute(
        update(BackgroundTask).where(BackgroundTask.id == task_id).values(**values)
    )
    if result.rowcount:
        return

    task = _build_task(data)
    db.add(task)
    try:
        await db.flush()
    except Exception:
        # 并发下另一个事务已插入：回滚这次 flush 后改为更新
        await db.rollback()
        await db.execute(
            update(BackgroundTask).where(BackgroundTask.id == task_id).values(**values)
        )


def _task_values(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task_id,
        "user_id": data.get("user_id", ""),
        "kind": data.get("kind", "agent_pipeline"),
        "status": data.get("status", "pending"),
        "progress": float(data.get("progress", 0.0)),
        "stage": data.get("stage", ""),
        "cancel_requested": bool(data.get("cancel_requested", False)),
        "payload": json.dumps(data.get("payload", {}), ensure_ascii=False),
        "result": json.dumps(data.get("result", {}), ensure_ascii=False),
        "error": data.get("error", ""),
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": _now() if data.get("status") in ("done", "failed", "cancelled") else None,
    }


def _build_task(data: dict[str, Any]) -> BackgroundTask:
    return BackgroundTask(
        id=data.get("id", ""),
        user_id=data.get("user_id", ""),
        kind=data.get("kind", "agent_pipeline"),
        status=data.get("status", "pending"),
        progress=float(data.get("progress", 0.0)),
        stage=data.get("stage", ""),
        cancel_requested=bool(data.get("cancel_requested", False)),
        payload=json.dumps(data.get("payload", {}), ensure_ascii=False),
        result=json.dumps(data.get("result", {}), ensure_ascii=False),
        error=data.get("error", ""),
    )


async def list_persisted_tasks(
    db: AsyncSession, user_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = (
        select(BackgroundTask)
        .where(BackgroundTask.user_id == user_id)
        .order_by(BackgroundTask.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "task_id": t.id,
            "kind": t.kind,
            "status": t.status,
            "progress": round(t.progress, 4),
            "stage": t.stage,
            "error": t.error,
            "created_at": _aware(t.created_at),
            "finished_at": _aware(t.finished_at),
        }
        for t in (await db.execute(stmt)).scalars().all()
    ]


async def reconcile_tasks() -> int:
    """启动对账：把上次进程遗留的 running/pending 任务标记为 interrupted。

    问题（清单 N5）：任务状态在内存态与 background_tasks 表各存一份。
    进程被 kill / 崩溃 / 重启时，内存态没了，但表里仍是 running，
    前端会永远转圈。启动时做一次对账即可根治。

    注意 interrupted 是我们新增的终态：与 cancelled 区分开 —— 前者是
    "被中断、结果未知"，后者是"用户主动取消"。
    """
    async with session_scope() as db:
        stmt = select(BackgroundTask).where(BackgroundTask.status.in_(("pending", "running")))
        rows = list((await db.execute(stmt)).scalars().all())
        for row in rows:
            row.status = "interrupted"
            row.error = (row.error or "") + "[启动对账] 进程重启，任务未完成"
            row.finished_at = _now()
            row.updated_at = _now()
        await db.flush()
        return len(rows)


# ================================================================ 数据导入导出
async def export_user_data(db: AsyncSession, user_id: str) -> dict[str, Any]:
    mastery = await get_mastery(db, user_id)
    wrongs = await list_wrong_book(db, user_id, limit=500)
    queue = await get_review_queue(db, user_id, limit=500)
    timeline = await state_timeline(db, user_id, limit=100)
    subs, _ = await list_submissions(db, user_id=user_id, limit=500)
    return {
        "user_id": user_id,
        "exported_at": _now().isoformat(),
        "mastery": mastery,
        "wrong_book": wrongs,
        "review_queue": queue,
        "state_timeline": timeline,
        "submissions": [
            {
                "id": s.id,
                "problem_id": s.problem_id,
                "verdict": s.verdict,
                "language": s.language,
                "created_at": _aware(s.created_at),
            }
            for s in subs
        ],
    }
