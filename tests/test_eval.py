"""难度标注一致性评测（纳入测试套件，防止后续改动造成回归）。

守护的核心主张：
  * 两阶段标注可复现（同一输入多次运行结果完全一致）；
  * 模型输出永不越出规则窗口（越界率必须为 0）；
  * 与人工标注基准的 Cohen's Kappa >= 0.7；
  * 校准层在"无额外信息"时不得损害与人工标注的一致性。
"""

from __future__ import annotations

from app.core.pipeline import annotate_problem
from app.core.rules import difficulty_window, rule_difficulty
from scripts.eval_difficulty import CASES, cohens_kappa, ordinal_distance

CONFIG = {"provider": "mock", "model": "mock-1", "temperature": 0.0}


async def _annotate_all() -> tuple[list[str], list[str], list[str], int, int]:
    human: list[str] = []
    rule_labels: list[str] = []
    labels: list[str] = []
    violations = 0
    total = 0
    for case in CASES:
        payload = {k: v for k, v in case.items() if k != "human"}
        rule_out = rule_difficulty(payload)
        lo, hi = difficulty_window(rule_out)
        result = await annotate_problem(payload, CONFIG)
        score = float(result["difficulty_score"])
        total += 1
        if not (lo - 1e-6 <= score <= hi + 1e-6):
            violations += 1
        human.append(str(case["human"]))
        rule_labels.append(rule_out.difficulty)
        labels.append(result["difficulty"])
    return human, rule_labels, labels, violations, total


async def test_annotation_never_leaves_rule_window():
    """核心机制：无论模型说什么，标注结果都被夹回规则窗口。"""
    _human, _rule, _labels, violations, total = await _annotate_all()
    assert total == len(CASES)
    assert violations == 0, f"{violations}/{total} 次越出规则窗口"


async def test_annotation_is_reproducible():
    """可复现：连续两轮标注结果必须逐条相同。"""
    _h1, _r1, labels1, _, _ = await _annotate_all()
    _h2, _r2, labels2, _, _ = await _annotate_all()
    assert labels1 == labels2


async def test_kappa_against_human_baseline():
    """与人工标注基准的一致性必须达标（简历验收口径）。"""
    human, _rule, labels, _, _ = await _annotate_all()
    kappa = cohens_kappa(human, labels)
    assert kappa >= 0.7, f"Kappa={kappa:.4f} 低于 0.7"
    agree = sum(1 for x, y in zip(human, labels, strict=True) if x == y) / len(human)
    assert agree >= 0.75, f"完全一致率={agree:.4f} 低于 0.75"
    assert ordinal_distance(human, labels) <= 0.3


async def test_calibration_layer_never_hurts_consistency():
    """守护测试：无额外信息的校准层不得让一致性变差。

    这是踩过的坑：早期 mock 校准层叠加扰动，12 题里改错 1 题、修正 0 题，
    把 Kappa 从 0.79 压到 0.68。此测试确保该回归不会再次发生。
    """
    human, rule_labels, labels, _, _ = await _annotate_all()
    kappa_rule = cohens_kappa(human, rule_labels)
    kappa_full = cohens_kappa(human, labels)
    assert kappa_full >= kappa_rule - 1e-9, (
        f"两阶段({kappa_full:.4f}) 比纯规则({kappa_rule:.4f}) 更差，校准层在损害一致性"
    )


async def test_rule_engine_covers_all_levels():
    """规则引擎应当覆盖全部五档，不能塌缩到少数几档。"""
    _human, rule_labels, _labels, _, _ = await _annotate_all()
    assert len(set(rule_labels)) >= 4, f"档位覆盖不足: {set(rule_labels)}"


async def test_hard_problems_scored_higher_than_easy():
    """单调性：人工标为难的题，规则分数必须高于人工标为易的题。"""
    from app.schemas import DIFFICULTY_ORDER

    by_level: dict[str, list[float]] = {}
    for case in CASES:
        payload = {k: v for k, v in case.items() if k != "human"}
        by_level.setdefault(str(case["human"]), []).append(
            rule_difficulty(payload).difficulty_score
        )
    ordered = [lvl for lvl in DIFFICULTY_ORDER if lvl in by_level]
    averages = [sum(by_level[lvl]) / len(by_level[lvl]) for lvl in ordered]
    assert averages == sorted(averages), (
        f"分数未随人工档位单调递增: {list(zip(ordered, averages, strict=True))}"
    )
