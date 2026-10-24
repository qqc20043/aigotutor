"""Agent 行为评估（golden set + 规则断言 + 报告）。

回答一个问题：**改了提示词/换了模型之后，Agent 的行为变好还是变坏了？**

设计原则（与 hint/quality 两阶段评分一脉相承）：
  * 策略性结论（提示强度、排序口径、掌握度档位）由规则层定义 ——
    评估断言的就是"pipeline 输出必须遵守规则层"，期望值直接从 rules.py
    现算，不在 golden set 里写死第二份口径（两份口径必然漂移）；
  * 表达性内容（提示文案、理由是否具体）只做硬下限检查（引用数字、
    不泄露完整代码），不做主观打分 —— 主观评估交给真模型抽样复核；
  * mock 下全绿是 CI 回归线：mock 是"无信息的模型"，它都做不到的
    约束，真实模型更没有借口。
"""

from .checks import (
    Check,
    kendall_tau,
    level_for_score,
)
from .golden import SCENARIOS, Scenario
from .runner import EvalReport, ScenarioResult, run_eval

__all__ = [
    "SCENARIOS",
    "Check",
    "EvalReport",
    "Scenario",
    "ScenarioResult",
    "kendall_tau",
    "level_for_score",
    "run_eval",
]
