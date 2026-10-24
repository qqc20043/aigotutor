"""坏 JSON 修复层与 schema 宽容化的回归测试。

来源：真模型行为评估首轮（deepseek 网关）——
  * 网关无视 strict json_schema，模型在中文句子里输出未转义的 ASCII 引号
    （`符合"先难后易"的编排意图`），把 JSON 直接打断，三次重试全灭 → 降级；
  * 模型会自创同义字段名（ordering_intent / notes）或漏掉可推断字段
    （order / tags），schema 层拒绝只会白白整单重试。
"""

from __future__ import annotations

import json

from app.agents.review import ReviewAgent
from app.core.agent import AgentContext, repair_broken_json
from app.schemas import ReviewPriorityItem, ReviewPriorityOut


def test_repair_escapes_inner_quotes():
    broken = '{"reason": "符合"先难后易"的编排意图", "k": 1}'
    data = json.loads(repair_broken_json(broken))
    assert data["reason"] == '符合"先难后易"的编排意图'
    assert data["k"] == 1


def test_repair_keeps_valid_json_untouched():
    text = '{"a": "x", "b": [1, 2], "c": {"d": "y"}}'
    assert repair_broken_json(text) == text


def test_repair_keeps_existing_escapes():
    text = '{"a": "行1\\n行2", "b": "引号\\" 内"}'
    assert repair_broken_json(text) == text


def test_repair_multiline_with_inner_quotes():
    broken = (
        '{\n  "ordered": [\n    {\n      "reason": "逾期 0 天，符合"先难后易"的编排"\n    }\n  ]\n}'
    )
    data = json.loads(repair_broken_json(broken))
    assert "先难后易" in data["ordered"][0]["reason"]


def test_parse_repairs_broken_model_output():
    """parse 链路：断 JSON 修复后必须能通过 schema 校验，而不是触发重试降级。"""
    agent = ReviewAgent()
    broken = (
        '{"ordered": [{"problem_id": "p1", "title": "甲", '
        '"reason": "逾期 3 天，要"先难后易"地复习。"}], "confidence": 0.8}'
    )
    data = agent.parse(broken)
    assert data.ordered[0].problem_id == "p1"
    assert "先难后易" in data.ordered[0].reason


def test_review_postprocess_backfills_missing_fields():
    """order 缺省重编号 + strategy 缺省回填（模型自创字段名/漏写时的兜底）。"""
    agent = ReviewAgent()
    ctx = AgentContext(
        data={
            "candidates": [
                {
                    "problem_id": "p1",
                    "title": "甲",
                    "tags": ["贪心"],
                    "overdue_days": 3,
                    "lapses": 0,
                    "mastery_scores": {"贪心": 30.0},
                },
                {
                    "problem_id": "p2",
                    "title": "乙",
                    "tags": ["二分"],
                    "overdue_days": 1,
                    "lapses": 0,
                    "mastery_scores": {"二分": 60.0},
                },
            ],
            "time_budget_minutes": 0,
            "rule_baseline_order": ["p1", "p2"],
        }
    )
    data = ReviewPriorityOut(
        ordered=[
            ReviewPriorityItem(problem_id="p1", title="甲", reason="逾期 3 天"),
            ReviewPriorityItem(problem_id="p2", title="乙", reason="掌握度 60 分"),
        ]
    )
    out = agent.postprocess(data, ctx)
    assert out.strategy, "strategy 缺省必须被回填"
    assert [i.order for i in out.ordered] == [1, 2], "order 必须按实际顺序重编号"
