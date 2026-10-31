"""MockProvider 内部生成器全路径：fixture 造值 / 质量评分 / 解析容错。"""

from __future__ import annotations

import json

from app.core.providers import MockProvider


def _p() -> MockProvider:
    return MockProvider()


# ============================================================ _fake_value
def test_fake_value_all_types():
    p = _p()
    assert p._fake_value("k", {"enum": ["甲", "乙"]}) == "甲"
    assert p._fake_value("k", {"const": "固定"}) == "固定"
    assert p._fake_value("k", {"type": "array", "items": {"type": "string"}}) == ["mock"]
    assert p._fake_value("k", {"type": "integer"}) == 1
    assert p._fake_value("k", {"type": "number"}) == 1.0
    assert p._fake_value("k", {"type": "boolean"}) is True
    assert p._fake_value("k", {"type": "object", "properties": {"a": {"type": "integer"}}}) == {
        "a": 1
    }
    assert p._fake_value("difficulty_key", {"type": "string"}) == "普及-"
    assert p._fake_value("reason_key", {"type": "string"}) == "Mock 输出：离线模式下的确定性结果。"
    assert p._fake_value("other", {"type": "string", "default": "默认"}) == "默认"


def test_fake_value_type_list_takes_first():
    assert _p()._fake_value("k", {"type": ["integer", "string"]}) == 1


# ============================================================ _fixture_for 兜底
def test_fixture_for_unknown_schema_builds_from_properties():
    p = _p()
    schema = {
        "title": "UnknownOut",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "verdict": {"enum": ["AC", "WA"]},
        },
    }
    out = p._fixture_for(schema)
    assert out["name"] == "mock" and out["count"] == 1 and out["verdict"] == "AC"


def test_fixture_for_static_fixtures():
    """内置 FIXTURES（如 ReviewPriorityOut 兜底）在无法解析 prompt 时返回。"""
    p = _p()
    out = p._fixture_for({"title": "ReviewPriorityOut"}, user="not-json")
    assert "ordered" in out


def test_fixture_for_mastery_uses_rule_baseline():
    p = _p()
    baseline = {"score": 72.5, "level": "L3"}
    out = p._fixture_for(
        {"title": "MasteryOut"}, user=json.dumps({"rule_baseline": baseline, "mastery": {}})
    )
    assert out == baseline


def test_fixture_for_mastery_ignores_empty_baseline():
    p = _p()
    out = p._fixture_for(
        {"title": "RoadmapOut"}, user=json.dumps({"rule_baseline": {}, "stages": []})
    )
    assert "stages" in out, "空 baseline 应回落到 FIXTURES/造值"


# ============================================================ 解析容错
def test_parse_payload_edge_cases():
    assert MockProvider._parse_payload("纯文本无 JSON") == {}
    assert MockProvider._parse_payload("") == {}
    # 带记忆前缀的 JSON：截取首个 { 到末个 }
    payload = MockProvider._parse_payload('记忆块\n{"a": 1}\n尾部')
    assert payload == {"a": 1}
    # 坏 JSON：返回空 dict
    assert MockProvider._parse_payload("{broken") == {}


# ============================================================ 质量评分路径
def test_quality_from_prompt_clamps_and_dimensions():
    p = _p()
    payload = {
        "rule_baseline": 70.0,
        "allowed_range": [58.0, 82.0],
        "rule_metrics": {"max_indent": 3, "short_ident_ratio": 0.1, "boundary_hints": 2},
        "required_dimensions": ["正确性", "可读性", "效率", "边界处理"],
    }
    out = p._quality_from_prompt(payload)
    assert out["score"] == 70.0
    assert [d["name"] for d in out["dimensions"]] == payload["required_dimensions"]
    assert "最大嵌套 3 层" in out["dimensions"][1]["comment"]


def test_quality_from_prompt_defaults():
    """缺省指标时用默认维度与基线（mock 恒等映射语义）。"""
    out = _p()._quality_from_prompt({})
    assert out["score"] == 60.0
    assert len(out["dimensions"]) == 4


# ============================================================ 判题归因路径
def test_judge_from_prompt_all_verdicts():
    p = _p()
    for verdict in ("AC", "WA", "TLE", "MLE", "RE", "CE"):
        out = p._judge_from_prompt({"verdict": verdict, "code": "x"})
        assert out["verdict"] == verdict
        assert out["explanation"]


def test_judge_from_prompt_ac_root_unknown():
    """AC 时根因应为未知（对的代码没有错误根因）。"""
    out = _p()._judge_from_prompt({"verdict": "AC", "code": "x"})
    assert out["root_cause"] == "未知"


# ============================================================ 推荐路径
def test_recommend_from_prompt_empty_candidates():
    out = _p()._recommend_from_prompt({"candidates": [], "mastery": {}})
    assert out["problem_id"] == ""
    assert "无法推荐" in out["reason"]


def test_recommend_from_prompt_all_recent():
    """候选全部做过时回退到全候选（宁可重复也不空手）。"""
    p = _p()
    candidates = [
        {
            "problem_id": "p1",
            "title": "甲",
            "difficulty": "入门",
            "difficulty_score": 30.0,
            "tags": ["模拟"],
        }
    ]
    out = p._recommend_from_prompt(
        {
            "candidates": candidates,
            "recent_problem_ids": ["p1"],
            "mastery": {"模拟": 40.0},
            "target_score": 50.0,
        }
    )
    assert out["problem_id"] == "p1"
