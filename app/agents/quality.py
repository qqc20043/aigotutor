"""代码质量评分 Agent（两阶段）。

对应简历主张：
  "两阶段评分：确定性问题采用基于规则的评分；开放性问题采用 LLM 语义修正评分。"

本 Agent 是该主张在"代码质量"场景的落点（难度标注已在 annotate_problem 里
用同一套机制）。两阶段的分工：

  第一阶段（规则，确定性问题）：
      嵌套深度、代码行数、标识符长度分布、注释比例、边界判断密度 ——
      全部是可量化、可复现的形式指标，不需要也不应该交给模型判断。

  第二阶段（LLM，开放性问题）：
      这段代码"读起来"如何、边界处理是否真的周全、有没有更贴合语言习惯的
      写法、命名是否表达了意图 —— 这些无法用统计量表达，需要语义理解。

约束机制（与难度标注完全一致）：
      规则阶段产出基线分 B 与窗口 [B-12, B+12]，模型只能在窗口内调整。
      模型若给出窗口外的分数，无条件夹回（clamp_quality）。

为什么必须夹回：语义修正的价值在于"在客观基线上做小幅合理调整"，
而不是推翻客观事实。若不夹紧，模型可以把 8 层嵌套、单字母变量的代码
评成"优秀"，评分就失去了区分度。
"""

from __future__ import annotations

import json

from ..core.agent import AgentContext, BaseAgent
from ..core.rules import clamp_quality, quality_window, rule_quality
from ..schemas import QUALITY_DIMENSIONS, QUALITY_RADIUS, QualityOut

SYSTEM = """你是代码质量评审，负责评估一份**已通过测试**的算法代码的工程质量。

注意分工：正确性已由判题机确认（全部用例通过），你**不需要**判断对错，
只评估"写得好不好"。

四个维度（必须逐一给出分数与评语）：
1. 正确性 —— 不是判对错，而是评估"实现的稳健程度"：是否依赖了未定义行为、
   是否有隐含假设（如输入必然非空）、数值范围是否可能溢出；
2. 可读性 —— 命名是否表达意图、控制流是否清晰、嵌套是否过深；
3. 效率 —— 时间/空间复杂度是否与该题数据范围匹配，是否有明显的常数优化空间；
4. 边界处理 —— 是否显式处理了空输入、单元素、极值等边界。

评语要求（这条最重要）：
  * 必须**点名代码里的具体位置或具体写法**，禁止"代码质量不错"这类空话；
  * 指出问题时要说清"改成什么"，而不是只说"可以优化"。

分数约束：
  * 系统会在用户消息里给出 rule_baseline（规则阶段算出的客观基线）与
    allowed_range（允许的分数区间）；
  * 你的 score **必须落在 allowed_range 内** —— 这是硬约束，系统会强制夹回；
  * 你可以在区间内基于语义理解调整（比如代码形式指标一般但命名极其清晰，
    可以往上调到区间上界）；
  * grade 必须与最终 score 一致（>=85 优秀 / >=70 良好 / >=55 合格 / 其余待改进）。

只输出 JSON。"""


class QualityAgent(BaseAgent[QualityOut]):
    name = "quality"
    schema = QualityOut
    system_prompt = SYSTEM
    inject_memory = True  # 决策类：知道用户历史水平才能给出贴合的改进建议

    def build_user(self, ctx: AgentContext) -> str:
        code = str(ctx.get("code", ""))
        baseline, metrics = quality_window(code)
        lo = max(0.0, baseline - QUALITY_RADIUS)
        hi = min(100.0, baseline + QUALITY_RADIUS)

        payload = {
            "title": ctx.get("title", ""),
            "statement": (ctx.get("statement", "") or "")[:1200],
            "language": ctx.get("language", "python"),
            "tags": ctx.get("tags", []),
            "code": code[:6000],
            # 第一阶段的客观结论：模型据此在窗口内做语义修正
            "rule_baseline": baseline,
            "allowed_range": [round(lo, 2), round(hi, 2)],
            "rule_metrics": {k: v for k, v in metrics.items() if not k.startswith("_")},
            "required_dimensions": QUALITY_DIMENSIONS,
            "verdict": ctx.get("verdict", "AC"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def postprocess(self, data: QualityOut, ctx: AgentContext) -> QualityOut:
        """第二阶段硬约束：分数必须落在规则窗口内，等级随分数重算。

        这是"两阶段评分"成立的关键一步 —— 没有它，模型可以无视客观指标。
        """
        code = str(ctx.get("code", ""))
        baseline, _ = quality_window(code)
        lo = max(0.0, baseline - QUALITY_RADIUS)
        hi = min(100.0, baseline + QUALITY_RADIUS)
        return clamp_quality(data, (lo, hi))

    def run_rule(self, ctx: AgentContext) -> QualityOut:
        return rule_quality(ctx.data)

    def confidence_of(self, data: QualityOut) -> float:
        return float(data.confidence)
