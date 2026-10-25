"""成长体系回归：XP 事实派生、等级曲线、成就纯函数、断签保护、API 契约。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.gamification import (
    XP_PER_AC,
    XP_PER_CHECKIN_DAY,
    XP_PER_SUBMISSION,
    compute_xp,
    evaluate_achievements,
    level_of,
    next_level_xp,
    streak_with_freeze,
)
from app.db import session_scope
from app.models import ChatSession, DailyCheckin, InterviewReport, Submission
from app.services import create_problem


@pytest.fixture
async def gm_user(client):
    name = f"gm{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/api/auth/register", json={"username": name, "password": "test123456", "nickname": "gm"}
    )
    token = r.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    return client, token, me.json()["id"]


async def _seed_submissions(uid: str, *, total: int, ac: int, langs: tuple[str, ...] = ("python",)):
    async with session_scope() as db:
        p = await create_problem(db, {"title": f"XP题{uid[:4]}", "statement": "s"})
        pid = p.id
        for i in range(total):
            verdict = "AC" if i < ac else "WA"
            lang = langs[i % len(langs)]
            db.add(
                Submission(
                    user_id=uid,
                    problem_id=pid,
                    language=lang,
                    code="x",
                    verdict=verdict,
                    passed_cases=1 if verdict == "AC" else 0,
                    total_cases=1,
                )
            )


# ================================================================ 纯函数
def test_level_curve():
    assert level_of(0) == 1 and level_of(49) == 1
    assert level_of(50) == 2 and level_of(199) == 2
    assert level_of(200) == 3
    assert next_level_xp(1) == 50 and next_level_xp(2) == 200


def test_streak_freeze_semantics():
    """断签保护：缺口日消耗冻结额度桥接，额度耗尽即停；无缺口不消耗。"""
    from datetime import date

    today = date(2026, 10, 25)
    # 连续 5 天（今天到最后 5 天）——无缺口
    dates = {(today - timedelta(days=i)).isoformat() for i in range(5)}
    out = streak_with_freeze(dates, today)
    assert out["current"] == 5 and out["freezes_used"] == 0

    # 中间断一天（-3 缺）：冻结桥接，current 仍为 5
    dates2 = {(today - timedelta(days=i)).isoformat() for i in (0, 1, 2, 4, 5)}
    out2 = streak_with_freeze(dates2, today)
    assert out2["current"] == 5 and out2["freezes_used"] == 1

    # 缺两天：两次冻结桥接，current 5
    dates3 = {(today - timedelta(days=i)).isoformat() for i in (0, 1, 4, 5, 6)}
    out3 = streak_with_freeze(dates3, today)
    assert out3["current"] == 5 and out3["freezes_used"] == 2

    # 缺三天：额度（2）耗尽，只能数到缺口前
    dates4 = {(today - timedelta(days=i)).isoformat() for i in (0, 1, 5, 6, 7)}
    out4 = streak_with_freeze(dates4, today)
    assert out4["current"] == 2 and out4["freezes_used"] == 2

    # 今天未打卡：从昨天起算（不视为缺口）
    dates5 = {(today - timedelta(days=i)).isoformat() for i in (1, 2, 3)}
    out5 = streak_with_freeze(dates5, today)
    assert out5["current"] == 3 and out5["checked_today"] is False


# ================================================================ XP 派生
async def test_compute_xp_derived_from_facts(gm_user):
    """XP 必须等于事实聚合 × 权重（无账本，重算即生效）。"""
    _c, _t, uid = gm_user
    await _seed_submissions(uid, total=6, ac=4)
    async with session_scope() as db:
        db.add(
            DailyCheckin(
                user_id=uid, checkin_date="2026-10-24", ac_count=3, submit_count=5, goal_met=True
            )
        )
        db.add(
            DailyCheckin(
                user_id=uid, checkin_date="2026-10-23", ac_count=1, submit_count=2, goal_met=False
            )
        )

    async with session_scope() as db:
        xp = await compute_xp(db, uid)
    b = {x["key"]: x for x in xp["breakdown"]}
    assert b["submissions"]["count"] == 6
    assert b["ac"]["count"] == 4
    assert b["distinct_ac"]["count"] == 1
    assert b["checkins"]["count"] == 2
    assert b["goal_days"]["count"] == 1
    expected = 6 * XP_PER_SUBMISSION + 4 * XP_PER_AC + 1 * 15 + 2 * XP_PER_CHECKIN_DAY + 1 * 10
    assert xp["xp"] == expected, "XP 必须逐项可复算"
    assert xp["level"] == level_of(expected)


# ================================================================ 成就
async def test_achievements_unlock_and_progress(gm_user):
    _c, _t, uid = gm_user
    await _seed_submissions(uid, total=3, ac=2, langs=("python", "cpp"))
    async with session_scope() as db:
        ach = await evaluate_achievements(db, uid, streak={"current": 0})
    by_id = {a["id"]: a for a in ach}
    assert by_id["first_submit"]["unlocked"] is True
    assert by_id["first_ac"]["unlocked"] is True
    assert by_id["ac_10"]["unlocked"] is False
    assert by_id["ac_10"]["progress"] == {"current": 2, "target": 10}
    assert by_id["polyglot"]["unlocked"] is True, "python+cpp 双语言"


async def test_interview_zero_hint_achievement(gm_user):
    """零提示 AC 面试解锁 clean 成就；索要过提示的不解锁。"""
    _c, _t, uid = gm_user
    async with session_scope() as db:
        import json as _json

        s1 = ChatSession(user_id=uid, kind="interview", meta=_json.dumps({"hint_requests": 0}))
        s2 = ChatSession(user_id=uid, kind="interview", meta=_json.dumps({"hint_requests": 2}))
        db.add_all([s1, s2])
        await db.flush()
        db.add(InterviewReport(session_id=s1.id, user_id=uid, verdict="AC", scores="{}"))
        db.add(InterviewReport(session_id=s2.id, user_id=uid, verdict="AC", scores="{}"))

    async with session_scope() as db:
        ach = await evaluate_achievements(db, uid)
    by_id = {a["id"]: a for a in ach}
    assert by_id["interview_first"]["unlocked"] is True
    assert by_id["interview_clean"]["unlocked"] is True


# ================================================================ API
async def test_gamification_api(gm_user):
    client, token, uid = gm_user
    H = {"Authorization": f"Bearer {token}"}
    await _seed_submissions(uid, total=2, ac=1)
    r = await client.get("/api/gamification", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {
        "xp",
        "level",
        "next_level_xp",
        "breakdown",
        "achievements",
        "streak",
        "unlocked_count",
    } <= set(body)
    assert body["xp"] > 0
    assert len(body["achievements"]) == 12
    assert "freezes_left" in body["streak"], "断签保护状态必须透出"
