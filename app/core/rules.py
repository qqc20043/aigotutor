"""规则引擎：每个 Agent 的确定性兜底实现。

定位：不依赖任何模型，纯 Python 计算，永远能给出合法结果。
  * 难度规则引擎：给"规则区间"（第一阶段），LLM 只在区间内选（第二阶段校准）；
  * 推荐规则引擎：按 掌握度缺口 × 难度贴合 × 遗忘紧迫度 打分排序；
  * 题解规则引擎：从题库预置模板/题解缓存取；
  * 评测规则引擎：按 verdict + 失败用例特征归因；
  * 掌握度规则引擎：加权正确率 + 样本量置信度；
  * 路线图规则引擎：按标签先修关系拓扑分层。

降级时这些函数产出的对象必须能通过 app/schemas.py 的同一份 schema 校验。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from ..schemas import (
    DIFFICULTY_BOUNDS,
    DIFFICULTY_ORDER,
    DIFFICULTY_RADIUS,
    DifficultyOut,
    HintOut,
    JudgeOut,
    MasteryOut,
    QualityDimension,
    QualityOut,
    RecommendOut,
    ReviewPriorityItem,
    ReviewPriorityOut,
    RoadmapOut,
    RoadmapStage,
    SolutionOut,
    difficulty_of,
)


# ------------------------------------------------------------ 难度规则引擎
def rule_difficulty(features: dict[str, Any]) -> DifficultyOut:
    """第一阶段：只用可量化特征算难度，输出档位 + 连续分。

    特征（全部可从题面静态提取，零成本、可复现）：
      statement_len   题面长度
      tag_count       知识点数量
      algo_depth      算法深度 1-5（贪心/DP/图论等，由标签映射）
      constraint_tight 数据范围是否到 1e5/1e9（需要优化）
      is_constructive  是否构造题
      pass_rate       通过率（有则用）
      ac_count        通过人数（有则用）
    """
    stmt_len = int(features.get("statement_len", 0))
    tag_count = int(features.get("tag_count", 1))
    algo_depth = int(features.get("algo_depth", 1))
    tight = bool(features.get("constraint_tight", False))
    constructive = bool(features.get("is_constructive", False))
    pass_rate = features.get("pass_rate")

    # 权重经离线标定：以人工标注档位为目标，拟合出 base=8、深度权重=15。
    # 早期版本用 base=5 / 深度=11，评测显示相对人工标注系统性偏低约一档
    # （Kappa 仅 0.34），此处按评测结果修正，而非拍脑袋取值。
    score = 8.0
    score += min(stmt_len / 120.0, 12.0)  # 题面越长越难，封顶 12
    score += min(max(tag_count - 1, 0) * 6.0, 18.0)  # 综合题每多一个标签 +6
    score += (algo_depth - 1) * 15.0  # 算法深度主权重
    score += 8.0 if tight else 0.0  # 需要优化
    score += 5.0 if constructive else 0.0
    if isinstance(pass_rate, (int, float)):
        score += max(0.0, (0.75 - float(pass_rate)) * 30.0)

    score = max(0.0, min(100.0, score))
    return DifficultyOut(
        difficulty=difficulty_of(score),  # type: ignore[arg-type]
        difficulty_score=round(score, 2),
        tags=list(features.get("tags") or ["未分类"])[:4],
        reason=(
            f"规则引擎：题面 {stmt_len} 字、{tag_count} 个知识点、算法深度 {algo_depth}"
            f"{'、数据范围需优化' if tight else ''}，综合得分 {score:.1f}。"
        ),
    )


def difficulty_window(rule_out: DifficultyOut) -> tuple[float, float]:
    """把规则档位扩成"最多 ±1 档"的连续分数窗口，供第二阶段 LLM 校准约束。

    窗口上下界都严格落在 ±1 档的分数区间内（上界取开区间，避免落到下一档的
    下界上），从而保证：模型无论给出什么分数，夹紧后的档位与规则档位最多差 1 档。
    """
    n = len(DIFFICULTY_ORDER)
    idx = DIFFICULTY_ORDER.index(rule_out.difficulty)
    eps = 1e-6
    lo_floor = DIFFICULTY_BOUNDS[max(0, idx - 1)]
    hi_ceil = DIFFICULTY_BOUNDS[idx + 2] - eps if idx + 2 < n else 100.0
    lo = max(lo_floor, DIFFICULTY_BOUNDS[idx] - DIFFICULTY_RADIUS)
    hi = min(hi_ceil, DIFFICULTY_BOUNDS[idx + 1] + DIFFICULTY_RADIUS)
    return max(0.0, lo), min(100.0, max(lo + eps, hi))


def clamp_to_window(out: DifficultyOut, window: tuple[float, float]) -> DifficultyOut:
    """第二阶段结果落回规则窗口内：越界则夹紧，档位按夹紧后的分数重算。"""
    lo, hi = window
    score = max(lo, min(hi, out.difficulty_score))
    return out.model_copy(
        update={"difficulty_score": round(score, 2), "difficulty": difficulty_of(score)}
    )


# ------------------------------------------------------------ 推荐规则引擎
def _parse_due(raw: Any) -> datetime | None:
    """把 due_at 归一成带时区的 datetime。

    候选数据可能来自 API（ISO 字符串）或内部（datetime），统一在此处理，
    时间口径统一交给 core.fsrs（技术债 D4：不再各写一份 /86400）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rule_recommend(ctx_data: dict[str, Any]) -> RecommendOut:
    """掌握度缺口 × 难度贴合 × 遗忘紧迫度 打分，排除最近做过的题。"""
    from .fsrs import overdue_days as _overdue_days

    problems: list[dict[str, Any]] = ctx_data.get("candidates") or []
    mastery: dict[str, float] = ctx_data.get("mastery") or {}
    recent: set[str] = set(ctx_data.get("recent_problem_ids") or [])
    target_score = float(ctx_data.get("target_score", 55.0))
    now = datetime.now(timezone.utc)

    best: dict[str, Any] | None = None
    best_score = -1e9
    for p in problems:
        if p.get("id") in recent:
            continue
        tags = p.get("tags") or ["未分类"]
        gap = sum(max(0.0, target_score - float(mastery.get(t, 50.0))) for t in tags) / len(tags)
        fit = 100.0 - abs(float(p.get("difficulty_score", 50.0)) - target_score)
        # 遗忘紧迫度：逾期越久权重越高，封顶 20 分
        urgency = min(20.0, _overdue_days(_parse_due(p.get("due_at")), now) * 2.0)
        s = gap * 1.2 + fit * 0.8 + urgency
        if s > best_score:
            best_score, best = s, p

    if best is None:
        return RecommendOut(
            problem_id="",
            title="",
            difficulty="入门",
            tags=["未分类"],
            reason="暂无候选题目（题库为空或全部近期已做）。",
            expected_gain=0.0,
        )

    tags = best.get("tags") or ["未分类"]
    weakest = min(tags, key=lambda t: float(mastery.get(t, 50.0)))
    return RecommendOut(
        problem_id=str(best.get("id", "")),
        title=str(best.get("title", "")),
        difficulty=str(best.get("difficulty", "普及-")),  # type: ignore[arg-type]
        tags=tags[:4],
        reason=(
            f"规则引擎：你的「{weakest}」掌握度 "
            f"{float(mastery.get(weakest, 50.0)):.0f} 分，该题难度 "
            f"{float(best.get('difficulty_score', 50.0)):.0f} 分贴近当前目标 "
            f"{target_score:.0f} 分，综合评分 {best_score:.1f}。"
        ),
        expected_gain=round(max(0.0, min(20.0, best_score / 20.0)), 2),
    )


# ------------------------------------------------------------ 题解规则引擎
def rule_solution(ctx_data: dict[str, Any]) -> SolutionOut | None:
    """规则引擎兜底题解。

    优先级：题库预置题解 -> 用户提交的代码骨架 -> 通用可运行模板。
    最后一种只保证"能跑通输入输出"，不假装给出了正确算法 —— 结果里
    会明确标注是框架而非正解，避免降级时输出误导性内容。
    """
    preset = ctx_data.get("preset_solution")
    if isinstance(preset, dict):
        return SolutionOut.model_validate(preset)

    language = str(ctx_data.get("language", "python"))
    tags = ctx_data.get("tags") or []
    tag_text = "、".join(tags) if tags else "基础算法"
    starter = ctx_data.get("starter_code")

    if starter:
        return SolutionOut(
            summary=f"规则引擎：基于你的代码骨架给出可运行结构，涉及 {tag_text}。",
            approach=[
                "读取并解析输入",
                "按题目约束选择数据结构",
                "实现核心逻辑并处理边界",
                "输出结果并自测样例",
            ],
            code=str(starter),
            language=language,
            complexity={"time": "取决于实现", "space": "取决于实现"},
            pitfalls=["注意空输入与最小规模边界", "注意数据范围是否超出朴素做法上限"],
        )

    template = _generic_template(language)
    if template is None:
        return None
    return SolutionOut(
        summary=f"规则引擎：模型不可用，给出 {language} 的通用输入输出框架（非正解，涉及 {tag_text}）。",
        approach=[
            "按题面确认输入格式与规模",
            "选择匹配数据规模的算法与数据结构",
            "实现主逻辑并覆盖边界用例",
            "用样例自测后提交",
        ],
        code=template,
        language=language,
        complexity={"time": "待人工确认", "space": "待人工确认"},
        pitfalls=[
            "此为降级模板，不包含本题算法，需人工补全",
            "注意空输入、单元素、最大规模三类边界",
        ],
    )


def _generic_template(language: str) -> str | None:
    """各语言的通用读写模板，保证语法可运行。"""
    templates = {
        "python": (
            "import sys\n\n\ndef main() -> None:\n"
            "    data = sys.stdin.read().split()\n"
            "    if not data:\n        return\n"
            "    # TODO: 按题面实现核心逻辑\n"
            "    print(0)\n\n\nif __name__ == '__main__':\n    main()\n"
        ),
        "cpp": (
            "#include <bits/stdc++.h>\nusing namespace std;\n\n"
            "int main() {\n    ios::sync_with_stdio(false);\n    cin.tie(nullptr);\n"
            "    // TODO: 按题面实现核心逻辑\n    cout << 0 << '\\n';\n    return 0;\n}\n"
        ),
        "java": (
            "import java.util.*;\n\npublic class Main {\n"
            "    public static void main(String[] args) {\n"
            "        Scanner sc = new Scanner(System.in);\n"
            "        // TODO: 按题面实现核心逻辑\n"
            "        System.out.println(0);\n    }\n}\n"
        ),
    }
    return templates.get(language.lower())


# ------------------------------------------------------------ 评测规则引擎
_PITFALL_KEYWORDS: list[tuple[str, str]] = [
    ("索引", "边界"),
    ("下标", "边界"),
    ("越界", "边界"),
    ("边界", "边界"),
    ("超时", "复杂度"),
    ("TLE", "复杂度"),
    ("复杂度", "复杂度"),
    ("递归", "实现细节"),
    ("溢出", "实现细节"),
    ("理解", "理解偏差"),
    ("题意", "理解偏差"),
]


def rule_judge(ctx_data: dict[str, Any]) -> JudgeOut:
    """按 verdict + 失败用例 + 代码特征做确定性归因。"""
    verdict = str(ctx_data.get("verdict", "WA")).upper()
    if verdict not in ("AC", "WA", "TLE", "MLE", "RE", "CE"):
        verdict = "WA"
    detail = json.dumps(ctx_data.get("judge_detail") or {}, ensure_ascii=False)
    code = str(ctx_data.get("code", ""))
    haystack = detail + code

    root = "未知"
    for kw, cause in _PITFALL_KEYWORDS:
        if kw in haystack:
            root = cause
            break
    if verdict == "TLE":
        root = "复杂度"
    elif verdict == "MLE":
        root = "实现细节"
    elif verdict == "RE":
        root = "边界"
    elif verdict == "CE":
        root = "实现细节"

    table = {
        "AC": ("通过全部用例，实现与题意一致。", "可尝试同标签更高难度题目巩固。"),
        "WA": ("存在用例输出不符。", "用最小反例手算，重点检查边界与循环终止条件。"),
        "TLE": ("算法复杂度过高，超出时间限制。", "评估是否可用排序/二分/前缀和/DP 降复杂度。"),
        "MLE": ("内存占用超出限制。", "检查是否开了过大数组或缓存未释放。"),
        "RE": ("运行时错误，通常为越界或空集合访问。", "检查数组下标范围与递归基例。"),
        "CE": ("编译/语法错误。", "按编译器首条报错逐条修复。"),
    }
    explanation, fix = table[verdict]
    return JudgeOut(
        verdict=verdict,  # type: ignore[arg-type]
        root_cause=root,  # type: ignore[arg-type]
        explanation=f"规则引擎归因：{explanation}",
        fix_suggestion=fix,
        confidence=0.55,
    )


# ---------------------------------------------------------- 提示规则引擎
# 提示强度的权威口径：掌握度越低给越强的提示。
# 阈值定义在规则层而不是 prompt 里，好处是强度可复现、可测试，
# 模型只负责"在该强度下写提示内容"，无权自行升降档。
HINT_STRENGTH_THRESHOLDS: list[tuple[float, str]] = [
    (40.0, "pseudocode"),  # 掌握度 < 40：给到伪代码级
    (65.0, "key_observation"),  # 40-65：给关键观察
    (100.0, "direction"),  # >= 65：只给方向
]

# 每档强度对应的提示条数（弱提示 1 条，强提示 3 条递进）
HINT_COUNT_BY_STRENGTH: dict[str, int] = {
    "direction": 1,
    "key_observation": 2,
    "pseudocode": 3,
}


def hint_strength_for(mastery_score: float, attempts: int = 0) -> tuple[str, str]:
    """按掌握度决定提示强度，返回 (强度, 依据说明)。

    两个额外考虑（都来自真实学习场景，不是拍脑袋）：
      * **样本不足时升一档**：只做过 1-2 次的题目，掌握度数字本身不可信，
        此时按数字给弱提示会让新手卡死；样本 < 3 时至少给到 key_observation。
      * **从未做过（attempts == 0）直接给最强**：第一次接触的知识点，
        给方向等于没说。
    """
    score = max(0.0, min(100.0, float(mastery_score)))

    if attempts <= 0:
        return "pseudocode", "该知识点尚无提交记录（0 次），首次接触给最强提示。"
    if attempts < 3:
        return "key_observation", (
            f"该知识点仅 {attempts} 次提交，样本不足以判断掌握度，保守给中等强度提示。"
        )

    for threshold, strength in HINT_STRENGTH_THRESHOLDS:
        if score < threshold:
            return (
                strength,
                f"该知识点掌握度 {score:.0f} 分（{attempts} 次提交），据此给出对应强度提示。",
            )
    return "direction", f"该知识点掌握度 {score:.0f} 分，掌握较好，只给方向性提示。"


def rule_hint(ctx_data: dict[str, Any]) -> HintOut:
    """提示规则引擎：不依赖模型，按强度给出模板化但有针对性的提示。

    诚实原则：模板不知道具体算法，因此**不假装给出解法**，而是给出
    "该强度下应有的思考框架" + 明确的下一步动作，并标注这是降级结果。
    """
    tags = ctx_data.get("tags") or []
    tag_text = "、".join(tags[:3]) if tags else "本题涉及的知识点"
    mastery = float(ctx_data.get("mastery_score", 50.0))
    attempts = int(ctx_data.get("attempts", 0))
    strength, reason = hint_strength_for(mastery, attempts)
    count = HINT_COUNT_BY_STRENGTH[strength]

    pool = {
        "direction": [
            f"先别急着写代码：把题面的输入输出与数据范围抄一遍，判断{tag_text}里哪一步是瓶颈。",
            "用小规模样例手算一遍，观察答案是怎么一步步得到的。",
            "如果朴素做法会超时，想想能否先排序/预处理再线性扫描。",
        ],
        "key_observation": [
            f"关键观察：{tag_text}类问题通常有一个「单调性」或「可合并」的性质，先把它找出来。",
            "检查一下：当前做法在最坏情况下要处理多少次元素？对照数据范围看是否可行。",
            "边界条件单独列出来（空输入、单元素、完全包含/完全不相交），逐条确认。",
        ],
        "pseudocode": [
            f"步骤 1：读入并按{tag_text}所需的结构整理数据。",
            "步骤 2：写主循环 —— 每次迭代维护当前状态，并判断何时该更新答案。",
            "步骤 3：输出答案，并用样例与边界各测一遍再提交。",
        ],
    }[strength]

    return HintOut(
        strength=strength,  # type: ignore[arg-type]
        hints=pool[:count],
        key_insight=f"规则引擎提示：重点在「{tag_text}」的性质识别与边界处理（未包含完整解法）。",
        next_step="按上面第 1 条动手写第一版代码，跑一遍样例看输出差在哪。",
        strength_reason=f"规则引擎：{reason}",
        confidence=0.5,
    )


# ------------------------------------------------------ 复习队列优先规则引擎
# 排序信号权重（可复现、可测试；模型只能在候选里重排，不能改变评分口径）
REVIEW_W_OVERDUE = 3.0  # 每逾期一天 +3 分
REVIEW_W_MASTERY_GAP = 0.8  # 掌握度缺口（100 - 掌握度）× 0.8
REVIEW_W_LAPSE = 6.0  # 每次遗忘 +6 分（反复出错的题优先巩固）
REVIEW_MINUTES_PER_PROBLEM = 15  # 时间预算换算：每题按 15 分钟估


def rule_review_priority(ctx_data: dict[str, Any]) -> ReviewPriorityOut:
    """规则引擎排序：逾期 × 掌握度缺口 × 遗忘次数，并遵守时间预算。

    打分公式（写在注释里是为了让"为什么这题排第一"可追溯）：
        score = 逾期天数 × 3 + (100 - 掌握度) × 0.8 + 遗忘次数 × 6
    各项权重是刻意选的量级：逾期天数通常 0-10，掌握度缺口 0-100，
    遗忘次数通常 0-3，乘完权重后三者对结果都有实际影响，不会互相淹没。
    """
    candidates = ctx_data.get("candidates") or []
    budget = int(ctx_data.get("time_budget_minutes", 0) or 0)

    scored: list[tuple[float, dict[str, Any], float, float, int]] = []
    for p in candidates:
        overdue = float(p.get("overdue_days", 0.0) or 0.0)
        lapses = int(p.get("lapses", 0) or 0)
        mastery_scores: dict[str, float] = p.get("mastery_scores") or {}
        tags = p.get("tags") or []
        # 掌握度缺口：取该题各标签里最弱的一项（最短板决定复习收益）
        if mastery_scores and tags:
            weakest = min(float(mastery_scores.get(t, 50.0)) for t in tags)
        elif mastery_scores:
            weakest = min(float(v) for v in mastery_scores.values())
        else:
            weakest = 50.0
        gap = 100.0 - weakest
        score = overdue * REVIEW_W_OVERDUE + gap * REVIEW_W_MASTERY_GAP + lapses * REVIEW_W_LAPSE
        scored.append((score, p, overdue, weakest, lapses))

    scored.sort(key=lambda x: x[0], reverse=True)

    # 时间预算：超出预算的截掉（宁可少挑，也不给做不完的队列）
    if budget > 0:
        max_items = max(1, budget // REVIEW_MINUTES_PER_PROBLEM)
        scored = scored[:max_items]

    ordered: list[ReviewPriorityItem] = []
    for i, (score, p, overdue, weakest, lapses) in enumerate(scored):
        bits = []
        if overdue > 0:
            bits.append(f"逾期 {overdue:.1f} 天")
        bits.append(f"「{_weakest_tag(p, weakest)}」掌握度 {weakest:.0f} 分")
        if lapses > 0:
            bits.append(f"已遗忘 {lapses} 次")
        ordered.append(
            ReviewPriorityItem(
                problem_id=str(p.get("problem_id") or p.get("id") or ""),
                title=str(p.get("title", "")),
                order=i + 1,
                reason=f"规则引擎：{'、'.join(bits)}，综合优先级 {score:.1f}。",
                urgency=(f"逾期 {overdue:.1f} 天" if overdue > 0 else "今日到期"),
            )
        )

    if not ordered:
        # schema 要求 ordered 至少 1 项；空队列时给一个语义明确的占位项
        ordered = [
            ReviewPriorityItem(
                problem_id="__none__",
                title="",
                order=1,
                reason="规则引擎：当前没有到期的复习项。",
                urgency="无待复习",
            )
        ]

    budget_note = (
        f"时间预算 {budget} 分钟（按每题 {REVIEW_MINUTES_PER_PROBLEM} 分钟估算，"
        f"共挑出 {len(ordered)} 道）。"
        if budget > 0
        else ""
    )
    return ReviewPriorityOut(
        ordered=ordered,
        strategy=(
            f"规则引擎：按「逾期紧迫度 ×3 + 掌握度缺口 ×0.8 + 遗忘次数 ×6」降序排列，"
            f"优先补最短板知识点。{budget_note}"
        ),
        confidence=0.6,
    )


def _weakest_tag(problem: dict[str, Any], weakest_score: float) -> str:
    """回填最弱标签名，仅用于生成可读理由。"""
    mastery_scores: dict[str, float] = problem.get("mastery_scores") or {}
    tags = problem.get("tags") or []
    if mastery_scores and tags:
        return min(tags, key=lambda t: float(mastery_scores.get(t, 50.0)))
    if tags:
        return str(tags[0])
    return "综合"


# ------------------------------------------------------ 代码质量两阶段评分
# 规则阶段：只算**客观可量化**的指标。这些不需要模型判断，且必须可复现 ——
# 同一份代码跑两次必须得到同一个分数，否则"两阶段"里的第一阶段就失去了
# 作为约束的意义（LLM 会在一个随机基线上做修正）。
QUALITY_W_STRUCTURE = 0.35  # 结构（嵌套深度、代码行数）
QUALITY_W_NAMING = 0.25  # 命名（标识符可读性）
QUALITY_W_COMMENT = 0.15  # 注释与说明
QUALITY_W_BOUNDARY = 0.25  # 边界处理迹象（显式判断、异常处理）


def _quality_metrics(code: str) -> dict[str, float]:
    """提取可量化的代码特征。全部是纯文本统计，不需要解析器。

    为什么不用 AST：本项目支持 Python/C++/Java 三种语言，维护三套解析器
    成本过高，而"缩进深度、标识符长度、注释比例"这类指标用行级统计已经
    足够稳定（它们是启发式，不需要绝对精确 —— 精确判断留给第二阶段的模型）。
    """
    import re as _re

    lines = [ln for ln in (code or "").splitlines() if ln.strip()]
    if not lines:
        return {
            "lines": 0.0,
            "max_indent": 0.0,
            "comment_ratio": 0.0,
            "avg_ident_len": 0.0,
            "short_ident_ratio": 0.0,
            "boundary_hints": 0.0,
            "_structure": 20.0,
            "_naming": 20.0,
            "_comment": 45.0,
            "_boundary": 40.0,
        }

    indents: list[int] = []
    comment_lines = 0
    identifiers: list[str] = []
    for raw in lines:
        stripped = raw.lstrip()
        if stripped.startswith(("#", "//", "/*", "*", "--")):
            comment_lines += 1
            continue
        # 缩进宽度按 4 空格折算（tab 记 4）
        indent = len(raw) - len(stripped)
        indents.append(indent // 4)
        for token in _re.findall(r"[A-Za-z_][A-Za-z_0-9]{1,}", stripped):
            identifiers.append(token)

    max_indent = max(indents) if indents else 0
    short = sum(1 for t in identifiers if len(t) <= 2)
    avg_ident = sum(len(t) for t in identifiers) / len(identifiers) if identifiers else 0

    # 边界处理迹象：只认**真正的边界检查模式**，而不是任意 if。
    # 早期版本把任何 "if" 都算作边界处理，结果深嵌套的条件分支也被加了分 ——
    # 实测一份 6 层嵌套、全是单字母变量的代码因此拿到"合格"档，评分失去区分度。
    # 这里改为匹配具体的边界语义：空值判断、零值比较、异常处理、越界保护。
    boundary_patterns = (
        r"\bif\s+not\s+\w+",  # if not x（空值判断）
        r"\bif\s+\w+\s*==\s*0\b",  # == 0（零值）
        r"\bif\s+\w+\s*<=\s*0\b",  # <= 0
        r"\bif\s+len\(",  # if len(...)
        r"\btry\s*:",  # try
        r"\bexcept\b",  # except
        r"\bcatch\s*\(",  # catch (
        r"\bthrow\b",  # throw
        r"\braise\b",  # raise
        r"\bassert\b",  # assert
        r"==\s*null\b",  # == null
        r"==\s*None\b",  # == None
        r"\bis\s+None\b",  # is None
        r"\bempty\b",  # empty
        r"\bINT_MAX\b|\bINT_MIN\b",  # 极值保护
    )
    haystack = code or ""
    boundary_hits = sum(len(_re.findall(pat, haystack)) for pat in boundary_patterns)

    # --- 结构分：嵌套过深扣分，代码过长扣分 ---
    structure = 100.0
    structure -= min(40.0, max(0.0, (max_indent - 2)) * 12.0)  # 超过 2 层开始扣
    structure -= min(30.0, max(0.0, (len(lines) - 60)) * 0.5)  # 超过 60 行开始扣
    structure = max(20.0, structure)

    # --- 命名分：短标识符越多越差，平均长度过短也扣 ---
    naming = 100.0
    naming -= min(50.0, (short / max(1, len(identifiers))) * 100.0)
    naming -= min(20.0, max(0.0, (5.0 - avg_ident)) * 6.0)
    naming = max(20.0, naming)

    # --- 注释分：有注释即可，不追求高比例（代码自解释优于注释堆砌）---
    ratio = comment_lines / max(1, len(lines))
    if ratio <= 0:
        comment = 45.0
    elif ratio < 0.05:
        comment = 65.0
    elif ratio < 0.35:
        comment = 95.0
    else:
        comment = 80.0  # 注释过多往往是代码不清晰的信号

    # --- 边界分：按显式判断的密度给分（封顶）---
    density = boundary_hits / max(1.0, len(lines) / 10.0)
    boundary = min(100.0, 40.0 + density * 15.0)

    return {
        "lines": float(len(lines)),
        "max_indent": float(max_indent),
        "comment_ratio": round(ratio, 3),
        "avg_ident_len": round(avg_ident, 3),
        "short_ident_ratio": round(short / max(1, len(identifiers)), 3),
        "boundary_hints": float(boundary_hits),
        "_structure": round(structure, 2),
        "_naming": round(naming, 2),
        "_comment": round(comment, 2),
        "_boundary": round(boundary, 2),
    }


def quality_window(code: str) -> tuple[float, dict[str, float]]:
    """第一阶段：算出规则基线与允许的修正窗口。

    返回 (规则基线分, 指标明细)。第二阶段的 LLM 只能在 [baseline-R, baseline+R]
    内调整总分 —— 与难度标注的"规则给区间 -> 模型在区间内校准"是同一套机制。
    """
    m = _quality_metrics(code)
    baseline = (
        m["_structure"] * QUALITY_W_STRUCTURE
        + m["_naming"] * QUALITY_W_NAMING
        + m["_comment"] * QUALITY_W_COMMENT
        + m["_boundary"] * QUALITY_W_BOUNDARY
    )
    return round(max(0.0, min(100.0, baseline)), 2), m


def clamp_quality(out: QualityOut, window: tuple[float, float]) -> QualityOut:
    """第二阶段结果落回规则窗口内：越界夹紧，等级按夹紧后的分数重算。

    这是"两阶段"的关键约束：LLM 可以做语义修正，但不能推翻客观指标
    （比如把一段 8 层嵌套、全是单字母变量的代码评为"优秀"）。
    """
    lo, hi = window
    score = max(lo, min(hi, float(out.score)))
    return out.model_copy(update={"score": round(score, 2), "grade": quality_grade_of(score)})


def quality_grade_of(score: float) -> str:
    """分数 -> 等级的唯一口径（全系统共用，杜绝不一致）。"""
    if score >= 85.0:
        return "优秀"
    if score >= 70.0:
        return "良好"
    if score >= 55.0:
        return "合格"
    return "待改进"


def rule_quality(ctx_data: dict[str, Any]) -> QualityOut:
    """规则引擎兜底：直接用第一阶段的客观指标产出评分。

    诚实原则：不假装做了语义判断 —— summary 里明确标注这是基于量化指标的
    评分，未做语义修正。用户能据此知道"这分数只反映形式指标"。
    """
    code = str(ctx_data.get("code", ""))
    baseline, m = quality_window(code)
    readable = round((m["_structure"] + m["_naming"]) / 2, 1)
    return QualityOut(
        score=baseline,
        grade=quality_grade_of(baseline),  # type: ignore[arg-type]
        dimensions=[
            QualityDimension(
                name="可读性",
                score=readable,
                comment=(
                    f"最大嵌套 {int(m['max_indent'])} 层、平均标识符长度 "
                    f"{m['avg_ident_len']:.1f} 字符、短标识符占比 "
                    f"{m['short_ident_ratio']:.0%}。"
                ),
            ),
            QualityDimension(
                name="边界处理",
                score=round(m["_boundary"], 1),
                comment=f"检出 {int(m['boundary_hints'])} 处显式判断/异常处理。",
            ),
        ],
        summary=(
            f"规则引擎评分：基于 {int(m['lines'])} 行代码的量化指标"
            f"（结构/命名/注释/边界），未做语义修正。"
        ),
        top_improvement=(
            "减少嵌套层级，并把单字母变量改成有含义的名字。"
            if m["max_indent"] > 3 or m["short_ident_ratio"] > 0.5
            else "补充边界条件的显式判断（空输入、最小规模、最大值）。"
        ),
        confidence=0.5,
    )


# ---------------------------------------------------------- 掌握度规则引擎
# 档位映射单一来源：分数 -> L1..L5。rule_mastery 的两条路径（有样本/零样本）
# 与评估断言（app/evals/checks.py）都必须引用这里，避免口径漂移。
# 真实故障：零样本分支曾硬编码 level="L3"，而 50 分按主路径映射应为 L2 ——
# 同一个分数在不同路径下展示不同档位（行为评估首轮暴露）。
MASTERY_LEVEL_BANDS: tuple[tuple[str, float], ...] = (
    ("L2", 40.0),
    ("L3", 55.0),
    ("L4", 70.0),
    ("L5", 85.0),
)


def mastery_level_for(score: float) -> str:
    level = "L1"
    for name, threshold in MASTERY_LEVEL_BANDS:
        if score >= threshold:
            level = name
    return level


def rule_mastery(ctx_data: dict[str, Any]) -> MasteryOut:
    """加权正确率 + 样本量置信度，输出 0-100 与五级。"""
    tag_stats: dict[str, dict[str, float]] = ctx_data.get("tag_stats") or {}
    if not tag_stats:
        return MasteryOut(
            score=50.0,
            level=mastery_level_for(50.0),  # type: ignore[arg-type]
            confidence=0.1,
            weak_tags=[],
            strong_tags=[],
            suggestion="样本不足，建议先完成 5 道入门题以建立基线。",
        )

    weighted_num = 0.0
    weighted_den = 0.0
    total_attempts = 0.0
    for _tag, st in tag_stats.items():
        attempts = float(st.get("attempts", 0))
        ac = float(st.get("ac_count", 0))
        if attempts <= 0:
            continue
        rate = ac / attempts
        w = math.log1p(attempts)  # 样本越多权重越高，但边际递减
        weighted_num += rate * w
        weighted_den += w
        total_attempts += attempts
    base = (weighted_num / weighted_den) if weighted_den else 0.5

    # 难度加成：做过的题平均难度越高，分数越高
    avg_diff = float(ctx_data.get("avg_difficulty_score", 40.0))
    score = base * 70.0 + (avg_diff / 100.0) * 30.0
    score = max(0.0, min(100.0, score))

    # 置信度：样本量 saturating，20 次提交接近 1
    confidence = round(1.0 - math.exp(-total_attempts / 12.0), 3)

    ranked = sorted(
        (
            (t, float(s.get("ac_count", 0)) / max(1.0, float(s.get("attempts", 1))))
            for t, s in tag_stats.items()
        ),
        key=lambda x: x[1],
    )
    # 强项按绝对门槛取，弱项在强项之外取 —— 两者必须互斥。
    # 真实故障：标签数 ≤3 且全部正确率 >0.6 时，ranked[:3] 与 ranked[-3:]
    # 会命中同一批标签，同一个知识点同时出现在"弱项"与"强项"里（行为评估
    # 首轮即暴露）。全部标签都强时弱项为空是诚实语义，不该硬凑。
    strong = [t for t, _ in ranked[-3:] if _ > 0.6][::-1]
    strong_set = set(strong)
    weak = [t for t, _ in ranked[:3] if t not in strong_set]

    level = mastery_level_for(score)

    suggestion = (
        f"优先补强「{weak[0]}」，当前正确率 {ranked[0][1] * 100:.0f}%；"
        f"置信度 {confidence:.2f}（样本 {int(total_attempts)} 次提交）。"
        if weak
        else "继续按计划推进。"
    )
    return MasteryOut(
        score=round(score, 2),
        level=level,  # type: ignore[arg-type]
        confidence=confidence,
        weak_tags=weak,
        strong_tags=strong,
        suggestion=suggestion,
    )


# ---------------------------------------------------------- 路线图规则引擎
def _normalize_tags(raw: Any) -> list[dict[str, Any]]:
    """把任意形状的标签输入归一成 [{id, name, prereq_ids}]。

    降级路径会被各种调用方以不同形状喂数据（str 列表 / dict 列表 / JSON 字符串），
    这里做统一归一，避免规则引擎因为输入形状不符而崩溃。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"id": f"t{i}", "name": item, "prereq_ids": "[]"})
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or f"t{i}")
            out.append(
                {
                    "id": str(item.get("id") or name),
                    "name": name,
                    "prereq_ids": item.get("prereq_ids") or "[]",
                }
            )
    return out


def rule_roadmap(ctx_data: dict[str, Any]) -> RoadmapOut:
    """按标签先修关系 + 掌握度做拓扑分层，输出阶段路线。"""
    tags = _normalize_tags(ctx_data.get("tags"))
    mastery: dict[str, float] = ctx_data.get("mastery") or {}
    weeks_budget = max(1, int(ctx_data.get("weeks", 6) or 6))

    by_id = {t["id"]: t for t in tags}
    prereq_of: dict[str, list[str]] = {}
    for t in tags:
        raw = t.get("prereq_ids") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = []
        prereq_of[t["id"]] = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else []

    # 拓扑分层
    layer_of: dict[str, int] = {}

    def depth(tid: str, seen: set[str] | None = None) -> int:
        seen = seen or set()
        if tid in layer_of:
            return layer_of[tid]
        if tid in seen:
            return 0
        seen.add(tid)
        parents = [p for p in prereq_of.get(tid, []) if p in by_id]
        d = 0 if not parents else 1 + max(depth(p, seen) for p in parents)
        layer_of[tid] = d
        return d

    for tid in by_id:
        depth(tid)

    layers: dict[int, list[str]] = {}
    for tid, d in layer_of.items():
        layers.setdefault(d, []).append(str(by_id[tid]["name"]))

    ordered = [layers[k] for k in sorted(layers)]
    if not ordered:
        ordered = [["模拟", "排序"], ["贪心", "二分"], ["动态规划"]]

    stages: list[RoadmapStage] = []
    per_stage = max(1, weeks_budget // max(1, len(ordered)))
    for i, names in enumerate(ordered[:6]):
        low = [n for n in names if mastery.get(n, 50.0) < 60.0] or names
        stages.append(
            RoadmapStage(
                name=f"第{i + 1}阶段",
                tags=low[:6],
                weeks=per_stage,
                goal=f"把 {('、'.join(low[:3]))} 的掌握度提到 60 分以上",
            )
        )
    return RoadmapOut(
        stages=stages,
        rationale=(
            f"规则引擎：按标签先修关系拓扑分层得到 {len(ordered)} 层，"
            f"总预算 {weeks_budget} 周，每阶段优先安排掌握度低于 60 的标签。"
        ),
    )
