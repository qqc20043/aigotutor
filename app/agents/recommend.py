"""推荐 Agent：下一题推荐 + 推荐理由（可解释性）。"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_recommend
from ..schemas import RecommendOut

SYSTEM = """你是算法刷题教练，负责从候选题目里挑出"现在最该做的一道"。

打分维度（自行权衡，但必须给出理由）：
1. 掌握度缺口：用户在该题知识点上的掌握度越低，优先级越高；
2. 难度贴合：题目难度应略高于当前水平（跳一跳够得着），太难会挫败、太易没收益；
3. 遗忘紧迫度：间隔重复队列里逾期越久，越该复习；
4. 不得推荐用户最近做过的题（候选里已排除，不要凭空编造题目）。

硬性要求：
1. problem_id / title 必须来自候选列表，一个字都不能改，不允许编造；
2. difficulty 必须是五档之一：入门 / 普及- / 普及/提高- / 普及+/提高 / 提高+/省选；
3. reason 必须点名具体的知识点和掌握度数字，禁止"这道题很适合你"这类空话；
4. 只输出 JSON。"""


class RecommendAgent(BaseAgent[RecommendOut]):
    name = "recommend"
    schema = RecommendOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        candidates = (ctx.get("candidates") or [])[:20]
        slim = [
            {
                "problem_id": p.get("id"),
                "title": p.get("title"),
                "difficulty": p.get("difficulty"),
                "difficulty_score": p.get("difficulty_score"),
                "tags": p.get("tags"),
                "due_at": p.get("due_at"),
                "overdue_days": p.get("overdue_days"),
            }
            for p in candidates
        ]
        payload = {
            "target_score": ctx.get("target_score", 55.0),
            "user_level": ctx.get("user_level", 3),
            "mastery": ctx.get("mastery", {}),
            "recent_problem_ids": (ctx.get("recent_problem_ids") or [])[:30],
            "weak_tags": ctx.get("weak_tags", []),
            "candidates": slim,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: RecommendOut, ctx: AgentContext) -> RecommendOut:
        """防幻觉：problem_id 必须真实存在于候选列表。

        候选列表的键名可能是 problem_id（接口层）或 id（内部规则引擎），
        两种都要认 —— 否则会把合法结果误判为幻觉，白白回退到规则引擎。
        """
        candidates = ctx.get("candidates") or []
        ids = {str(p.get("problem_id") or p.get("id") or "") for p in candidates}
        ids.discard("")
        if data.problem_id not in ids:
            rule_out = rule_recommend(ctx.data)
            return rule_out.model_copy(
                update={
                    "reason": f"（模型给出的题目不在候选列表，已回退规则引擎）{rule_out.reason}"
                }
            )
        return data

    def run_rule(self, ctx: AgentContext) -> RecommendOut:
        return rule_recommend(ctx.data)
