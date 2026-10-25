"""成长体系：XP（事实派生）+ 等级 + 成就 + 断签保护（streak freeze）。

架构决策（对标 Forge 的 gamification 参考 / Duolingo / Habitica）：
  * **XP 不建账本**——等级、成就全部是既有事实表（submissions /
    daily_checkins / interview_reports / review_queue）上的纯函数派生。
    好处：可审计（"你的 XP 来自 48 次提交×2 + 20 次 AC×10"）、可重算
    （调整公式后历史立即生效）、永不与事实漂移。与 FSRS 队列/掌握度
    画像同一哲学：单一事实源，其余皆派生。
  * **断签保护**（Duolingo streak freeze 的简化版）：连续打卡回溯时
    允许最多 2 个缺口日被"冻结"桥接；历史最长连击保持严格口径
    （不含冻结），两者分开陈述、不互相污染。

XP 公式（全部可解释，前端逐项展示）：
    提交 ×2 + AC ×10 + 做出的不同题 ×15 + 打卡日 ×5 + 达标日 ×10
    + 复习打分 ×3 + 完成模拟面 ×30 + 面试总分 ×0.5
等级：level = floor(sqrt(xp / 50)) + 1（1 级 0-49，2 级 50-199……）
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, cast, func, select

from ..models import (
    ChatSession,
    DailyCheckin,
    InterviewReport,
    ReviewQueue,
    Submission,
)

# ---------------- XP 公式权重（规则层单一来源，前端逐项解释） ----------------
XP_PER_SUBMISSION = 2
XP_PER_AC = 10
XP_PER_DISTINCT_AC = 15
XP_PER_CHECKIN_DAY = 5
XP_PER_GOAL_MET_DAY = 10
XP_PER_REVIEW_MARK = 3
XP_PER_INTERVIEW = 30
XP_PER_INTERVIEW_SCORE = 0.5  # 每分总分

# 断签保护：连击回溯允许的缺口日上限（Duolingo streak freeze 简化版）
STREAK_FREEZE_ALLOWANCE = 2

# 等级曲线
XP_LEVEL_BASE = 50


def level_of(xp: float) -> int:
    return int(math.sqrt(max(0.0, xp) / XP_LEVEL_BASE)) + 1


def next_level_xp(level: int) -> int:
    return XP_LEVEL_BASE * level * level


async def compute_xp(db, user_id: str) -> dict[str, Any]:
    """从事实表派生 XP：返回总分 + 逐项明细 + 等级信息。"""
    sub_row = (
        await db.execute(
            select(
                func.count(Submission.id),
                func.sum(cast(Submission.verdict == "AC", Integer)),
                func.count(func.distinct(Submission.language)),
            ).where(Submission.user_id == user_id)
        )
    ).one()
    submissions_total, ac_total, lang_count = (
        int(sub_row[0] or 0),
        int(sub_row[1] or 0),
        int(sub_row[2] or 0),
    )

    distinct_ac = int(
        (
            await db.execute(
                select(func.count(func.distinct(Submission.problem_id))).where(
                    Submission.user_id == user_id, Submission.verdict == "AC"
                )
            )
        ).scalar()
        or 0
    )

    checkin_row = (
        await db.execute(
            select(
                func.count(DailyCheckin.id),
                func.sum(cast(DailyCheckin.goal_met, Integer)),
            ).where(DailyCheckin.user_id == user_id)
        )
    ).one()
    checkin_days, goal_days = int(checkin_row[0] or 0), int(checkin_row[1] or 0)

    review_reps = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(ReviewQueue.reps), 0)).where(
                    ReviewQueue.user_id == user_id
                )
            )
        ).scalar()
        or 0
    )

    interview_row = (
        await db.execute(
            select(func.count(InterviewReport.id)).where(InterviewReport.user_id == user_id)
        )
    ).scalar_one()
    interviews_done = int(interview_row or 0)
    interview_score_sum = 0.0
    if interviews_done:
        reports = (
            (
                await db.execute(
                    select(InterviewReport.scores).where(InterviewReport.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        for s in reports:
            try:
                interview_score_sum += float(json.loads(s or "{}").get("total", 0.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    breakdown = [
        {
            "key": "submissions",
            "label": "提交练习",
            "count": submissions_total,
            "per": XP_PER_SUBMISSION,
            "xp": submissions_total * XP_PER_SUBMISSION,
        },
        {
            "key": "ac",
            "label": "通过用例全对（AC）",
            "count": ac_total,
            "per": XP_PER_AC,
            "xp": ac_total * XP_PER_AC,
        },
        {
            "key": "distinct_ac",
            "label": "攻克的题目",
            "count": distinct_ac,
            "per": XP_PER_DISTINCT_AC,
            "xp": distinct_ac * XP_PER_DISTINCT_AC,
        },
        {
            "key": "checkins",
            "label": "打卡天数",
            "count": checkin_days,
            "per": XP_PER_CHECKIN_DAY,
            "xp": checkin_days * XP_PER_CHECKIN_DAY,
        },
        {
            "key": "goal_days",
            "label": "达标日",
            "count": goal_days,
            "per": XP_PER_GOAL_MET_DAY,
            "xp": goal_days * XP_PER_GOAL_MET_DAY,
        },
        {
            "key": "reviews",
            "label": "复习打分",
            "count": review_reps,
            "per": XP_PER_REVIEW_MARK,
            "xp": review_reps * XP_PER_REVIEW_MARK,
        },
        {
            "key": "interviews",
            "label": "完成模拟面试",
            "count": interviews_done,
            "per": XP_PER_INTERVIEW,
            "xp": interviews_done * XP_PER_INTERVIEW,
        },
        {
            "key": "interview_scores",
            "label": "面试表现分",
            "count": round(interview_score_sum, 1),
            "per": XP_PER_INTERVIEW_SCORE,
            "xp": round(interview_score_sum * XP_PER_INTERVIEW_SCORE),
        },
    ]
    total = sum(b["xp"] for b in breakdown)
    level = level_of(total)
    return {
        "xp": round(total),
        "level": level,
        "level_title": LEVEL_TITLES.get(level, "传奇"),
        "next_level_xp": next_level_xp(level),
        "current_level_floor": next_level_xp(level - 1),
        "breakdown": breakdown,
        "meta": {"languages_used": lang_count, "interviews_done": interviews_done},
    }


LEVEL_TITLES = {
    1: "入门新手",
    2: "勤勉学徒",
    3: "稳步前行",
    4: "熟练解题者",
    5: "算法熟手",
    6: "高手",
    7: "大师",
    8: "传奇",
}


# ================================================================ 成就
async def evaluate_achievements(
    db, user_id: str, streak: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """成就 = 事实聚合上的纯函数；每条带进度（12/50）与解锁状态。"""
    xp_info = await compute_xp(db, user_id)
    b = {x["key"]: x["count"] for x in xp_info["breakdown"]}

    # 单日 AC>=5：来自打卡记录的快照（打卡日本身就是"当日终算"事实）
    best_day_ac = int(
        (
            await db.execute(
                select(func.coalesce(func.max(DailyCheckin.ac_count), 0)).where(
                    DailyCheckin.user_id == user_id
                )
            )
        ).scalar()
        or 0
    )

    # 零提示通过模拟面：报告 AC 且会话 meta.hint_requests == 0
    zero_hint_pass = False
    rows = (
        await db.execute(
            select(InterviewReport.verdict, ChatSession.meta)
            .join(ChatSession, ChatSession.id == InterviewReport.session_id)
            .where(InterviewReport.user_id == user_id)
        )
    ).all()
    for verdict, meta_json in rows:
        if verdict != "AC":
            continue
        try:
            if int(json.loads(meta_json or "{}").get("hint_requests", 99)) == 0:
                zero_hint_pass = True
                break
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    cur_streak = int((streak or {}).get("current", 0))

    def ach(aid: str, title: str, desc: str, current: int, target: int) -> dict[str, Any]:
        return {
            "id": aid,
            "title": title,
            "desc": desc,
            "unlocked": current >= target,
            "progress": {"current": min(current, target), "target": target},
        }

    return [
        ach("first_submit", "迈出第一步", "完成第一次提交", int(b["submissions"]), 1),
        ach("first_ac", "首杀", "第一次 AC", int(b["ac"]), 1),
        ach("ac_10", "十连破", "累计 10 次 AC", int(b["ac"]), 10),
        ach("ac_50", "半百雄关", "累计 50 次 AC", int(b["ac"]), 50),
        ach("distinct_20", "题海扬帆", "攻克 20 道不同的题", int(b["distinct_ac"]), 20),
        ach("streak_3", "三日之约", "连续打卡 3 天", cur_streak, 3),
        ach("streak_7", "一周坚持", "连续打卡 7 天", cur_streak, 7),
        ach("streak_30", "月度习惯", "连续打卡 30 天", cur_streak, 30),
        ach("day_5_ac", "状态火热", "单日 AC 达到 5 题", best_day_ac, 5),
        ach("interview_first", "初登考场", "完成一场模拟面试", int(b["interviews"]), 1),
        ach(
            "interview_clean",
            "零提示通过",
            "不索要提示并通过模拟面试",
            1 if zero_hint_pass else 0,
            1,
        ),
        ach(
            "polyglot",
            "双语开发者",
            "用两种语言提交过代码",
            int(xp_info["meta"]["languages_used"]),
            2,
        ),
    ]


# ================================================================ 断签保护
def streak_with_freeze(
    dates: set[str], local_today, allowance: int = STREAK_FREEZE_ALLOWANCE
) -> dict[str, int]:
    """带回溯缺口的连击计算（纯函数，便于单测）。

    从今天（或昨天，今天未打卡时）往前走：打卡日 +1；缺口日消耗一次
    冻结额度桥接（不计数）；额度耗尽即停。返回 current / freezes_used。
    历史最长保持严格口径（不在此函数——见 operations.compute_streak）。
    """
    checked_today = local_today.isoformat() in dates
    cursor = local_today if checked_today else local_today - timedelta(days=1)
    current = 0
    freezes_used = 0
    while True:
        if cursor.isoformat() in dates:
            current += 1
            cursor -= timedelta(days=1)
            continue
        # 缺口处理：只要更早一侧还存在打卡日，就逐日消耗冻结额度桥接
        # （连续缺口 = 连续消耗，Duolingo 语义）；起点之前的空无不耗额度
        cursor_str = cursor.isoformat()
        if freezes_used < allowance and any(d < cursor_str for d in dates):
            freezes_used += 1
            cursor -= timedelta(days=1)
            continue
        break
    return {"current": current, "freezes_used": freezes_used, "checked_today": checked_today}


def now_utc() -> datetime:  # 供测试注入
    return datetime.now(__import__("datetime").timezone.utc)
