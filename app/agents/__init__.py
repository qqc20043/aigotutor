"""5 个 Agent + 注册表。

每个 Agent 只做三件事：
  1. 声明 schema（结构化输出契约）
  2. build_user() 拼 prompt
  3. run_rule() 给出确定性兜底

其余（重试/校验/降级/缓存/统计）全在 BaseAgent。
"""

from __future__ import annotations

from typing import Any

from .difficulty import DifficultyAgent
from .judge import JudgeAgent
from .mastery import MasteryAgent
from .recommend import RecommendAgent
from .roadmap import RoadmapAgent
from .solution import SolutionAgent

AGENTS: dict[str, Any] = {
    "difficulty": DifficultyAgent(),
    "recommend": RecommendAgent(),
    "solution": SolutionAgent(),
    "judge": JudgeAgent(),
    "mastery": MasteryAgent(),
    "roadmap": RoadmapAgent(),
}

# 简历里说的"5 个 Agent"：推荐/题解/评测/学习状态/路线图；
# difficulty 作为题目标注链路的一部分，不算在 5 个之内。
CORE_AGENTS = ["recommend", "solution", "judge", "mastery", "roadmap"]


def get_agent(name: str):
    agent = AGENTS.get(name)
    if agent is None:
        raise KeyError(f"未知 Agent: {name}，可选 {list(AGENTS)}")
    return agent


def validate_registry() -> list[str]:
    """启动自检：Agent 注册表与结构化输出契约必须完全对齐。

    这里让 schemas.SCHEMA_BY_AGENT 从"只导出未使用的死代码"（技术债 D5）
    变成真正的校验依据：
      * 每个 Agent 的 schema 必须已登记在 SCHEMA_BY_AGENT；
      * 登记名与实际 schema 类必须一致；
      * 每个核心 Agent 必须带规则兜底（降级链的前提）。

    返回问题清单，空列表表示健康。启动时调用，配置漂移会立刻暴露而不是
    等到线上降级失败才发现。
    """
    from ..core.agent import AgentContext
    from ..schemas import SCHEMA_BY_AGENT

    problems: list[str] = []
    for name, agent in AGENTS.items():
        registered = SCHEMA_BY_AGENT.get(name)
        if registered is None:
            problems.append(f"Agent '{name}' 的 schema 未登记在 SCHEMA_BY_AGENT")
        elif registered is not agent.schema:
            problems.append(
                f"Agent '{name}' schema 不一致：Agent 用 {agent.schema.__name__}，"
                f"登记为 {registered.__name__}"
            )

    for name in CORE_AGENTS:
        agent = AGENTS.get(name)
        if agent is None:
            problems.append(f"核心 Agent '{name}' 未注册")
            continue
        ctx = AgentContext(data={"language": "python", "tags": ["模拟"]})
        try:
            if agent.run_rule(ctx) is None:
                problems.append(f"核心 Agent '{name}' 缺少规则兜底，降级链会断")
        except Exception as e:  # 兜底本身不能崩
            problems.append(f"核心 Agent '{name}' 的规则兜底抛异常: {e}")

    return problems


__all__ = [
    "AGENTS",
    "CORE_AGENTS",
    "DifficultyAgent",
    "JudgeAgent",
    "MasteryAgent",
    "RecommendAgent",
    "RoadmapAgent",
    "SolutionAgent",
    "get_agent",
    "validate_registry",
]
