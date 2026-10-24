"""⑫ 模拟面试：单题限时技术面 + 结构化复盘报告。

复用 chat_sessions/chat_messages（kind=interview 区分），新增
interview_reports。评分 Rubric 见 core/interview.py —— 分数全部规则
算定，LLM 只写叙事（可降级）。
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.chat import ChatTurn
from ..core.interview import (
    INTERVIEW_INITIAL_STRENGTH,
    compute_scores,
    elapsed_seconds_of,
    generate_narrative,
    hint_request_flag,
    interview_messages,
    parse_meta,
)
from ..core.interview import build_interview_context as _ctx
from ..core.memory import memory_block_for
from ..core.providers import build_provider
from ..deps import DbSession, ModelConfigDep, UserDep
from ..models import ChatMessage, ChatSession, InterviewReport
from ..services import get_problem, get_problem_tags

router = APIRouter(prefix="/api/interview", tags=["⑫ 模拟面试"])


class StartIn(BaseModel):
    problem_id: str = Field(min_length=1, max_length=32)
    time_limit_minutes: int = Field(default=30, ge=5, le=90)


class InterviewMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    ask_hint: bool = False  # 显式索要提示：升一档并计入提示依赖


class SubmitIn(BaseModel):
    language: str = Field(default="python", max_length=16)
    code: str = Field(min_length=1, max_length=20000)


def _session_out(s: ChatSession, meta: dict) -> dict:
    return {
        "id": s.id,
        "problem_id": s.problem_id,
        "title": s.title,
        "kind": s.kind,
        "time_limit_minutes": s.time_limit_minutes,
        "message_count": s.message_count,
        "status": meta.get("status", "ongoing"),
        "hint_requests": meta.get("hint_requests", 0),
        "submission_ids": meta.get("submission_ids", []),
        "elapsed_seconds": round(elapsed_seconds_of(meta), 1),
        "created_at": s.created_at.isoformat(),
    }


async def _own_interview(session_id: str, user_id: str, db) -> ChatSession:
    s = await db.get(ChatSession, session_id)
    if s is None or s.user_id != user_id or s.kind != "interview":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "面试场次不存在")
    return s


def _meta_of(s: ChatSession) -> dict:
    return parse_meta(s.meta)


async def _save_meta(db, s: ChatSession, meta: dict) -> None:
    s.meta = json.dumps(meta, ensure_ascii=False)
    await db.flush()


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_interview(body: StartIn, db: DbSession, user: UserDep):
    """开一场单题模拟面：校验题目存在，初始提示档 direction（默认不提示）。"""
    problem = await get_problem(db, body.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    meta = {
        "status": "ongoing",
        "started_at": time.time(),
        "hint_requests": 0,
        "submission_ids": [],
    }
    s = ChatSession(
        user_id=user.id,
        problem_id=problem.id,
        title=f"模拟面 · {problem.title}"[:200],
        kind="interview",
        time_limit_minutes=body.time_limit_minutes,
        strength=INTERVIEW_INITIAL_STRENGTH,
        meta=json.dumps(meta, ensure_ascii=False),
    )
    db.add(s)
    await db.flush()
    return _session_out(s, meta)


@router.get("")
async def list_interviews(
    db: DbSession, user: UserDep, limit: int = Query(default=20, ge=1, le=100)
):
    rows = (
        (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user.id, ChatSession.kind == "interview")
                .order_by(ChatSession.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_session_out(s, _meta_of(s)) for s in rows]}


@router.get("/{session_id}/report")
async def get_report(session_id: str, db: DbSession, user: UserDep):
    s = await _own_interview(session_id, user.id, db)
    report = (
        await db.execute(select(InterviewReport).where(InterviewReport.session_id == s.id))
    ).scalar_one_or_none()
    if report is None:
        return {"report": None, "session": _session_out(s, _meta_of(s))}
    return {
        "session": _session_out(s, _meta_of(s)),
        "report": {
            "verdict": report.verdict,
            "scores": json.loads(report.scores or "{}"),
            "narrative": report.narrative,
            "degraded": report.degraded,
            "created_at": report.created_at.isoformat(),
        },
    }


@router.post("/{session_id}/message")
async def interview_message(
    session_id: str, body: InterviewMessageIn, db: DbSession, user: UserDep, cfg: ModelConfigDep
):
    """与考官对话。索要提示时提示档升一档并计数（报告的提示依赖口径）。"""
    s = await _own_interview(session_id, user.id, db)
    meta = _meta_of(s)
    if meta.get("status") != "ongoing":
        raise HTTPException(status.HTTP_409_CONFLICT, "本场面试已结束")
    # 到时自动收卷
    if s.time_limit_minutes > 0 and elapsed_seconds_of(meta) >= s.time_limit_minutes * 60:
        meta["status"] = "time_up"
        await _save_meta(db, s, meta)
        raise HTTPException(status.HTTP_409_CONFLICT, "已到时限，请调用 finish 生成报告")

    problem = await get_problem(db, s.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    tag_map = await get_problem_tags(db, [problem.id])
    history_rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == s.id)
                .order_by(ChatMessage.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    ctx = _ctx(
        {
            "title": problem.title,
            "statement": problem.statement,
            "tags": [t["name"] for t in tag_map.get(problem.id, [])],
            "difficulty": problem.difficulty,
        },
        await memory_block_for(db, user.id),
        [ChatTurn(role=m.role, content=m.content) for m in reversed(history_rows)],
    )
    # 考官必须知道判题事实才能有效追问（此前只存 meta 不进上下文，
    # 考官对 WA/AC 一无所知，追问只能盲猜）
    sub_ids = meta.get("submission_ids") or []
    if sub_ids:
        from ..models import Submission

        facts = []
        for i, sub_id in enumerate(sub_ids[-3:], 1):
            sub = await db.get(Submission, sub_id)
            if sub is not None:
                facts.append(
                    f"第 {i} 次提交：{sub.verdict}（{sub.passed_cases}/{sub.total_cases}，"
                    f"{sub.language}）"
                )
        ctx.extra = "最近判题：" + "；".join(facts)

    messages = interview_messages(ctx, body.content, s.strength)
    from ..core.chat import STRENGTH_ORDER, rule_reply

    provider_str = str(cfg.get("provider", "mock"))
    if provider_str == "mock":
        reply_text, degraded = rule_reply(ctx, body.content, s.strength), True
        # 考官语境下规则模板措辞不合适，改用面试化的模板
        reply_text = _interviewer_rule_reply(body.content)
    else:
        provider = build_provider(
            str(cfg.get("provider", "")), str(cfg.get("base_url", "")), str(cfg.get("api_key", ""))
        )
        try:
            result = await provider.complete_chat(
                messages, str(cfg.get("model", "")), temperature=float(cfg.get("temperature", 0.3))
            )
            reply_text = (result.text or "").strip() or "(考官沉默，请继续)"
            degraded = False
        except Exception:
            reply_text, degraded = _interviewer_rule_reply(body.content), True

    if body.ask_hint and not hint_request_flag(reply_text):
        reply_text = "(提示) " + reply_text
        hint_request_flag_marker = True
    else:
        hint_request_flag_marker = hint_request_flag(reply_text)
    if hint_request_flag_marker:
        meta["hint_requests"] = int(meta.get("hint_requests", 0)) + 1
        if body.ask_hint:
            idx = STRENGTH_ORDER.index(s.strength) if s.strength in STRENGTH_ORDER else 0
            s.strength = STRENGTH_ORDER[min(idx + 1, len(STRENGTH_ORDER) - 1)]

    db.add(ChatMessage(session_id=s.id, role="user", content=body.content.strip()))
    db.add(
        ChatMessage(
            session_id=s.id,
            role="assistant",
            content=reply_text,
            strength=s.strength,
            degraded=degraded,
        )
    )
    s.message_count += 2
    await _save_meta(db, s, meta)
    await db.flush()
    return {
        "reply": reply_text,
        "degraded": degraded,
        "strength": s.strength,
        "hint_requests": meta["hint_requests"],
        "elapsed_seconds": round(elapsed_seconds_of(meta), 1),
        "time_limit_minutes": s.time_limit_minutes,
    }


def _interviewer_rule_reply(user_message: str) -> str:
    """考官的规则模板（mock/降级）：追问思路而非教学。"""
    return (
        "（规则考官）请先讲讲你的整体思路：打算用什么数据结构、怎么处理输入、"
        "核心循环里维护什么状态？讲清楚这三点我们再看代码。"
    )


@router.post("/{session_id}/submit")
async def submit_code(
    session_id: str, body: SubmitIn, db: DbSession, user: UserDep, cfg: ModelConfigDep
):
    """面试中提交代码：真实沙箱判题并记录（考官在后续对话中可见结果）。"""
    s = await _own_interview(session_id, user.id, db)
    meta = _meta_of(s)
    if meta.get("status") != "ongoing":
        raise HTTPException(status.HTTP_409_CONFLICT, "本场面试已结束")
    # 与 message 同一道时间门：到时后不得再交代码刷正确性分
    if s.time_limit_minutes > 0 and elapsed_seconds_of(meta) >= s.time_limit_minutes * 60:
        meta["status"] = "time_up"
        await _save_meta(db, s, meta)
        raise HTTPException(status.HTTP_409_CONFLICT, "已到时限，请调用 finish 生成报告")
    problem = await get_problem(db, s.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")

    from ..judge_service import judge_submission_in_sandbox
    from ..services import create_submission

    fields = await judge_submission_in_sandbox(db, problem, body.language, body.code)
    sub = await create_submission(
        db,
        {
            "user_id": user.id,
            "problem_id": s.problem_id,
            "language": body.language,
            "code": body.code,
            "verdict": fields["verdict"],
            "passed_cases": fields["passed_cases"],
            "total_cases": fields["total_cases"],
            "runtime_ms": fields["runtime_ms"],
            "memory_kb": fields["memory_kb"],
            "failed_case": fields.get("failed_case", ""),
            "judge_detail": fields.get("judge_detail", {}),
        },
    )
    meta.setdefault("submission_ids", []).append(sub.id)
    meta["last_verdict"] = fields["verdict"]
    await _save_meta(db, s, meta)
    return {
        "submission_id": sub.id,
        "verdict": fields["verdict"],
        "passed": fields["passed_cases"],
        "total": fields["total_cases"],
        "runtime_ms": fields["runtime_ms"],
    }


@router.post("/{session_id}/finish")
async def finish_interview(session_id: str, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """结束面试并生成复盘报告（分数规则算定 + LLM 叙事可降级）。幂等。"""
    s = await _own_interview(session_id, user.id, db)
    meta = _meta_of(s)
    existing = (
        await db.execute(select(InterviewReport).where(InterviewReport.session_id == s.id))
    ).scalar_one_or_none()
    if existing is not None:
        return {"ok": True, "already": True, "report_id": existing.id}

    meta["status"] = meta.get("status") if meta.get("status") == "time_up" else "finished"
    meta["finished_at"] = time.time()
    history_rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == s.id)
                .order_by(ChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    history = [ChatTurn(role=m.role, content=m.content) for m in history_rows]

    # 最终判题：取该场最后一次提交
    verdict, passed, total = "", 0, 0
    sub_ids = meta.get("submission_ids") or []
    if sub_ids:
        from ..models import Submission

        last = await db.get(Submission, sub_ids[-1])
        if last is not None:
            verdict, passed, total = last.verdict, last.passed_cases, last.total_cases

    # 代码质量规则基线：取最后一次提交的代码
    quality_baseline = 50.0
    if sub_ids:
        from ..core.rules import quality_window
        from ..models import Submission

        last = await db.get(Submission, sub_ids[-1])
        if last is not None and last.code:
            quality_baseline, _metrics = quality_window(last.code)

    scores = compute_scores(
        verdict=verdict,
        passed=passed,
        total=total,
        quality_baseline=quality_baseline,
        hint_requests=int(meta.get("hint_requests", 0)),
        elapsed_seconds=elapsed_seconds_of(meta),
        limit_minutes=s.time_limit_minutes,
        history=history,
    )
    narrative, degraded = await generate_narrative(cfg, scores, verdict, history)

    report = InterviewReport(
        session_id=s.id,
        user_id=user.id,
        problem_id=s.problem_id or "",
        verdict=verdict,
        scores=json.dumps(scores, ensure_ascii=False),
        narrative=narrative,
        degraded=degraded,
    )
    db.add(report)
    await _save_meta(db, s, meta)
    await db.flush()
    return {
        "ok": True,
        "report_id": report.id,
        "scores": scores,
        "narrative": narrative,
        "degraded": degraded,
    }


@router.get("/{session_id}")
async def interview_detail(session_id: str, db: DbSession, user: UserDep):
    s = await _own_interview(session_id, user.id, db)
    msgs = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == s.id)
                .order_by(ChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "session": _session_out(s, _meta_of(s)),
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "strength": m.strength,
                "degraded": m.degraded,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }
