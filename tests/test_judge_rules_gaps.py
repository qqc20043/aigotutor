"""judge/base.py 与 core/rules.py 剩余分支补齐（纯逻辑直接调用）。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from app.core.mastery import compute_tag_stats
from app.core.rules import (
    _normalize_tags,
    difficulty_of,
    hint_strength_for,
    quality_grade_of,
    rule_judge,
    rule_review_priority,
    rule_roadmap,
    rule_solution,
)
from app.judge.base import (
    NullBackend,
    build_backend,
    describe_backend,
    language_available,
    normalize_output,
    probe_language,
    reset_backend_cache,
    resolve_toolchain,
    strip_bom,
)


# ============================================================ judge/base
def test_normalize_output_variants():
    assert normalize_output("  abc  \n\n") == "  abc"
    assert normalize_output("a\r\nb\r\n") == "a\nb"
    assert normalize_output("") == ""
    assert normalize_output("  \n  \n") == ""


def test_strip_bom():
    assert strip_bom("\ufeffabc") == "abc"
    assert strip_bom("abc") == "abc"
    assert strip_bom("") == ""


def test_null_backend():
    nb = NullBackend()
    assert nb.available_languages() == []


def test_build_backend_none_and_unknown():

    reset_backend_cache()
    assert build_backend("none").name == "none"
    assert build_backend("unknown-xyz").name == "none"


def test_describe_backend_none():
    reset_backend_cache()
    with patch("app.config.settings.JUDGE_BACKEND", "none"):
        info = describe_backend()
    assert info["backend"] == "none"
    assert info["available"] is False


def test_resolve_toolchain_python():
    """python 必在 PATH（测试进程自己就是 python）。"""
    cmd = resolve_toolchain("python")
    assert cmd, "python 工具链必须可解析"
    assert "python" in cmd[0] or "python" in " ".join(cmd)


def test_probe_language_python_ok():
    ok, _msg = probe_language("python")
    assert ok is True


def test_probe_language_unknown():
    ok, msg = probe_language("brainfuck")
    assert ok is False and "brainfuck" in msg


def test_language_available_python():
    assert language_available("python") is True


def test_judge_service_and_base_extra():
    """judge/base 96/122/132：availability dict 与 score 计算。"""
    from app.judge.base import JudgeResult

    r = JudgeResult(verdict="AC", passed=2, total=2)
    assert r.score == 100.0
    r2 = JudgeResult(verdict="IE", passed=0, total=0)
    assert r2.score == 0.0


# ============================================================ rules 分支
def test_difficulty_of_boundary():
    assert difficulty_of(0.0) == "入门"
    assert difficulty_of(100.0) == "提高+/省选"


def test_rule_judge_wa_fallback():
    """非法 verdict 回退 WA（279 行）。"""
    out = rule_judge({"verdict": "bogus", "code": "x"})
    assert out.verdict == "WA"


def test_hint_strength_direction_boundary():
    """掌握度刚好 65 -> direction（358 行分支）。"""
    strength, reason = hint_strength_for(65.0, 10)
    assert strength == "direction"
    assert "65" in reason


def test_rule_review_single_mastery_value():
    """mastery_scores 是单值 dict（431 行 min 分支）。"""
    out = rule_review_priority(
        {
            "candidates": [
                {
                    "problem_id": "p1",
                    "title": "t",
                    "tags": ["贪心"],
                    "overdue_days": 2,
                    "lapses": 0,
                    "mastery_scores": {"贪心": 25.0},
                }
            ],
            "time_budget_minutes": 0,
        }
    )
    assert out.ordered[0].problem_id == "p1"
    assert "25" in out.ordered[0].reason


def test_quality_grade_and_comment_branches():
    """注释密度分支（594-599 行）与档位（499 行）。"""
    assert quality_grade_of(95.0) == "优秀"
    assert quality_grade_of(75.0) == "良好"
    assert quality_grade_of(55.0) == "合格"
    assert quality_grade_of(30.0) == "待改进"


def test_solution_preset_branches():
    """rule_solution 的 preset 分支（190/198 行）。"""
    out = rule_solution(
        {
            "title": "两数之和",
            "statement": "求和",
            "tags": ["模拟"],
            "language": "python",
            "preset": "hash",
        }
    )
    assert out is not None


def test_compute_tag_stats_datetime_variants():
    """时间解析三态：datetime 对象 / ISO 字符串 / 非法字符串（119-126 行）。"""

    good_dt = datetime.now(timezone.utc)
    iso_str = good_dt.isoformat()
    bad_str = "not-a-date"
    submissions = [
        {"problem_id": "p", "verdict": "AC", "created_at": good_dt},
        {"problem_id": "p", "verdict": "WA", "created_at": iso_str},
        {"problem_id": "p", "verdict": "AC", "created_at": bad_str},
    ]
    problem_tags = {"p": [("贪心", 1.0)]}
    problem_difficulty = {"p": 40.0}
    stats = compute_tag_stats(submissions, problem_tags, problem_difficulty)
    assert "贪心" in stats
    assert stats["贪心"]["attempts"] == 3


def test_rule_roadmap_normalize_tags_variants():
    """_normalize_tags：字符串 JSON / 纯字符串 / dict 列表 / 非法输入。"""
    out = _normalize_tags('[{"name": "甲", "id": "t0"}]')
    assert out[0]["name"] == "甲"
    out = _normalize_tags(["乙", "丙"])
    assert [o["name"] for o in out] == ["乙", "丙"]
    out = _normalize_tags([{"name": "丁"}])
    assert out[0]["name"] == "丁"
    assert _normalize_tags(12345) == []
    assert _normalize_tags(None) == []


def test_rule_roadmap_cycle_safe():
    """先修关系成环时 depth 不得死循环（279 行附近）。"""
    tags = [
        {"id": "a", "name": "A", "prereq_ids": '["b"]'},
        {"id": "b", "name": "B", "prereq_ids": '["a"]'},
    ]
    out = rule_roadmap({"tags": tags, "mastery": {}, "weeks": 4})
    assert out.stages  # 不死循环即通过


def test_rule_hint_score_75_direction():
    """掌握度 75 -> direction（358 行完整分支）。"""
    strength, reason = hint_strength_for(75.0, 5)
    assert strength == "direction"
    assert "75" in reason
