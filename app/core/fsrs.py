"""FSRS 简化版：稳定性 S、难度 D、可提取性 R 的遗忘曲线与调度。

不做完整 FSRS 参数拟合，只保留三个核心量，参数少、可解释、代码 <100 行：
  R(t) = exp(ln(0.9) * t / S)          —— 可提取性随天数衰减
  S'   = S * (1 + 0.9 * (11 - D) * ... )  —— 复习成功则稳定度增长
  I    = S * ln(target) / ln(0.9)      —— 下次复习间隔

默认目标保留率 0.9，与 FSRS 一致。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

TARGET_RETENTION = 0.9
DECAY = math.log(TARGET_RETENTION)  # 负数
MIN_STABILITY = 0.1
MAX_INTERVAL_DAYS = 365.0
MIN_INTERVAL_DAYS = 0.5


def retrievability(stability: float, elapsed_days: float) -> float:
    """R(t)：距上次复习 elapsed_days 天后仍能回忆起的概率。"""
    s = max(MIN_STABILITY, float(stability))
    return math.exp(DECAY * max(0.0, float(elapsed_days)) / s)


def initial_stability(rating: int) -> float:
    """首次学习：rating 1-4（again/hard/good/easy）。"""
    table = {1: 0.4, 2: 1.2, 3: 3.0, 4: 8.0}
    return table.get(int(rating), 3.0)


def initial_difficulty(rating: int) -> float:
    """D ∈ [1, 10]，越高越难。"""
    r = max(1, min(4, int(rating)))
    return max(1.0, min(10.0, 7.0 - (r - 3) * 1.5))


def next_difficulty(difficulty: float, rating: int) -> float:
    """复习后难度更新：评分低则升难（D 变大），评分高则降难（D 变小），并做均值回归。"""
    r = max(1, min(4, int(rating)))
    delta_map = {1: 1.5, 2: 0.5, 3: -0.5, 4: -1.5}
    d = float(difficulty) + delta_map[r]
    return max(1.0, min(10.0, d * 0.95 + 5.0 * 0.05))


def next_stability(stability: float, difficulty: float, recall: float, rating: int) -> float:
    """复习后的稳定度：成功则增长，失败则回退。"""
    s = max(MIN_STABILITY, float(stability))
    d = max(1.0, min(10.0, float(difficulty)))
    r = max(1, min(4, int(rating)))
    if recall < 0.5 or r == 1:
        # 遗忘：稳定度回退（lapse）
        return max(MIN_STABILITY, s * 0.3)
    bonus = {2: 0.8, 3: 1.0, 4: 1.3}.get(r, 1.0)
    growth = 1.0 + bonus * (11.0 - d) * 0.12 * (1.0 - recall)
    return max(MIN_STABILITY, s * max(1.05, growth))


def next_interval(stability: float, target: float = TARGET_RETENTION) -> float:
    """I = S * ln(target) / ln(0.9)，再夹到合理区间。"""
    s = max(MIN_STABILITY, float(stability))
    interval = s * math.log(target) / DECAY
    return max(MIN_INTERVAL_DAYS, min(MAX_INTERVAL_DAYS, interval))


def schedule(
    *,
    stability: float = 0.0,
    difficulty: float = 5.0,
    reps: int = 0,
    lapses: int = 0,
    last_review_at: datetime | None = None,
    rating: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次复习的完整调度：输入旧状态 + 本次评分，输出新状态。

    返回 dict 直接映射 review_queue 表的字段，方便落库。
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if reps <= 0 or stability <= 0:
        new_s = initial_stability(rating)
        new_d = initial_difficulty(rating)
        recall = 1.0
        new_lapses = lapses
    else:
        elapsed = 0.0
        if last_review_at is not None:
            lr = last_review_at
            if lr.tzinfo is None:
                lr = lr.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (now - lr).total_seconds() / 86400.0)
        recall = retrievability(stability, elapsed)
        new_d = next_difficulty(difficulty, rating)
        new_s = next_stability(stability, new_d, recall, rating)
        new_lapses = lapses + (1 if (recall < 0.5 or rating == 1) else 0)

    interval = next_interval(new_s)
    due_at = now + timedelta(days=interval)
    return {
        "stability": round(new_s, 4),
        "difficulty_fsrs": round(new_d, 4),
        "retrievability": round(recall, 4),
        "interval_days": round(interval, 4),
        "reps": reps + 1,
        "lapses": new_lapses,
        "last_review_at": now,
        "due_at": due_at,
    }


def forgetting_curve(
    stability: float, days: int = 100, step: float = 1.0
) -> list[dict[str, float]]:
    """给前端画图：0..days 天内的保留率曲线。"""
    curve: list[dict[str, float]] = []
    t = 0.0
    while t <= days:
        curve.append({"day": round(t, 2), "retention": round(retrievability(stability, t), 4)})
        t += step
    return curve


def is_due(due_at: datetime | None, now: datetime | None = None) -> bool:
    if due_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return due_at <= now


def overdue_days(due_at: datetime | None, now: datetime | None = None) -> float:
    if due_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - due_at).total_seconds() / 86400.0)
