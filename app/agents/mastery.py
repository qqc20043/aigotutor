"""学习状态 Agent：0-100 分 + 五级 + 置信度 + 强弱项。"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_mastery
from ..schemas import MasteryOut

SYSTEM = """你是算法学习状态分析师。根据用户近期的提交统计，给出 0-100 的学习状态评分。

评分口径（固定，不得自创）：
1. 正确率是主项，但要按样本量加权：3 次里 2 次对 ≠ 稳定掌握；
2. 难度加成：在更高难度题上保持正确率应当加分；
3. 近 30 天内的表现权重高于更早的数据；
4. level 由分数决定：<40 L1，40-54 L2，55-69 L3，70-84 L4，>=85 L5；
5. confidence 反映样本量是否足够，样本越少越低，0-1；
6. weak_tags / strong_tags 从统计里挑，必须真实出现过的标签，最多各 5 个；
7. suggestion 必须点名具体标签和下一步动作，禁止空话；
8. 只输出 JSON。"""


class MasteryAgent(BaseAgent[MasteryOut]):
    name = "mastery"
    schema = MasteryOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        payload = {
            "window_days": ctx.get("window_days", 30),
            "total_submissions": ctx.get("total_submissions", 0),
            "ac_count": ctx.get("ac_count", 0),
            "avg_difficulty_score": ctx.get("avg_difficulty_score", 0.0),
            "active_days": ctx.get("active_days", 0),
            "tag_stats": ctx.get("tag_stats", {}),
            "recent_verdicts": (ctx.get("recent_verdicts") or [])[:30],
            "rule_baseline": ctx.get("rule_baseline", {}),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: MasteryOut, ctx: AgentContext) -> MasteryOut:
        """level 必须与 score 口径自洽，模型自相矛盾时以分数为准重算。"""
        score = max(0.0, min(100.0, data.score))
        level = "L1"
        for name, threshold in (("L2", 40), ("L3", 55), ("L4", 70), ("L5", 85)):
            if score >= threshold:
                level = name
        # 标签必须真实存在于统计里
        known = set((ctx.get("tag_stats") or {}).keys())
        weak = [t for t in data.weak_tags if t in known][:5] or data.weak_tags[:5]
        strong = [t for t in data.strong_tags if t in known][:5] or data.strong_tags[:5]
        return data.model_copy(
            update={
                "score": round(score, 2),
                "level": level,
                "weak_tags": weak,
                "strong_tags": strong,
            }
        )

    def run_rule(self, ctx: AgentContext) -> MasteryOut:
        return rule_mastery(ctx.data)

    def confidence_of(self, data: MasteryOut) -> float:
        return float(data.confidence)
