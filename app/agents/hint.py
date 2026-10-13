"""解题提示 Agent：渐进式提示，强度由掌握度决定。

对应简历主张：
  "知识状态建模：构建基于知识点的掌握度画像，动态调整推荐难度与提示强度"

本 Agent 是"提示强度"的落点：
  * 强度档位（direction / key_observation / pseudocode）由**规则层**按该题
    知识点的掌握度算定（rules.hint_strength_for），模型无权升降档；
  * 模型只负责"在这个强度下写出好提示"——这既保证了强度可复现可测试，
    又保留了 LLM 在具体题目上的表达优势。

为什么不让模型自己决定强度：掌握度是数据库里的客观事实，让模型"看着办"
会导致同一个用户同一道题在不同时间拿到不同强度的提示，既不可复现也无法
评测。强度是策略，内容是表达 —— 策略归规则，表达归模型。

提示的边界：不输出可提交的完整代码。提示的目的是让用户自己写出来，
一旦给出完整解法，这道题的训练价值就归零了。
"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import HINT_COUNT_BY_STRENGTH, hint_strength_for, rule_hint
from ..schemas import HintOut

SYSTEM = """你是算法刷题教练，负责给学生「渐进式提示」——帮他跨过卡点，但不替他解题。

【最重要的一条】你**绝对不能**给出可直接提交的完整代码或完整算法步骤。
学生要自己写出来，你的价值是让他"想到"，不是让他"抄到"。

提示强度分三档（已由系统按该生的掌握度算定，你必须遵守，不得自行升降档）：
  * direction（弱）：只点方向 —— 该往哪类算法/性质上想，不点破具体做法；
  * key_observation（中）：点出关键观察 —— 这题成立的"那个性质"是什么，
    以及需要检查的边界，但不给步骤；
  * pseudocode（强）：给出 3 步以内的伪代码级骨架（用自然语言描述步骤，
    **不要写真实代码**），仍由学生自己翻译成代码。

硬性要求：
1. strength 必须等于用户消息里给的 strength，一个字都不能改；
2. hints 的条数必须等于给定条数，且**从弱到强递进**（后一条比前一条更具体）；
3. key_insight 一句话点出突破口，禁止直接把完整解法写出来；
4. next_step 必须是可立刻执行的小动作（"先手算样例""先确认数据范围"这类）；
5. strength_reason 必须引用用户消息里给的掌握度数字；
6. 只输出 JSON。"""


class HintAgent(BaseAgent[HintOut]):
    name = "hint"
    inject_memory = True  # 决策类 Agent：注入学习记忆清单
    schema = HintOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        # 强度在这里算定并写进 prompt：模型拿到的是"结论"，不是"决策权"
        strength, reason = hint_strength_for(
            float(ctx.get("mastery_score", 50.0)),
            int(ctx.get("attempts", 0)),
        )
        count = HINT_COUNT_BY_STRENGTH[strength]
        payload = {
            "title": ctx.get("title", ""),
            "statement": (ctx.get("statement", "") or "")[:2500],
            "tags": ctx.get("tags", []),
            "difficulty": ctx.get("difficulty", ""),
            "language": ctx.get("language", "python"),
            # 已确定的策略：模型照此产出内容
            "strength": strength,
            "hint_count": count,
            "strength_reason": reason,
            # 学生当前进度（若有）：已尝试的代码能帮模型定位卡点
            "current_code": (ctx.get("current_code", "") or "")[:3000],
            "verdict": ctx.get("verdict", ""),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: HintOut, ctx: AgentContext) -> HintOut:
        """硬约束归一：强度由掌握度决定，模型无权改写。

        与 JudgeAgent 的 verdict 归一同一思路 —— 事实性的字段必须由
        系统决定，模型只能填充表达性字段。
        """
        expected_strength, _reason = hint_strength_for(
            float(ctx.get("mastery_score", 50.0)),
            int(ctx.get("attempts", 0)),
        )
        expected_count = HINT_COUNT_BY_STRENGTH[expected_strength]

        updates: dict[str, object] = {}
        if data.strength != expected_strength:
            updates["strength"] = expected_strength
        # 条数也必须对齐：模型多给或少给提示都会破坏"强度"的语义
        if len(data.hints) != expected_count:
            hints = list(data.hints)[:expected_count]
            while len(hints) < expected_count:
                hints.append("（提示条数不足，请按 key_insight 自行展开）")
            updates["hints"] = hints
        return data.model_copy(update=updates) if updates else data

    def run_rule(self, ctx: AgentContext) -> HintOut:
        return rule_hint(ctx.data)

    def confidence_of(self, data: HintOut) -> float:
        return float(data.confidence)
