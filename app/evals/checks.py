"""评估断言库：每个 Agent 一组"硬下限"检查。

每条检查返回 Check(name, passed, detail)。设计标准：
  * **能自动判定**（结构、防幻觉、策略一致性），失败即 eval 失败；
  * 断言的是"pipeline 最终输出"（postprocess 之后），而不是模型原始
    返回 —— 评估对象是系统承诺给用户的东西；
  * 期望值尽量从 rules.py 现算（如提示强度、复习排序基线），避免
    golden set 里沉淀第二份会漂移的口径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from ..core.rules import (
    HINT_COUNT_BY_STRENGTH,
    hint_strength_for,
    mastery_level_for,
)

# 与 rule_mastery 共用同一份档位映射（规则层单一来源，杜绝口径漂移）
level_for_score = mastery_level_for

# 完整代码的特征串。提示（哪怕 pseudocode 档）必须是自然语言 ——
# 出现这些模式说明模型在"替学生解题"，训练价值归零。
_CODE_LEAK_PATTERNS: tuple[str, ...] = (
    "```",
    "def ",
    "print(",
    "printf(",
    "System.out",
    "#include",
    "std::",
    "public class",
    "console.log",
)

# 复习排序保真度下限：与规则基线的 Kendall tau 低于它判失败。
# 0.6 意味着允许"相邻且分差不大"的局部换序，不允许把高优先级的题
# 沉底 —— 那是教练策略层面的错误，不是表达风格差异。
REVIEW_TAU_MIN = 0.6


@dataclass
class Check:
    """一条断言的结果。detail 写"为什么失败"或关键数值，供报告直读。"""

    name: str
    passed: bool
    detail: str = ""


def kendall_tau(model_order: list[str], baseline_order: list[str]) -> float:
    """模型排序 vs 规则基线排序的 Kendall tau（只算两边都出现的题）。

    1.0 = 完全一致；出现"基线认为更紧急的题被排到后面"即产生逆序对。
    模型只挑了部分题（预算截断）时按共同子集计算 —— 截断本身是合规行为。
    """
    rank = {pid: i for i, pid in enumerate(baseline_order)}
    items = [pid for pid in model_order if pid in rank]
    if len(items) < 2:
        return 1.0
    concordant = 0
    discordant = 0
    for (_i, a), (_j, b) in combinations(enumerate(items), 2):
        if rank[a] < rank[b]:
            concordant += 1
        else:
            discordant += 1
    total = concordant + discordant
    if total == 0:
        return 1.0
    return (concordant - discordant) / total


def _has_digit(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def _has_code_leak(texts: list[str]) -> str:
    """返回首个命中的代码特征串，未泄露返回空串。"""
    joined = "\n".join(texts)
    for pattern in _CODE_LEAK_PATTERNS:
        if pattern in joined:
            return pattern
    return ""


# --------------------------------------------------------------- hint
def check_hint(data: dict[str, Any], expect: dict[str, Any]) -> list[Check]:
    """渐进式提示：强度必须等于规则算定值，且不泄露完整代码。"""
    checks: list[Check] = []
    expected_strength, _ = hint_strength_for(
        float(expect["mastery_score"]), int(expect["attempts"])
    )
    checks.append(
        Check(
            "强度档位与规则层一致",
            data.get("strength") == expected_strength,
            f"输出 {data.get('strength')} / 期望 {expected_strength}"
            f"（掌握度 {expect['mastery_score']}、{expect['attempts']} 次提交）",
        )
    )
    expected_count = HINT_COUNT_BY_STRENGTH.get(str(data.get("strength")), 0)
    hints = data.get("hints") or []
    checks.append(
        Check(
            "提示条数与强度对应",
            len(hints) == expected_count,
            f"{len(hints)} 条 / 期望 {expected_count}",
        )
    )
    leak = _has_code_leak([*hints, str(data.get("key_insight", ""))])
    checks.append(
        Check("不泄露完整代码", leak == "", f"检出代码特征串「{leak}」" if leak else "未检出")
    )
    checks.append(
        Check(
            "强度依据引用了具体数字",
            _has_digit(str(data.get("strength_reason", ""))),
            str(data.get("strength_reason", ""))[:60],
        )
    )
    return checks


# --------------------------------------------------------------- review
def check_review(
    data: dict[str, Any], baseline: dict[str, Any], expect: dict[str, Any]
) -> list[Check]:
    """复习队列：防幻觉、预算、Top-1 与规则一致、排序保真度。"""
    checks: list[Check] = []
    ordered = data.get("ordered") or []
    ids = [str(o.get("problem_id", "")) for o in ordered]
    valid = set(expect["valid_ids"])

    bad = [i for i in ids if i not in valid]
    checks.append(
        Check(
            "题目全部来自候选（防幻觉）",
            not bad,
            f"越界 id: {bad}" if bad else f"{len(ids)}/{len(valid)} 候选",
        )
    )

    max_items = int(expect["max_items"])
    checks.append(
        Check(
            "时间预算不被超出",
            len(ordered) <= max_items,
            f"{len(ordered)} 题 / 预算 {max_items} 题",
        )
    )

    baseline_ids = [str(o.get("problem_id", "")) for o in (baseline.get("ordered") or [])]
    top1_ok = bool(ids) and bool(baseline_ids) and ids[0] == baseline_ids[0]
    checks.append(
        Check(
            "Top-1 与规则基线一致",
            top1_ok,
            f"输出 {ids[0] if ids else '∅'} / 基线 {baseline_ids[0] if baseline_ids else '∅'}",
        )
    )

    no_num = [o.get("problem_id") for o in ordered if not _has_digit(str(o.get("reason", "")))]
    checks.append(
        Check(
            "每条理由引用具体数字",
            not no_num,
            f"缺数字的理由: {no_num}" if no_num else "全部引用逾期天数/掌握度",
        )
    )

    tau = kendall_tau(ids, baseline_ids)
    checks.append(
        Check(
            f"排序保真度 tau≥{REVIEW_TAU_MIN}",
            tau >= REVIEW_TAU_MIN,
            f"tau={tau:.2f}（基线 {baseline_ids} vs 输出 {ids}）",
        )
    )
    return checks


# --------------------------------------------------------------- mastery
def check_mastery(
    data: dict[str, Any], baseline: dict[str, Any], expect: dict[str, Any]
) -> list[Check]:
    """掌握度：分数贴近规则基线、档位与分数一致、强弱项不编造。"""
    checks: list[Check] = []
    score = float(data.get("score", -1))
    rule_score = float(baseline.get("score", score))
    drift = abs(score - rule_score)
    max_drift = float(expect.get("max_drift", 15.0))
    checks.append(
        Check(
            f"分数贴近规则基线（|Δ|≤{max_drift:.0f}）",
            drift <= max_drift,
            f"输出 {score:.1f} / 基线 {rule_score:.1f}（Δ={drift:.1f}）",
        )
    )
    checks.append(
        Check(
            "档位与分数自洽",
            data.get("level") == level_for_score(score),
            f"level={data.get('level')} / score={score:.1f} 应为 {level_for_score(score)}",
        )
    )
    tags = set(expect["tags"])
    weak = set(data.get("weak_tags") or [])
    strong = set(data.get("strong_tags") or [])
    outside = (weak | strong) - tags
    checks.append(
        Check(
            "强弱项全部来自输入标签（不编造知识点）",
            not outside,
            f"越界标签: {sorted(outside)}" if outside else "全部在输入标签内",
        )
    )
    checks.append(Check("强弱项不重叠", not (weak & strong), f"重叠: {sorted(weak & strong)}"))
    if "min_score" in expect:
        checks.append(
            Check(
                f"分数不低于 {expect['min_score']}",
                score >= float(expect["min_score"]),
                f"输出 {score:.1f}",
            )
        )
    if "max_score" in expect:
        checks.append(
            Check(
                f"分数不高于 {expect['max_score']}",
                score <= float(expect["max_score"]),
                f"输出 {score:.1f}",
            )
        )
    if "max_confidence" in expect:
        conf = float(data.get("confidence", 1.0))
        checks.append(
            Check(
                f"低样本置信度 ≤{expect['max_confidence']}",
                conf <= float(expect["max_confidence"]),
                f"confidence={conf}",
            )
        )
    return checks


# --------------------------------------------------------------- roadmap
def check_roadmap(data: dict[str, Any], expect: dict[str, Any]) -> list[Check]:
    """路线图：标签不编造、先修关系在前、总周数守预算。"""
    checks: list[Check] = []
    stages = data.get("stages") or []
    allowed = set(expect["tag_names"])

    used = {str(t) for st in stages for t in (st.get("tags") or [])}
    outside = used - allowed
    checks.append(
        Check(
            "阶段标签全部来自输入（不编造知识点）",
            not outside,
            f"越界标签: {sorted(outside)}" if outside else "全部在输入标签内",
        )
    )

    def first_stage_of(tag: str) -> int:
        for i, st in enumerate(stages):
            if tag in (st.get("tags") or []):
                return i
        return -1

    violated = []
    for before, after in expect.get("prereq_pairs", []):
        i_before, i_after = first_stage_of(before), first_stage_of(after)
        if i_after >= 0 and (i_before < 0 or i_before > i_after):
            violated.append(f"{before} 应先于 {after}")
    checks.append(
        Check(
            "先修关系排在后面之前",
            not violated,
            "；".join(violated) if violated else "拓扑序成立",
        )
    )

    weeks_budget = int(expect["weeks"])
    total = sum(int(st.get("weeks", 0)) for st in stages)
    checks.append(
        Check(
            f"总周数不超过预算 2 倍（{weeks_budget} 周）",
            total <= weeks_budget * 2,
            f"合计 {total} 周",
        )
    )
    return checks


# --------------------------------------------------------------- recommend
def check_recommend(data: dict[str, Any], expect: dict[str, Any]) -> list[Check]:
    """下一题推荐：必须在候选内、不得推荐最近做过的、理由引用数字。"""
    checks: list[Check] = []
    pid = str(data.get("problem_id", ""))
    checks.append(
        Check(
            "推荐题来自候选（防幻觉）",
            pid in set(expect["valid_ids"]),
            f"输出 {pid or '∅'}",
        )
    )
    excluded = set(expect.get("exclude_ids") or [])
    checks.append(
        Check(
            "不推荐最近做过的题",
            pid not in excluded,
            f"命中近期题 {pid}" if pid in excluded else "未命中",
        )
    )
    checks.append(
        Check(
            "理由引用了具体数字",
            _has_digit(str(data.get("reason", ""))),
            str(data.get("reason", ""))[:60],
        )
    )
    return checks
