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
    JudgeOut,
    MasteryOut,
    RecommendOut,
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


# ---------------------------------------------------------- 掌握度规则引擎
def rule_mastery(ctx_data: dict[str, Any]) -> MasteryOut:
    """加权正确率 + 样本量置信度，输出 0-100 与五级。"""
    tag_stats: dict[str, dict[str, float]] = ctx_data.get("tag_stats") or {}
    if not tag_stats:
        return MasteryOut(
            score=50.0,
            level="L3",
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
    weak = [t for t, _ in ranked[:3]]
    strong = [t for t, _ in ranked[-3:] if _ > 0.6][::-1]

    level = "L1"
    for name, threshold in (("L2", 40), ("L3", 55), ("L4", 70), ("L5", 85)):
        if score >= threshold:
            level = name

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
