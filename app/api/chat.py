"""⑪ 对话式辅导：多轮会话 + 强度治理 + SSE 流式。

与单发提示（/api/problems/{id}/hint）的分工见 core/chat.py 模块注释。
安全：所有会话接口校验属主（非属主 404，不泄露存在性）—— 任务接口
越权修复后的同一范式。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.chat import (
    ChatTurn,
    CoachContext,
    coach_reply,
    coach_reply_stream,
    initial_strength,
    next_strength,
)
from ..core.memory import memory_block_for
from ..core.tasks import sse_format
from ..db import session_scope
from ..deps import DbSession, ModelConfigDep, UserDep
from ..models import ChatMessage, ChatSession
from ..services import (
    get_problem,
    get_problem_tags,
    mastery_scores_map,
    problem_attempt_stats,
)

router = APIRouter(prefix="/api/chat", tags=["⑪ 对话式辅导"])


class SessionCreateIn(BaseModel):
    problem_id: str = Field(default="", max_length=32, description="绑定的题目（可空）")


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


def _session_out(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "problem_id": s.problem_id,
        "title": s.title,
        "strength": s.strength,
        "message_count": s.message_count,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _msg_out(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "strength": m.strength,
        "degraded": m.degraded,
        "created_at": m.created_at.isoformat(),
    }


async def _own_session(session_id: str, user_id: str, db) -> ChatSession:
    """取会话并校验属主（非属主与不存在同返 404）。"""
    s = await db.get(ChatSession, session_id)
    if s is None or s.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


# ------------------------------------------------------------------ 会话 CRUD
@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreateIn, db: DbSession, user: UserDep):
    """创建辅导会话（可选绑定题目；绑定时强度初始值由该题掌握度算定）。"""
    problem = None
    title = "自由辅导"
    strength = ""
    if body.problem_id:
        problem = await get_problem(db, body.problem_id)
        if problem is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
        title = problem.title[:200]
        tag_map = await get_problem_tags(db, [problem.id])
        tags = [t["name"] for t in tag_map.get(problem.id, [])]
        mastery = await mastery_scores_map(db, user.id)
        weakest = min(float(mastery.get(t, 50.0)) for t in tags) if tags else 50.0
        stats = await problem_attempt_stats(db, user.id, problem.id)
        strength = initial_strength(weakest, stats["attempts"])

    s = ChatSession(user_id=user.id, problem_id=body.problem_id, title=title, strength=strength)
    db.add(s)
    await db.flush()
    return _session_out(s)


@router.get("/sessions")
async def list_sessions(db: DbSession, user: UserDep, limit: int = Query(default=20, ge=1, le=100)):
    rows = (
        (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_session_out(s) for s in rows]}


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, db: DbSession, user: UserDep):
    """会话详情 + 全部消息（按时间正序）。"""
    s = await _own_session(session_id, user.id, db)
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
    return {"session": _session_out(s), "messages": [_msg_out(m) for m in msgs]}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db: DbSession, user: UserDep):
    s = await _own_session(session_id, user.id, db)
    await db.delete(s)
    await db.flush()
    return {"ok": True}


# ------------------------------------------------------------------ 消息
async def _build_context(db, user_id: str, s: ChatSession) -> CoachContext:
    """组装辅导上下文：题面 + 记忆 + 历史轮次（引擎不碰库，这里备齐）。"""
    problem: dict = {}
    mastery_score, attempts = 50.0, 0
    if s.problem_id:
        p = await get_problem(db, s.problem_id)
        if p is not None:
            tag_map = await get_problem_tags(db, [p.id])
            tags = [t["name"] for t in tag_map.get(p.id, [])]
            mastery = await mastery_scores_map(db, user_id)
            mastery_score = min(float(mastery.get(t, 50.0)) for t in tags) if tags else 50.0
            stats = await problem_attempt_stats(db, user_id, p.id)
            attempts = stats["attempts"]
            problem = {
                "title": p.title,
                "statement": p.statement,
                "tags": tags,
                "difficulty": p.difficulty,
            }

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
    history = [
        ChatTurn(role=m.role, content=m.content, strength=m.strength)
        for m in reversed(history_rows)
    ]
    memory = await memory_block_for(db, user_id)
    return CoachContext(
        problem=problem,
        memory=memory,
        mastery_score=mastery_score,
        attempts=attempts,
        history=history,
    )


async def _append_message(
    db, session: ChatSession, role: str, content: str, strength: str, degraded: bool
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session.id, role=role, content=content, strength=strength, degraded=degraded
    )
    db.add(msg)
    session.message_count += 1
    session.strength = strength or session.strength
    await db.flush()
    return msg


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageIn,
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    stream: bool = Query(default=False, description="true 时返回 SSE 流"),
):
    """发一条消息并获取教练回复。

    非流式返回 JSON；流式返回 SSE（open -> token* -> done），
    done.data 携带完整消息（含强度档与降级标记），前端总能拿到终态。
    """
    s = await _own_session(session_id, user.id, db)
    new_strength = next_strength(s.strength or "key_observation", body.content)
    ctx = await _build_context(db, user.id, s)

    if not stream:
        await _append_message(db, s, "user", body.content.strip(), "", False)
        reply = await coach_reply(cfg, ctx, body.content, new_strength)
        msg = await _append_message(db, s, "assistant", reply.text, reply.strength, reply.degraded)
        return {
            "message": _msg_out(msg),
            "strength": reply.strength,
            "degraded": reply.degraded,
            "reason": reply.reason,
        }

    async def gen():
        yield sse_format({"event": "open", "data": {"session_id": s.id, "strength": new_strength}})
        final = None
        async for piece in coach_reply_stream(cfg, ctx, body.content, new_strength):
            if isinstance(piece, str):
                yield sse_format({"event": "token", "data": {"text": piece}})
            else:
                final = piece
        if final is None:  # 引擎异常未产出终态时的兜底
            from ..core.chat import rule_reply

            final = type(
                "R",
                (),
                {
                    "text": rule_reply(ctx, body.content, new_strength),
                    "strength": new_strength,
                    "degraded": True,
                    "reason": "stream-no-final",
                },
            )()
        # 流式路径的所有写入合并到这一个事务：外层请求 session 在 SSE 期间
        # 持有未提交写入，再开第二个写事务会撞 SQLite 单写锁（实测
        # database is locked）。用户消息 + 助教回复一起落，请求事务只读。
        async with session_scope() as sdb:
            fresh = await sdb.get(ChatSession, s.id)
            if fresh is not None:
                sdb.add(
                    ChatMessage(
                        session_id=s.id,
                        role="user",
                        content=body.content.strip(),
                    )
                )
                sdb.add(
                    ChatMessage(
                        session_id=s.id,
                        role="assistant",
                        content=final.text,
                        strength=final.strength,
                        degraded=bool(final.degraded),
                    )
                )
                fresh.message_count += 2
                fresh.strength = final.strength or fresh.strength
        yield sse_format(
            {
                "event": "done",
                "data": {
                    "reply": final.text,
                    "strength": final.strength,
                    "degraded": bool(final.degraded),
                },
            }
        )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
