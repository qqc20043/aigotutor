"""Agent 结构化输出契约（Pydantic v2）。

设计要点：
  * 难度用 Literal 枚举锁死，LLM 只能在这 5 档里选，杜绝"较难/中等偏上"这类漂移；
  * 所有 schema 的 title 与 MockProvider.FIXTURES 的键一一对应，mock 模式可离线跑通；
  * 规则引擎产出的对象必须能通过同一份 schema 校验 —— 这是"降级不中断主链"的前提。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Difficulty = Literal["入门", "普及-", "普及/提高-", "普及+/提高", "提高+/省选"]

DIFFICULTY_ORDER: list[str] = ["入门", "普及-", "普及/提高-", "普及+/提高", "提高+/省选"]
DIFFICULTY_MID: dict[str, float] = {
    "入门": 15.0,
    "普及-": 40.0,
    "普及/提高-": 58.0,
    "普及+/提高": 75.0,
    "提高+/省选": 90.0,
}

# 连续分 -> 档位的唯一权威边界（相邻档位中点的均值）。
# 规则引擎、窗口夹紧、Mock 输出全部走这一个函数，杜绝映射不一致。
DIFFICULTY_BOUNDS: list[float] = [0.0, 27.5, 49.0, 66.5, 82.5, 100.0]
# 每个档位允许的校准半径：±1 档以内，防止 LLM 把入门题标成省选
DIFFICULTY_RADIUS: float = 20.0


def level_index_of(score: float) -> int:
    """连续分 -> 档位下标（0..4）。全系统唯一的映射口径。"""
    s = max(0.0, min(100.0, float(score)))
    for i in range(len(DIFFICULTY_ORDER) - 1):
        if s < DIFFICULTY_BOUNDS[i + 1]:
            return i
    return len(DIFFICULTY_ORDER) - 1


def difficulty_of(score: float) -> str:
    return DIFFICULTY_ORDER[level_index_of(score)]


# 兼容旧名：档位名 -> (下一档下界, 上一档上界) 的语义已由 DIFFICULTY_BOUNDS 表达
DIFFICULTY_BOUND: dict[str, tuple[str, str]] = {
    DIFFICULTY_ORDER[i]: (
        DIFFICULTY_ORDER[max(0, i - 1)],
        DIFFICULTY_ORDER[min(len(DIFFICULTY_ORDER) - 1, i + 1)],
    )
    for i in range(len(DIFFICULTY_ORDER))
}

LEVEL = Literal["L1", "L2", "L3", "L4", "L5"]
VERDICT = Literal["AC", "WA", "TLE", "MLE", "RE", "CE"]
ROOT_CAUSE = Literal["边界", "复杂度", "理解偏差", "算法选型", "实现细节", "未知"]


class DifficultyOut(BaseModel):
    """两阶段难度标注的第二阶段：模型在规则区间内做校准。"""

    difficulty: Difficulty
    difficulty_score: float = Field(ge=0, le=100, description="0-100 连续难度分")
    tags: list[str] = Field(min_length=1, max_length=4, description="1-4 个知识点标签")
    reason: str = Field(min_length=1, max_length=400)

    @field_validator("tags")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for t in v:
            t = t.strip()
            if t and t not in seen:
                seen.append(t)
        return seen or ["未分类"]


class RecommendOut(BaseModel):
    """推荐 Agent：下一题 + 为什么是它。

    tags 允许为空：真模型实测（deepseek）会稳定漏掉描述性字段导致校验
    失败重试，而 RecommendAgent.postprocess 会按 problem_id 从候选回填 ——
    schema 宽容 + 后处理补齐，比"指望模型每次完整复述"可靠。
    """

    problem_id: str
    title: str = ""
    difficulty: Difficulty
    tags: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=600)
    expected_gain: float = Field(ge=0, le=20, default=0.0, description="预期掌握度提升")


class SolutionOut(BaseModel):
    """题解 Agent：结构化题解 + 可运行代码 + 复杂度 + 易错点。"""

    summary: str = Field(min_length=1, max_length=300)
    approach: list[str] = Field(min_length=1, max_length=8)
    code: str = Field(min_length=1)
    language: str = "python"
    complexity: dict[str, str] = Field(default_factory=lambda: {"time": "O(n)", "space": "O(1)"})
    pitfalls: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("complexity")
    @classmethod
    def _norm(cls, v: dict[str, str]) -> dict[str, str]:
        return {"time": v.get("time", "O(n)"), "space": v.get("space", "O(1)")}


class JudgeOut(BaseModel):
    """评测 Agent：判错归因 + 修复建议（错题本归因字段直接来自这里）。"""

    verdict: VERDICT
    root_cause: ROOT_CAUSE
    explanation: str = Field(min_length=1, max_length=800)
    fix_suggestion: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1, default=0.8)


class MasteryOut(BaseModel):
    """学习状态 Agent：0-100 分 + 五级 + 置信度 + 强弱项。"""

    score: float = Field(ge=0, le=100)
    level: LEVEL
    confidence: float = Field(ge=0, le=1)
    weak_tags: list[str] = Field(default_factory=list, max_length=5)
    strong_tags: list[str] = Field(default_factory=list, max_length=5)
    suggestion: str = Field(min_length=1, max_length=500)


class RoadmapStage(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    tags: list[str] = Field(min_length=1, max_length=6)
    weeks: int = Field(ge=1, le=26)
    goal: str = Field(min_length=1, max_length=200)


class RoadmapOut(BaseModel):
    """路线图 Agent：分阶段 + 依据。"""

    stages: list[RoadmapStage] = Field(min_length=1, max_length=6)
    rationale: str = Field(min_length=1, max_length=600)


# 提示强度三档：由掌握度决定（掌握度越低给越强的提示）。
# 用 Literal 锁死，模型不能自造"中等偏强"这类档位。
HintStrength = Literal["direction", "key_observation", "pseudocode"]


class HintOut(BaseModel):
    """解题提示 Agent：渐进式提示 + 强度自解释。

    设计要点（对应简历"知识状态建模：动态调整推荐难度与提示强度"）：
      * 提示分三档递进：方向 -> 关键观察 -> 伪代码，每档都可单独使用；
      * 强度由**该题知识点掌握度**决定，而非模型自由发挥；
      * 不直接给可提交的完整代码 —— 提示的目的是让用户自己写出来。
    """

    strength: HintStrength = Field(description="提示强度：direction 最弱、pseudocode 最强")
    hints: list[str] = Field(
        min_length=1,
        # 上限故意放宽到 6：真模型偶尔会多给一条（实测 pseudocode 档输出
        # 3 步骨架 + 1 条手算验证），postprocess 会裁剪到强度对应条数 ——
        # 校验层直接拒绝只会白白触发整单重试。
        max_length=6,
        description="按强度从弱到强的提示，条数与 strength 对应",
    )
    key_insight: str = Field(
        min_length=1, max_length=300, description="一句话点出这题的突破口（不泄露完整解法）"
    )
    next_step: str = Field(
        min_length=1, max_length=200, description="建议用户接下来先做什么（可执行的小步）"
    )
    strength_reason: str = Field(
        min_length=1, max_length=300, description="为什么给这个强度（引用掌握度数字）"
    )
    confidence: float = Field(ge=0, le=1, default=0.8)


class ReviewPriorityItem(BaseModel):
    """复习队列里的一项：题目 + 排在第几位 + 为什么。

    order 允许缺省（默认 1）：真模型会漏掉"可从列表顺序推断"的字段
    （deepseek 实测三次全漏），而 ReviewAgent.postprocess 本来就会按
    实际顺序重编号 —— 校验层不必为这个字段整单作废重试。
    """

    problem_id: str = Field(min_length=1)
    title: str = ""
    order: int = Field(default=1, ge=1, description="1 = 今天最先复习")
    reason: str = Field(min_length=1, max_length=400, description="为什么排这个位置")
    urgency: str = Field(default="", max_length=80, description="紧迫度标签，如「逾期 6 天」")


class ReviewPriorityOut(BaseModel):
    """复习队列优先 Agent：排序 + 可解释的编排意图。

    与 FSRS 的分工：FSRS 决定"何时到期"，本 Agent 决定"到期后先做哪个"。
    间隔重复是记忆科学问题，排序是学习策略问题，两者不混在一起。

    strategy 允许缺省：真模型会自创同义字段名（ordering_intent / notes，
    实测两种都出现过），schema 层拒绝只会整单作废重试；ReviewAgent
    的 postprocess 会用规则口径回填。
    """

    ordered: list[ReviewPriorityItem] = Field(
        min_length=1, max_length=20, description="按优先级排序的复习清单"
    )
    strategy: str = Field(
        default="", max_length=400, description="整体编排意图（为什么是这个顺序）"
    )
    confidence: float = Field(ge=0, le=1, default=0.8)


# 代码质量的四个评分维度（固定口径，避免每次评分维度漂移导致不可比）
QUALITY_DIMENSIONS: list[str] = ["正确性", "可读性", "效率", "边界处理"]

# 两阶段评分的窗口半径：规则分 ± 该值 之内允许 LLM 修正。
# 取值理由：太小则语义修正无意义（模型只能照抄规则分），太大则规则阶段的
# 客观约束失效。±12 分意味着"档位不会跨两级"，与难度标注的 ±1 档同思路。
QUALITY_RADIUS: float = 12.0

# 掌握度修正窗口：与 QUALITY_RADIUS 同构。规则基线由 tag_stats 客观算出，
# 模型只做窗口内语义修正（真模型实测：无约束时会把 85 分的基线评到 68 分，
# 偏离 17 分）。
MASTERY_RADIUS: float = 15.0


class QualityDimension(BaseModel):
    """单个评分维度：分数 + 具体评语（必须点名代码里的具体位置）。"""

    name: str = Field(min_length=1, max_length=20)
    score: float = Field(ge=0, le=100)
    comment: str = Field(min_length=1, max_length=300)


class QualityOut(BaseModel):
    """代码质量评分（两阶段）。

    对应简历主张："两阶段评分：确定性问题采用基于规则的评分；
    开放性问题采用 LLM 语义修正评分。"

    落点说明：
      * **规则阶段**算的是客观可量化指标（行数、嵌套深度、圈复杂度估算、
        注释率、变量命名长度）—— 这些是"确定性问题"，不需要模型判断；
      * **LLM 阶段**做的是语义修正（这段代码的可读性到底如何、边界处理是否
        周全、有没有更贴合语言习惯的写法）—— 这些是"开放性问题"。

    与判题的分工：判题管"对不对"（AC/WA/TLE），本评分管"好不好"。
    只有 AC 的代码才值得评质量 —— 错代码谈可读性没有意义。
    """

    score: float = Field(ge=0, le=100, description="总分 0-100")
    grade: Literal["优秀", "良好", "合格", "待改进"] = Field(description="等级")
    dimensions: list[QualityDimension] = Field(
        min_length=1, max_length=6, description="各维度得分与评语"
    )
    summary: str = Field(min_length=1, max_length=400, description="总体评价")
    top_improvement: str = Field(
        min_length=1, max_length=300, description="最值得改的一处（可落地）"
    )
    confidence: float = Field(ge=0, le=1, default=0.8)


SCHEMA_BY_AGENT: dict[str, type[BaseModel]] = {
    "difficulty": DifficultyOut,
    "recommend": RecommendOut,
    "solution": SolutionOut,
    "judge": JudgeOut,
    "mastery": MasteryOut,
    "roadmap": RoadmapOut,
    "hint": HintOut,
    "review": ReviewPriorityOut,
    "quality": QualityOut,
}
