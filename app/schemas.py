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
    """推荐 Agent：下一题 + 为什么是它。"""

    problem_id: str
    title: str = ""
    difficulty: Difficulty
    tags: list[str] = Field(min_length=1)
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


SCHEMA_BY_AGENT: dict[str, type[BaseModel]] = {
    "difficulty": DifficultyOut,
    "recommend": RecommendOut,
    "solution": SolutionOut,
    "judge": JudgeOut,
    "mastery": MasteryOut,
    "roadmap": RoadmapOut,
}
