"""题解 Agent：结构化题解 + 可运行代码 + 复杂度 + 易错点。"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import rule_solution
from ..schemas import SolutionOut

SYSTEM = """你是算法竞赛题解作者。给定题面，输出一份可直接运行、能通过样例的题解。

硬性要求：
1. code 必须是完整可运行的程序（含读入与输出），不能有 TODO、不能有伪代码、不能省略函数体；
2. 默认使用 Python 3，若题面指定其他语言则按指定语言；
3. complexity.time / complexity.space 必须给出具体量级，如 O(n log n)、O(n)；
4. pitfalls 列出 2-4 个真实易错点（边界、取模、溢出、下标、多测清空等），禁止写"注意细节"；
5. approach 是 3-6 步解题思路，每步一句话，按执行顺序排列；
6. summary 一句话概括核心思想；
7. 只输出 JSON。"""


class SolutionAgent(BaseAgent[SolutionOut]):
    name = "solution"
    schema = SolutionOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        payload = {
            "title": ctx.get("title", ""),
            "statement": (ctx.get("statement", "") or "")[:4000],
            "input_desc": ctx.get("input_desc", ""),
            "output_desc": ctx.get("output_desc", ""),
            "samples": (ctx.get("samples") or [])[:3],
            "constraints": ctx.get("constraints", ""),
            "tags": ctx.get("tags", []),
            "difficulty": ctx.get("difficulty", ""),
            "language": ctx.get("language", "python"),
            "time_limit_ms": ctx.get("time_limit_ms", 1000),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: SolutionOut, ctx: AgentContext) -> SolutionOut:
        """剥离 markdown 代码围栏：模型很爱包 ```python，直接用会编译失败。"""
        code = data.code.strip()
        if code.startswith("```"):
            parts = code.split("```")
            if len(parts) >= 2:
                code = parts[1]
                if code.lower().startswith("python"):
                    code = code[6:]
                elif code.lower().startswith("cpp"):
                    code = code[3:]
                code = code.strip()
        return data.model_copy(
            update={"code": code, "language": ctx.get("language", data.language)}
        )

    def run_rule(self, ctx: AgentContext) -> SolutionOut | None:
        return rule_solution(ctx.data)
