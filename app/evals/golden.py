"""Golden set：覆盖 5 个核心 Agent 的行为场景。

选场景的原则：**每个场景钉住一条"对外承诺"**（提示强度策略、防幻觉、
时间预算、先修顺序、低样本置信度……），而不是凑数量。输入刻意构造得
区分度充足（候选项分差拉开、掌握度档位明确），让"模型答对"与"模型
蒙对"可区分 —— 分差太小的场景只会产出随机的 tau，没有评估价值。

期望值不在这里写死：提示强度由 checks 现算 hint_strength_for，
复习排序基线用 pipeline 返回的 rule_baseline —— golden set 只描述
输入与"该验证什么"，不维护第二份策略口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REVIEW_MINUTES_PER_PROBLEM = 15


@dataclass
class Scenario:
    """一个评估场景：输入 + 期望断言的参数包。"""

    id: str
    agent: str  # hint / review / mastery / roadmap / recommend
    title: str
    inputs: dict[str, Any]
    expect: dict[str, Any] = field(default_factory=dict)


def _review_max_items(budget: int, n_candidates: int) -> int:
    if budget <= 0:
        return n_candidates
    return max(1, budget // REVIEW_MINUTES_PER_PROBLEM)


# ---------------------------------------------------------------- hint
_HINT_PROBLEM = {
    "title": "区间合并",
    "statement": (
        "给出 n 个区间 [l_i, r_i]，把所有有重叠的区间合并成一个，"
        "输出合并后区间的个数与各自的左右端点。1 <= n <= 1e5，坐标 <= 1e9。"
    ),
    "tags": ["排序", "贪心", "双指针"],
    "difficulty": "普及-",
    "language": "python",
}

# 复习候选：分差刻意拉开（相邻分差 >= 15），避免"模型蒙对也分不清"
_REVIEW_CANDIDATES_A = [
    {
        "problem_id": "rev-a1",
        "title": "最长上升子序列",
        "difficulty": "普及/提高-",
        "tags": ["动态规划"],
        "overdue_days": 6.5,
        "retrievability": 0.49,
        "lapses": 1,
        "mastery_scores": {"动态规划": 30.0},
    },
    {
        "problem_id": "rev-a2",
        "title": "二分答案模板题",
        "difficulty": "普及-",
        "tags": ["二分"],
        "overdue_days": 1.0,
        "retrievability": 0.72,
        "lapses": 0,
        "mastery_scores": {"二分": 20.0},
    },
    {
        "problem_id": "rev-a3",
        "title": "区间调度贪心",
        "difficulty": "普及-",
        "tags": ["贪心"],
        "overdue_days": 0.0,
        "retrievability": 0.65,
        "lapses": 0,
        "mastery_scores": {"贪心": 45.0},
    },
]

_REVIEW_CANDIDATES_B = [
    {
        "problem_id": "rev-b1",
        "title": "滑动窗口最大值",
        "difficulty": "普及/提高-",
        "tags": ["双指针", "单调队列"],
        "overdue_days": 8.0,
        "retrievability": 0.41,
        "lapses": 2,
        "mastery_scores": {"双指针": 25.0, "单调队列": 30.0},
    },
    {
        "problem_id": "rev-b2",
        "title": "前缀和求区间和",
        "difficulty": "入门",
        "tags": ["前缀和"],
        "overdue_days": 2.0,
        "retrievability": 0.70,
        "lapses": 0,
        "mastery_scores": {"前缀和": 35.0},
    },
    {
        "problem_id": "rev-b3",
        "title": "活动选择问题",
        "difficulty": "普及-",
        "tags": ["贪心"],
        "overdue_days": 0.0,
        "retrievability": 0.68,
        "lapses": 1,
        "mastery_scores": {"贪心": 40.0},
    },
]

_ROADMAP_TAGS = [
    {"id": "t1", "name": "前缀和", "prereq_ids": "[]"},
    {"id": "t2", "name": "线段树", "prereq_ids": '["t1"]'},
    {"id": "t3", "name": "二分", "prereq_ids": "[]"},
    {"id": "t4", "name": "动态规划", "prereq_ids": "[]"},
]
_ROADMAP_MASTERY = {"前缀和": 55.0, "线段树": 20.0, "二分": 40.0, "动态规划": 35.0}

_RECOMMEND_CANDIDATES = [
    {
        "id": "rec-1",
        "title": "斐波那契数列",
        "difficulty": "入门",
        "difficulty_score": 25.0,
        "tags": ["动态规划"],
        "overdue_days": 0.0,
    },
    {
        "id": "rec-2",
        "title": "贪心区间选点",
        "difficulty": "普及-",
        "difficulty_score": 42.0,
        "tags": ["贪心"],
        "overdue_days": 1.0,
    },
    {
        "id": "rec-3",
        "title": "二分查找最值",
        "difficulty": "普及-",
        "difficulty_score": 48.0,
        "tags": ["二分"],
        "overdue_days": 0.0,
    },
    {
        "id": "rec-4",
        "title": "双指针原地删除",
        "difficulty": "普及-",
        "difficulty_score": 40.0,
        "tags": ["双指针"],
        "overdue_days": 0.0,
    },
]

SCENARIOS: list[Scenario] = [
    # ---- hint：策略归规则（强度 = hint_strength_for），表达归模型 ----
    Scenario(
        id="hint-first-contact",
        agent="hint",
        title="首次接触（0 次提交）必须给最强提示",
        inputs={
            "problem": _HINT_PROBLEM,
            "mastery_score": 40.0,
            "attempts": 0,
        },
        expect={"mastery_score": 40.0, "attempts": 0},
    ),
    Scenario(
        id="hint-low-sample",
        agent="hint",
        title="样本不足（<3 次）保守给中等强度",
        inputs={"problem": _HINT_PROBLEM, "mastery_score": 80.0, "attempts": 1},
        expect={"mastery_score": 80.0, "attempts": 1},
    ),
    Scenario(
        id="hint-strong-mastery",
        agent="hint",
        title="掌握良好只给方向，不啰嗦",
        inputs={"problem": _HINT_PROBLEM, "mastery_score": 85.0, "attempts": 10},
        expect={"mastery_score": 85.0, "attempts": 10},
    ),
    Scenario(
        id="hint-weak-mastery",
        agent="hint",
        title="掌握薄弱给最强提示（但不能替他写代码）",
        inputs={"problem": _HINT_PROBLEM, "mastery_score": 20.0, "attempts": 10},
        expect={"mastery_score": 20.0, "attempts": 10},
    ),
    # ---- review：防幻觉 + 预算 + 排序保真 ----
    Scenario(
        id="review-basic",
        agent="review",
        title="基础排序：逾期×遗忘×缺口最高的题必须第一",
        inputs={
            "candidates": _REVIEW_CANDIDATES_A,
            "time_budget_minutes": 0,
            "daily_goal": 3,
        },
        expect={
            "valid_ids": [c["problem_id"] for c in _REVIEW_CANDIDATES_A],
            "max_items": _review_max_items(0, len(_REVIEW_CANDIDATES_A)),
        },
    ),
    Scenario(
        id="review-budget",
        agent="review",
        title="时间预算 30 分钟只能排 2 题，宁可少挑",
        inputs={
            "candidates": _REVIEW_CANDIDATES_B,
            "time_budget_minutes": 30,
            "daily_goal": 2,
        },
        expect={
            "valid_ids": [c["problem_id"] for c in _REVIEW_CANDIDATES_B],
            "max_items": _review_max_items(30, len(_REVIEW_CANDIDATES_B)),
        },
    ),
    # ---- mastery：贴近规则基线 + 档位自洽 + 单调性 ----
    Scenario(
        id="mastery-low",
        agent="mastery",
        title="低正确率样本应判低掌握度",
        inputs={
            "tag_stats": {
                "贪心": {"attempts": 12, "ac_count": 3},
                "二分": {"attempts": 8, "ac_count": 1},
            },
            "avg_difficulty_score": 42.0,
        },
        expect={
            "tags": ["贪心", "二分"],
            "max_score": 40.0,
            "max_drift": 15.0,
        },
    ),
    Scenario(
        id="mastery-high",
        agent="mastery",
        title="高正确率+高难度样本应判高掌握度（且高于 mastery-low）",
        inputs={
            "tag_stats": {
                "贪心": {"attempts": 20, "ac_count": 18},
                "二分": {"attempts": 15, "ac_count": 13},
            },
            "avg_difficulty_score": 78.0,
        },
        expect={
            "tags": ["贪心", "二分"],
            "max_drift": 15.0,
            "score_gt_scenario": "mastery-low",
        },
    ),
    Scenario(
        id="mastery-empty",
        agent="mastery",
        title="零样本：分数居中、置信度必须低",
        inputs={"tag_stats": {}, "avg_difficulty_score": 40.0},
        expect={
            "tags": [],
            "max_confidence": 0.3,
            "max_drift": 15.0,
        },
    ),
    # ---- roadmap：结构有效性 ----
    Scenario(
        id="roadmap-prereq",
        agent="roadmap",
        title="先修标签必须排在依赖它的标签之前",
        inputs={"tags": _ROADMAP_TAGS, "mastery": _ROADMAP_MASTERY, "weeks": 6},
        expect={
            "tag_names": [t["name"] for t in _ROADMAP_TAGS],
            "prereq_pairs": [("前缀和", "线段树")],
            "weeks": 6,
        },
    ),
    # ---- recommend：防幻觉 + 近期去重 ----
    Scenario(
        id="recommend-basic",
        agent="recommend",
        title="推荐必须在候选内且避开最近做过的题",
        inputs={
            "candidates": _RECOMMEND_CANDIDATES,
            "mastery": {"贪心": 20.0, "二分": 65.0, "动态规划": 45.0, "双指针": 55.0},
            "recent_problem_ids": ["rec-4"],
            "target_score": 55.0,
            "weak_tags": ["贪心"],
            "user_level": 3,
        },
        expect={
            "valid_ids": [c["id"] for c in _RECOMMEND_CANDIDATES],
            "exclude_ids": ["rec-4"],
        },
    ),
]
