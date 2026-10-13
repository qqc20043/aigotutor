"""复习队列优先 Agent：决定"今天先复习哪几道"，并解释为什么。

对应简历主张：
  "多 Agent 协作：按职责划分路线规划、题目推荐、解题提示、掌握度评估、
   复习队列优先等 5 个 Agent"

与 FSRS 的分工（关键设计）：
  * **FSRS 决定"什么时候该复习"**（何时到期、间隔多长）——这是记忆模型的职责；
  * **本 Agent 决定"到期了先做哪个"**（排序与解释）——这是教练策略的职责。

为什么不把优先级直接写进 FSRS：间隔重复是**记忆科学**问题（保留率、稳定性），
而"先做哪个"是**学习策略**问题（薄弱点、错误模式、时间预算）。两者混在一起
会导致 FSRS 参数被业务策略污染，无法独立评估与调参。

排序信号（权重写在规则层，可复现、可测试）：
  1. 逾期紧迫度：逾期越久保留率越低，优先；
  2. 掌握度缺口：该知识点掌握度越低，优先；
  3. 错误模式：反复出错的题（lapses 多）优先；
  4. 时间预算：用户给的时间预算内能做完的优先（避免给一个"做不完"的队列）。
"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_review_priority
from ..schemas import ReviewPriorityOut

SYSTEM = """你是算法刷题教练，负责决定学生「今天先复习哪几道题」，并给出可解释的理由。

你收到的候选已经按 FSRS 到期情况筛过（都是该复习的）。你的任务是**排序**：
在有限的时间预算内，挑出收益最高的若干道，并说清楚"为什么是它"。

排序时请权衡这些信号：
1. 逾期紧迫度：逾期越久，保留率掉得越多，越该优先（避免"白学"）；
2. 掌握度缺口：该知识点掌握度越低，复习收益越大；
3. 反复出错的题：错过的题再错概率高，优先巩固；
4. 难度梯度：同一批里先易后难，或先难后易都可以，但**要在理由里说明**你的编排意图。

硬性要求：
1. problem_id / title 必须来自候选列表，一个字都不能改，不允许编造；
2. ordered 里每道题都要给出 reason，且必须点名具体的逾期天数或掌握度数字，
   禁止"这题比较重要"这类空话；
3. 如果给了 time_budget_minutes，挑出的题数要与之匹配（每题按 15 分钟估算），
   宁可少挑也不要给一个做不完的队列；
4. 只输出 JSON。"""


class ReviewAgent(BaseAgent[ReviewPriorityOut]):
    name = "review"
    inject_memory = True  # 决策类 Agent：注入学习记忆清单
    schema = ReviewPriorityOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        candidates = (ctx.get("candidates") or [])[:30]
        slim = [
            {
                "problem_id": p.get("problem_id") or p.get("id"),
                "title": p.get("title"),
                "difficulty": p.get("difficulty"),
                "tags": p.get("tags"),
                "overdue_days": p.get("overdue_days"),
                "retrievability": p.get("retrievability"),
                "lapses": p.get("lapses", 0),
                "mastery_scores": p.get("mastery_scores", {}),
            }
            for p in candidates
        ]
        payload = {
            "candidates": slim,
            "time_budget_minutes": ctx.get("time_budget_minutes", 0),
            "daily_goal": ctx.get("daily_goal", 3),
            "weak_tags": ctx.get("weak_tags", []),
            "rule_baseline_order": ctx.get("rule_baseline_order", []),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: ReviewPriorityOut, ctx: AgentContext) -> ReviewPriorityOut:
        """防幻觉 + 预算硬约束。

        与 RecommendAgent 的 postprocess 同一思路：模型只能在候选里挑，
        挑出候选之外的题一律回退规则结果（宁可降级也不能凭空造题）。
        """
        candidates = ctx.get("candidates") or []
        valid_ids = {str(p.get("problem_id") or p.get("id") or "") for p in candidates}
        valid_ids.discard("")

        ordered = [o for o in data.ordered if o.problem_id in valid_ids]
        if not ordered:
            return rule_review_priority(ctx.data)

        # 时间预算硬约束：每题按 15 分钟估，超出预算的截掉
        budget = int(ctx.get("time_budget_minutes", 0) or 0)
        if budget > 0:
            max_items = max(1, budget // 15)
            ordered = ordered[:max_items]

        # 按模型给出的顺序重编号，保证 order 字段连续（前端直接按此渲染）
        renumbered = [item.model_copy(update={"order": i + 1}) for i, item in enumerate(ordered)]
        return data.model_copy(update={"ordered": renumbered})

    def run_rule(self, ctx: AgentContext) -> ReviewPriorityOut:
        return rule_review_priority(ctx.data)

    def confidence_of(self, data: ReviewPriorityOut) -> float:
        return float(data.confidence)
