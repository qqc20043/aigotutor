"""Agent 行为评估的回归测试：mock 下全绿是 CI 的底线。

同时验证评估器自身（断言库失败路径、报告渲染、tau 计算）——
评估器自己的 bug 和 Agent 行为退化都必须在 CI 里拦住。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.evals import SCENARIOS, kendall_tau, level_for_score, run_eval
from app.evals.checks import (
    check_hint,
    check_mastery,
    check_recommend,
    check_review,
    check_roadmap,
)


def _mock_config() -> dict:
    return {
        "provider": "mock",
        "model": settings.DEFAULT_MODEL,
        "base_url": "",
        "api_key": "",
    }


def _by_name(checks):
    return {c.name: c for c in checks}


# ============================================================ 全量回归
async def test_full_eval_green_under_mock():
    """mock（无信息的模型）下全场景全绿 —— 任何一条失败都是行为回归。"""
    report = await run_eval(_mock_config())
    assert len(report.results) == len(SCENARIOS)
    failed = {
        r.scenario_id: [c.name for c in r.failed_checks] for r in report.results if not r.passed
    }
    assert not failed, f"mock 下评估必须全绿，失败场景: {failed}"
    assert report.degraded_count == 0, "mock 链路不应触发降级"


async def test_eval_report_rendering():
    report = await run_eval(_mock_config())
    md = report.to_markdown()
    assert "Agent 行为评估报告" in md
    assert report.summary_line() in md
    for r in report.results:
        assert r.scenario_id in md
    d = report.to_dict()
    assert d["summary"]["total"] == len(SCENARIOS) == d["summary"]["passed"]
    assert [r["scenario_id"] for r in d["results"]] == [s.id for s in SCENARIOS]


async def test_mastery_monotonic_property():
    """跨场景单调性：同样标签、更高正确率+更高难度 => 更高掌握度。"""
    report = await run_eval(_mock_config(), only=["mastery"])
    scores = {r.scenario_id: r for r in report.results}
    high = next(c for c in scores["mastery-high"].checks if c.name.startswith("单调性"))
    assert high.passed, high.detail


# ============================================================ 断言库自身
def test_kendall_tau():
    assert kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    # 单个相邻换序：3 项里 1 个逆序对 -> 1/3
    assert kendall_tau(["a", "c", "b"], ["a", "b", "c"]) == pytest.approx(1 / 3)
    assert kendall_tau(["c", "b", "a"], ["a", "b", "c"]) == pytest.approx(-1.0)
    # 共同子集不足 2 项 / 模型挑了基线外的 id：按共同子集算
    assert kendall_tau(["a"], ["a", "b"]) == 1.0
    assert kendall_tau(["a", "b", "x"], ["a", "b"]) == 1.0


def test_level_for_score_bands():
    assert level_for_score(39.9) == "L1"
    assert level_for_score(40.0) == "L2"
    assert level_for_score(54.9) == "L2"
    assert level_for_score(55.0) == "L3"
    assert level_for_score(70.0) == "L4"
    assert level_for_score(85.0) == "L5"


def test_check_hint_failure_paths():
    expect = {"mastery_score": 85.0, "attempts": 10}
    good = {
        "strength": "direction",
        "hints": ["先确认数据范围，判断朴素做法是否会超时"],
        "key_insight": "从复杂度上限入手",
        "next_step": "手算小样例",
        "strength_reason": "掌握度 85 分（10 次提交），只给方向性提示",
    }
    assert all(c.passed for c in check_hint(good, expect))

    bad = {**good, "strength": "pseudocode", "hints": ["直接 print(a + b) 就能过"]}
    results = _by_name(check_hint(bad, expect))
    assert not results["强度档位与规则层一致"].passed
    assert not results["提示条数与强度对应"].passed
    assert not results["不泄露完整代码"].passed


def test_check_review_failure_paths():
    baseline = {
        "ordered": [
            {"problem_id": "p1"},
            {"problem_id": "p2"},
            {"problem_id": "p3"},
        ]
    }
    expect = {"valid_ids": ["p1", "p2", "p3"], "max_items": 3}
    good = {
        "ordered": [
            {"problem_id": "p1", "reason": "逾期 6 天、掌握度 30 分"},
            {"problem_id": "p2", "reason": "缺口 80 分"},
            {"problem_id": "p3", "reason": "掌握度 45 分"},
        ]
    }
    assert all(c.passed for c in check_review(good, baseline, expect))

    bad = {
        "ordered": [
            {"problem_id": "编造的题", "reason": "很重要"},
            {"problem_id": "p3", "reason": "比较重要"},
            {"problem_id": "p2", "reason": "也还行"},
        ]
    }
    results = _by_name(check_review(bad, baseline, expect))
    assert not results["题目全部来自候选（防幻觉）"].passed
    assert not results["Top-1 与规则基线一致"].passed
    assert not results["每条理由引用具体数字"].passed
    assert not results["排序保真度 tau≥0.6"].passed


def test_check_review_budget_violation():
    baseline = {"ordered": [{"problem_id": "p1"}, {"problem_id": "p2"}, {"problem_id": "p3"}]}
    expect = {"valid_ids": ["p1", "p2", "p3"], "max_items": 2}
    over = {
        "ordered": [
            {"problem_id": "p1", "reason": "逾期 3 天"},
            {"problem_id": "p2", "reason": "掌握度 40 分"},
            {"problem_id": "p3", "reason": "掌握度 45 分"},
        ]
    }
    results = _by_name(check_review(over, baseline, expect))
    assert not results["时间预算不被超出"].passed


def test_check_mastery_failure_paths():
    baseline = {"score": 80.0}
    expect = {"tags": ["贪心", "二分"], "min_score": 75.0, "max_drift": 15.0}
    good = {
        "score": 86.0,
        "level": "L5",
        "weak_tags": [],
        "strong_tags": ["贪心"],
        "confidence": 0.9,
    }
    assert all(c.passed for c in check_mastery(good, baseline, expect))

    bad = {
        "score": 72.0,
        "level": "L3",  # 72 分按口径应为 L4
        "weak_tags": ["并不存在的标签"],
        "strong_tags": ["贪心"],
        "confidence": 0.9,
    }
    results = _by_name(check_mastery(bad, baseline, expect))
    assert not results["档位与分数自洽"].passed
    assert not results["强弱项全部来自输入标签（不编造知识点）"].passed
    assert not results["分数不低于 75.0"].passed


def test_check_mastery_low_confidence_gate():
    baseline = {"score": 50.0}
    expect = {"tags": [], "max_confidence": 0.3, "max_drift": 15.0}
    good = {"score": 50.0, "level": "L2", "weak_tags": [], "strong_tags": [], "confidence": 0.1}
    assert all(c.passed for c in check_mastery(good, baseline, expect))
    bad = {**good, "confidence": 0.9}  # 零样本却声称高置信
    results = _by_name(check_mastery(bad, baseline, expect))
    assert not results["低样本置信度 ≤0.3"].passed


def test_check_roadmap_failure_paths():
    expect = {
        "tag_names": ["前缀和", "线段树", "二分"],
        "prereq_pairs": [("前缀和", "线段树")],
        "weeks": 6,
    }
    good = {
        "stages": [
            {"tags": ["前缀和", "二分"], "weeks": 3},
            {"tags": ["线段树"], "weeks": 3},
        ]
    }
    assert all(c.passed for c in check_roadmap(good, expect))

    bad = {
        "stages": [
            {"tags": ["线段树"], "weeks": 5},  # 先修被排到了后面
            {"tags": ["前缀和"], "weeks": 5},
            {"tags": ["杜撰标签"], "weeks": 5},
        ]
    }
    results = _by_name(check_roadmap(bad, expect))
    assert not results["阶段标签全部来自输入（不编造知识点）"].passed
    assert not results["先修关系排在后面之前"].passed
    assert not results["总周数不超过预算 2 倍（6 周）"].passed


def test_check_recommend_failure_paths():
    expect = {"valid_ids": ["a", "b"], "exclude_ids": ["a"]}
    good = {"problem_id": "b", "reason": "「贪心」掌握度 30 分，贴近目标难度 55"}
    assert all(c.passed for c in check_recommend(good, expect))

    bad = {"problem_id": "a", "reason": "这道题很适合你"}
    results = _by_name(check_recommend(bad, expect))
    assert not results["不推荐最近做过的题"].passed
    assert not results["理由引用了具体数字"].passed

    hallucinated = {"problem_id": "不存在", "reason": "掌握度 30 分"}
    results = _by_name(check_recommend(hallucinated, expect))
    assert not results["推荐题来自候选（防幻觉）"].passed
