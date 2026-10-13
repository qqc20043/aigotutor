"""难度标注 Agent —— 两阶段标注的第二阶段（模型在规则区间内校准）。"""

from __future__ import annotations

import json
from typing import Any

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import clamp_to_window, difficulty_window, rule_difficulty
from ..schemas import DIFFICULTY_ORDER, DifficultyOut

SYSTEM = """你是算法竞赛题库的难度标注专家。规则引擎已经根据题面长度、知识点数量、算法深度、
数据范围、构造性等特征，给出了一个"规则区间"。你的任务是在这个区间内做精细校准。

硬性要求：
1. difficulty 必须是这五档之一：入门 / 普及- / 普及/提高- / 普及+/提高 / 提高+/省选；
2. difficulty_score 必须落在给定的 [下限, 上限] 区间内，不得越界；
3. tags 给 1-4 个，使用算法竞赛通用知识点名称（如 排序、贪心、二分、前缀和、动态规划、
   图论、并查集、线段树、数论、字符串、模拟、搜索、构造）；
4. reason 必须说明你相对规则区间做了上调还是下调，以及依据（不要空话）；
5. 只输出 JSON，不要任何额外文字。"""


class DifficultyAgent(BaseAgent[DifficultyOut]):
    name = "difficulty"
    schema = DifficultyOut
    system_prompt = SYSTEM

    def build_user(self, ctx: AgentContext) -> str:
        rule_out: DifficultyOut = ctx.get("rule_out") or rule_difficulty(ctx.data)
        lo, hi = difficulty_window(rule_out)
        samples = ctx.get("samples") or []
        payload = {
            "title": ctx.get("title", ""),
            "statement": (ctx.get("statement", "") or "")[:3000],
            "constraints": (ctx.get("constraints", "") or "")[:500],
            "samples": samples[:3],
            "rule_difficulty": rule_out.difficulty,
            "rule_score": rule_out.difficulty_score,
            "allowed_score_range": [round(lo, 2), round(hi, 2)],
            "rule_tags": rule_out.tags,
            "features": {
                "statement_len": len(ctx.get("statement", "") or ""),
                "tag_count": len(ctx.get("tags") or []),
                "algo_depth": ctx.get("algo_depth", 1),
                "constraint_tight": ctx.get("constraint_tight", False),
                "is_constructive": ctx.get("is_constructive", False),
                "pass_rate": ctx.get("pass_rate"),
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: DifficultyOut, ctx: AgentContext) -> DifficultyOut:
        """把模型输出夹回规则窗口 —— 保证标注可复现、不漂移。"""
        rule_out: DifficultyOut = ctx.get("rule_out") or rule_difficulty(ctx.data)
        clamped = clamp_to_window(data, difficulty_window(rule_out))
        # 标签以规则引擎抽出的为准，模型只做补充
        merged = list(dict.fromkeys((ctx.get("tags") or []) + clamped.tags))[:4]
        return clamped.model_copy(update={"tags": merged or ["未分类"]})

    def run_rule(self, ctx: AgentContext) -> DifficultyOut:
        """第一阶段本身就是规则引擎，所以降级等价于直接用规则结果。"""
        return ctx.get("rule_out") or rule_difficulty(ctx.data)

    def confidence_of(self, data: DifficultyOut) -> float:
        return 0.85

    def describe_window(self, ctx: AgentContext) -> dict[str, Any]:
        rule_out = ctx.get("rule_out") or rule_difficulty(ctx.data)
        lo, hi = difficulty_window(rule_out)
        return {
            "rule_difficulty": rule_out.difficulty,
            "rule_score": rule_out.difficulty_score,
            "window": [lo, hi],
            "levels": DIFFICULTY_ORDER,
        }
