"""掌握度计算：0-100 分 + 五级分档 + 置信度，供 mastery Agent 与规则引擎共用。

分数口径（与 MasteryAgent 的 system prompt 一致）：
  score = 加权正确率 × 70 + 难度加成 × 30
  权重 w = log1p(attempts) × 时间衰减（近 30 天权重更高）
  置信度 = 1 - exp(-total_attempts / 12)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

LEVEL_THRESHOLDS: list[tuple[str, float]] = [("L5", 85), ("L4", 70), ("L3", 55), ("L2", 40)]
HALF_LIFE_DAYS = 21.0


def level_of(score: float) -> str:
    for name, threshold in LEVEL_THRESHOLDS:
        if score >= threshold:
            return name
    return "L1"


def time_decay(dt: datetime | None, now: datetime | None = None) -> float:
    """时间衰减：半衰期 21 天，越近的提交权重越高。"""
    if dt is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * days / HALF_LIFE_DAYS)


def confidence_of(total_attempts: float, distinct_tags: int = 1) -> float:
    """置信度：样本量饱和函数，同时考虑标签覆盖面。"""
    base = 1.0 - math.exp(-max(0.0, total_attempts) / 12.0)
    coverage = 1.0 - math.exp(-max(1, distinct_tags) / 6.0)
    return round(base * 0.8 + coverage * 0.2, 4)


def compute_tag_stats(
    submissions: list[dict[str, Any]],
    problem_tags: dict[str, list[tuple[str, float]]],
    problem_difficulty: dict[str, float],
    now: datetime | None = None,
    window_days: int = 30,
) -> dict[str, dict[str, float]]:
    """从提交记录聚合出每个标签的统计。

    入参均为纯 dict，便于单测与离线复算（不碰数据库）。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    stats: dict[str, dict[str, float]] = {}

    for sub in submissions:
        created = sub.get("created_at")
        if isinstance(created, datetime):
            c = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            if window_days > 0 and c < cutoff:
                continue
            decay = time_decay(c, now)
        else:
            decay = 0.6

        pid = str(sub.get("problem_id", ""))
        verdict = str(sub.get("verdict", "")).upper()
        is_ac = 1.0 if verdict == "AC" else 0.0
        diff = float(problem_difficulty.get(pid, 40.0))
        weight = float(sub.get("weight", 1.0)) * decay

        for tag_id, tag_weight in problem_tags.get(pid, []):
            st = stats.setdefault(
                tag_id,
                {
                    "attempts": 0.0,
                    "ac_count": 0.0,
                    "weighted_attempts": 0.0,
                    "weighted_ac": 0.0,
                    "difficulty_sum": 0.0,
                    "last_at": 0.0,
                },
            )
            w = weight * float(tag_weight)
            st["attempts"] += 1.0
            st["ac_count"] += is_ac
            st["weighted_attempts"] += w
            st["weighted_ac"] += w * is_ac
            st["difficulty_sum"] += diff * w
            st["last_at"] = max(
                st["last_at"], c.timestamp() if isinstance(created, datetime) else 0.0
            )

    # 附加派生字段
    for _tag, st in stats.items():
        wa = st["weighted_attempts"] or 1e-9
        st["accuracy"] = round(st["weighted_ac"] / wa, 4)
        st["avg_difficulty"] = round(st["difficulty_sum"] / wa, 2)
        st["raw_accuracy"] = round(st["ac_count"] / max(1.0, st["attempts"]), 4)
    return stats


def compute_score(
    tag_stats: dict[str, dict[str, float]],
    window_days: int = 30,
) -> dict[str, Any]:
    """主评分函数：返回 score / level / confidence / 分项明细。"""
    if not tag_stats:
        return {
            "score": 50.0,
            "level": "L3",
            "confidence": 0.1,
            "metrics": {"reason": "无提交样本，返回中性基线"},
            "weak_tags": [],
            "strong_tags": [],
        }

    num = den = 0.0
    total_attempts = 0.0
    diff_num = diff_den = 0.0
    for _tag, st in tag_stats.items():
        attempts = float(st.get("attempts", 0.0))
        if attempts <= 0:
            continue
        w = math.log1p(attempts)
        num += float(st.get("accuracy", 0.0)) * w
        den += w
        diff_num += float(st.get("avg_difficulty", 40.0)) * attempts
        diff_den += attempts
        total_attempts += attempts

    accuracy = (num / den) if den else 0.5
    avg_diff = (diff_num / diff_den) if diff_den else 40.0
    score = accuracy * 70.0 + (avg_diff / 100.0) * 30.0
    score = round(max(0.0, min(100.0, score)), 2)

    ranked = sorted(
        tag_stats.items(),
        key=lambda kv: float(kv[1].get("accuracy", 0.0)),
    )
    return {
        "score": score,
        "level": level_of(score),
        "confidence": confidence_of(total_attempts, len(tag_stats)),
        "weak_tags": [t for t, _ in ranked[:3]],
        "strong_tags": [t for t, s in ranked[::-1][:3] if float(s.get("accuracy", 0)) >= 0.6],
        "metrics": {
            "accuracy": round(accuracy, 4),
            "avg_difficulty": round(avg_diff, 2),
            "total_attempts": int(total_attempts),
            "tag_count": len(tag_stats),
            "window_days": window_days,
            "formula": "score = 加权正确率*70 + (平均难度/100)*30",
        },
    }


def recalc_profile_rows(
    user_id: str,
    tag_stats: dict[str, dict[str, float]],
    existing: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """生成 mastery_profiles 的 upsert 行。"""
    existing = existing or {}
    rows: list[dict[str, Any]] = []
    for tag_id, st in tag_stats.items():
        attempts = float(st.get("attempts", 0.0))
        accuracy = float(st.get("accuracy", 0.0))
        # 单标签掌握度：正确率 × 70 + 难度加成 × 30，再乘置信度做收缩
        avg_diff = float(st.get("avg_difficulty", 40.0))
        raw = accuracy * 70.0 + (avg_diff / 100.0) * 30.0
        conf = confidence_of(attempts)
        score = raw * conf + 50.0 * (1.0 - conf)  # 样本少则向中性 50 收缩
        rows.append(
            {
                "user_id": user_id,
                "tag_id": tag_id,
                "score": round(max(0.0, min(100.0, score)), 2),
                "confidence": conf,
                "attempts": int(attempts),
                "ac_count": int(st.get("ac_count", 0)),
                "raw_score": round(raw, 2),
                "prev_score": existing.get(tag_id, {}).get("score"),
            }
        )
    return rows
