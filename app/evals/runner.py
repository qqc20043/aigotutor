"""评估执行器：跑 golden set -> 断言 -> 聚合报告。

双层用法：
  * **CI / 回归**（provider=mock）：全场景必须全绿。mock 是"无信息的
    模型"，它都遵守不了的约束（防幻觉、预算、强度档位），真实模型更没有
    借口 —— 所以 mock 层断言可以进 pytest 当回归线；
  * **真模型抽检**（provider=openai/...）：同一套场景换 provider 跑，
    产出 Markdown 报告人工审阅。模型可能失败的地方（表达质量、排序
    微调）在报告里一目了然，改动提示词前后的两份报告可直接对比。

结果缓存说明：结果缓存是进程内的（或 Redis），而 eval 每次都是全新
进程跑完整链路 —— 不存在"上次的缓存污染本次评估"的问题，无需开关。

执行器不依赖数据库 —— pipeline 层是纯编排，provider/model/key 全部
由 config 注入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.pipeline import (
    analyze_mastery,
    build_roadmap,
    generate_hint,
    prioritize_review,
    recommend_next,
)
from .checks import Check, check_hint, check_mastery, check_recommend, check_review, check_roadmap
from .golden import SCENARIOS, Scenario


@dataclass
class ScenarioResult:
    """单个场景的评估结果：断言明细 + 元信息（延迟/token/降级）。"""

    scenario_id: str
    agent: str
    title: str
    checks: list[Check] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    degraded: bool = False
    latency_ms: int = 0
    tokens: int = 0
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


@dataclass
class EvalReport:
    """一次评估的完整报告：可渲染 Markdown / JSON，用于前后对比与存档。"""

    provider: str
    model: str
    started_at: str
    elapsed_ms: int
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def degraded_count(self) -> int:
        return sum(1 for r in self.results if r.degraded)

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.results)

    def summary_line(self) -> str:
        return (
            f"{self.passed_count}/{len(self.results)} 场景通过，"
            f"降级 {self.degraded_count} 次，token {self.total_tokens}，"
            f"耗时 {self.elapsed_ms / 1000:.1f}s"
        )

    def to_markdown(self) -> str:
        lines = [
            "# Agent 行为评估报告",
            "",
            f"- **时间**: {self.started_at}",
            f"- **Provider / 模型**: {self.provider} / {self.model}",
            f"- **结论**: {self.summary_line()}",
            "",
            "| 场景 | Agent | 结果 | 失败断言 | 延迟 | token |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in self.results:
            failed = "；".join(f"{c.name}（{c.detail}）" for c in r.failed_checks) or "—"
            mark = "✅ 通过" if r.passed else f"❌ {r.error or '断言失败'}"
            lines.append(
                f"| {r.scenario_id} | {r.agent} | {mark} | {failed} "
                f"| {r.latency_ms}ms | {r.tokens} |"
            )
        lines.append("")
        lines.append("## 场景明细")
        for r in self.results:
            lines.append("")
            lines.append(f"### {r.scenario_id} —— {r.title}")
            lines.append("")
            if r.error:
                lines.append(f"> 执行失败: {r.error}")
            for c in r.checks:
                mark = "✓" if c.passed else "✗"
                suffix = f" —— {c.detail}" if c.detail else ""
                lines.append(f"- {mark} {c.name}{suffix}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "summary": {
                "total": len(self.results),
                "passed": self.passed_count,
                "degraded": self.degraded_count,
                "tokens": self.total_tokens,
            },
            "results": [
                {
                    "scenario_id": r.scenario_id,
                    "agent": r.agent,
                    "title": r.title,
                    "passed": r.passed,
                    "error": r.error,
                    "degraded": r.degraded,
                    "latency_ms": r.latency_ms,
                    "tokens": r.tokens,
                    "checks": [
                        {"name": c.name, "passed": c.passed, "detail": c.detail} for c in r.checks
                    ],
                }
                for r in self.results
            ],
        }


async def _execute(scenario: Scenario, config: dict[str, Any]) -> dict[str, Any]:
    """调用对应 pipeline 函数，返回完整 payload（含 data / 元信息 / rule_baseline）。"""
    agent = scenario.agent
    inputs = dict(scenario.inputs)
    if agent == "hint":
        return await generate_hint("eval-user", config, **inputs)
    if agent == "review":
        return await prioritize_review("eval-user", config, **inputs)
    if agent == "mastery":
        return await analyze_mastery("eval-user", config, **inputs)
    if agent == "roadmap":
        return await build_roadmap("eval-user", config, **inputs)
    if agent == "recommend":
        return await recommend_next("eval-user", config, **inputs)
    raise ValueError(f"未知 Agent 类型: {agent}")


def _checks_for(
    scenario: Scenario, data: dict[str, Any], baseline: dict[str, Any] | None
) -> list[Check]:
    agent = scenario.agent
    expect = scenario.expect
    if agent == "hint":
        return check_hint(data, expect)
    if agent == "review":
        return check_review(data, baseline or {}, expect)
    if agent == "mastery":
        return check_mastery(data, baseline or {}, expect)
    if agent == "roadmap":
        return check_roadmap(data, expect)
    if agent == "recommend":
        return check_recommend(data, expect)
    raise ValueError(f"未知 Agent 类型: {agent}")


async def run_eval(
    config: dict[str, Any],
    only: list[str] | None = None,
) -> EvalReport:
    """跑完整 golden set，返回聚合报告。

    config: {"provider": "mock|openai|...", "model": "...", "base_url": "...",
             "api_key": "..."} —— 与 pipeline 层的 config 语义一致。
    only:   只跑指定 Agent（如 ["hint", "review"]）。
    """
    started = time.perf_counter()
    scenarios = [s for s in SCENARIOS if not only or s.agent in only]
    report = EvalReport(
        provider=str(config.get("provider", "mock")),
        model=str(config.get("model", "-")),
        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        elapsed_ms=0,
    )
    # 跨场景断言（mastery 单调性）：记录每个 mastery 场景的最终分数
    scores_by_scenario: dict[str, float] = {}

    for scenario in scenarios:
        result = ScenarioResult(scenario_id=scenario.id, agent=scenario.agent, title=scenario.title)
        try:
            payload = await _execute(scenario, config)
            data = payload.get("data") or {}
            baseline = payload.get("rule_baseline")
            result.checks = _checks_for(scenario, data, baseline)

            if scenario.agent == "mastery":
                scores_by_scenario[scenario.id] = float(data.get("score", 0.0))
                target_id = scenario.expect.get("score_gt_scenario")
                if target_id:
                    result.checks.append(
                        Check(
                            f"单调性：高于场景 {target_id}",
                            target_id in scores_by_scenario
                            and scores_by_scenario[scenario.id] > scores_by_scenario[target_id],
                            f"{scores_by_scenario[scenario.id]:.1f} vs "
                            f"{scores_by_scenario.get(target_id, 0.0):.1f}",
                        )
                    )

            # 元信息：payload 里就是 AgentResult.to_dict() 的结构
            result.provider = str(payload.get("provider") or report.provider)
            result.model = str(payload.get("model") or report.model)
            result.degraded = bool(payload.get("degraded"))
            result.latency_ms = int(payload.get("latency_ms") or 0)
            result.tokens = int(payload.get("tokens") or 0)
        except Exception as e:  # 单场景失败不中断整场评估
            result.error = f"{type(e).__name__}: {e}"
        report.results.append(result)

    report.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return report
