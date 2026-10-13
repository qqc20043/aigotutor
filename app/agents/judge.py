"""评测 Agent：判错归因 + 修复建议（错题本 root_cause 直接落库）。"""

from __future__ import annotations

import json
from typing import Any

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_judge
from ..schemas import JudgeOut

SYSTEM = """你是算法竞赛的判题分析师。给定用户代码、判题结果与失败用例，定位真正的错误原因。

硬性要求：
1. verdict 必须与判题机给出的结论一致，不得改写；
2. root_cause 只能从这六类里选：边界 / 复杂度 / 理解偏差 / 算法选型 / 实现细节 / 未知；
3. explanation 必须引用具体的代码行或具体用例，禁止"可能有问题"这类模糊表述；
4. fix_suggestion 给出可直接落地的改法（改哪一行、改成什么）；
5. confidence 是你对本次归因的把握，0-1；
6. 只输出 JSON。"""


class JudgeAgent(BaseAgent[JudgeOut]):
    name = "judge"
    schema = JudgeOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        payload = {
            "problem_title": ctx.get("title", ""),
            "statement": (ctx.get("statement", "") or "")[:2000],
            "language": ctx.get("language", "python"),
            "verdict": ctx.get("verdict", "WA"),
            "passed_cases": ctx.get("passed_cases", 0),
            "total_cases": ctx.get("total_cases", 0),
            "failed_case": ctx.get("failed_case", ""),
            "judge_detail": ctx.get("judge_detail", {}),
            "code": (ctx.get("code", "") or "")[:6000],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: JudgeOut, ctx: AgentContext) -> JudgeOut:
        """硬约束归一：verdict 与 root_cause 由判题事实决定，模型无权改写。

        1) verdict 必须等于判题机的结论；
        2) 当 verdict 是 TLE/MLE/RE/CE 时，根因几乎唯一，模型若给出别的类别
           属于自相矛盾（例如 TLE 却说"理解偏差"），直接按事实修正。
        """
        real = str(ctx.get("verdict", data.verdict)).upper()
        updates: dict[str, Any] = {}
        if real in ("AC", "WA", "TLE", "MLE", "RE", "CE") and real != data.verdict:
            updates["verdict"] = real

        implied = {"TLE": "复杂度", "MLE": "实现细节", "RE": "边界", "CE": "实现细节"}
        target_verdict = updates.get("verdict", data.verdict)
        forced = implied.get(str(target_verdict))
        if forced and data.root_cause != forced:
            updates["root_cause"] = forced
        return data.model_copy(update=updates) if updates else data

    def run_rule(self, ctx: AgentContext) -> JudgeOut:
        return rule_judge(ctx.data)

    def confidence_of(self, data: JudgeOut) -> float:
        return float(data.confidence)
