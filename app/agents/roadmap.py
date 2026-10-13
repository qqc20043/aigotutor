"""路线图 Agent：按标签先修关系分层，输出分阶段学习路线。"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_roadmap
from ..schemas import RoadmapOut

SYSTEM = """你是算法学习路线规划师。根据用户的掌握度画像与知识点先修关系，规划分阶段路线。

硬性要求：
1. 阶段按先修顺序排列：不会「排序」就不该先排「线段树」；
2. 每阶段的 tags 必须是给定的知识点集合里真实存在的，不得编造；
3. 阶段数量 2-5 个，weeks 之和应接近给定的总预算；
4. goal 必须可验证（含具体数字或可判定的结果），禁止"打好基础"这类空话；
5. rationale 说明为什么这样排序，要引用掌握度数字；
6. 只输出 JSON。"""


class RoadmapAgent(BaseAgent[RoadmapOut]):
    name = "roadmap"
    schema = RoadmapOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        payload = {
            "weeks_budget": ctx.get("weeks", 6),
            "target_level": ctx.get("target_level", "L4"),
            "mastery": ctx.get("mastery", {}),
            "tags": (ctx.get("tags") or [])[:60],
            "weak_tags": ctx.get("weak_tags", []),
            "daily_hours": ctx.get("daily_hours", 2),
            "rule_baseline": ctx.get("rule_baseline", {}),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: RoadmapOut, ctx: AgentContext) -> RoadmapOut:
        """过滤幻觉标签：只保留题库里真实存在的知识点。"""
        known = {str(t.get("name")) for t in (ctx.get("tags") or [])}
        if not known:
            return data
        stages = []
        for st in data.stages:
            tags = [t for t in st.tags if t in known]
            if not tags:
                continue
            stages.append(st.model_copy(update={"tags": tags}))
        if not stages:
            return rule_roadmap(ctx.data)
        return data.model_copy(update={"stages": stages})

    def run_rule(self, ctx: AgentContext) -> RoadmapOut:
        return rule_roadmap(ctx.data)
