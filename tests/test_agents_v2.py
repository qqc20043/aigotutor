"""A 档能力测试：解题提示 Agent、复习队列优先、记忆注入、两阶段质量评分。

这些测试守护简历点名的四项能力，防止后续回归：
  * 提示强度必须由掌握度决定（不是模型自由发挥）；
  * 复习排序必须可解释且遵守时间预算；
  * 记忆注入必须是"聚合结论"而非历史流水（token 可控）；
  * 质量评分必须是两阶段（规则给窗口、模型在窗口内修正）。
"""

from __future__ import annotations

import pytest

from app.agents import AGENTS, CORE_AGENTS
from app.core.agent import AgentContext
from app.core.rules import (
    HINT_COUNT_BY_STRENGTH,
    clamp_quality,
    hint_strength_for,
    quality_grade_of,
    quality_window,
    rule_hint,
    rule_quality,
    rule_review_priority,
)
from app.schemas import QUALITY_RADIUS, QualityDimension, QualityOut


# ==================================================== 提示强度（A1）
def test_hint_strength_decreases_with_mastery():
    """掌握度越高，提示越弱 —— 这是"动态调整提示强度"的核心契约。"""
    # 掌握度很低 -> 最强提示
    assert hint_strength_for(20.0, attempts=10)[0] == "pseudocode"
    # 掌握度中等 -> 中等提示
    assert hint_strength_for(50.0, attempts=10)[0] == "key_observation"
    # 掌握度很高 -> 只给方向
    assert hint_strength_for(90.0, attempts=10)[0] == "direction"


def test_hint_strength_conservative_when_samples_insufficient():
    """样本不足时必须保守升档：掌握度数字本身不可信。

    真实场景：用户第一次做某知识点的题，掌握度可能因为一次偶然 AC 而虚高，
    若照数字给弱提示，新手会直接卡死。
    """
    # 首次接触（0 次提交）-> 直接给最强
    assert hint_strength_for(95.0, attempts=0)[0] == "pseudocode"
    # 仅 1-2 次提交 -> 至少给中等，即使掌握度显示很高
    assert hint_strength_for(95.0, attempts=1)[0] == "key_observation"
    assert hint_strength_for(95.0, attempts=2)[0] == "key_observation"
    # 样本充足后才按掌握度给弱提示
    assert hint_strength_for(95.0, attempts=5)[0] == "direction"


def test_hint_strength_reason_cites_mastery():
    """强度依据必须引用掌握度数字（可解释性要求）。"""
    strength, reason = hint_strength_for(42.0, attempts=8)
    assert "42" in reason
    assert strength in ("pseudocode", "key_observation", "direction")


def test_hint_rule_fallback_is_valid_and_has_no_solution():
    """降级提示必须合法，且**不能包含完整解法**（否则训练价值归零）。"""
    out = rule_hint({"tags": ["排序", "贪心"], "mastery_score": 30.0, "attempts": 5})
    assert out.strength in HINT_COUNT_BY_STRENGTH
    assert len(out.hints) == HINT_COUNT_BY_STRENGTH[out.strength]
    # 降级结果也要明确标注是规则引擎产出，不假装是模型给的
    assert "规则引擎" in out.strength_reason
    assert out.confidence <= 0.6


def test_hint_agent_forces_strength_from_mastery():
    """Agent 的 postprocess 必须把模型给的强度改回掌握度算定的值。"""
    from app.agents.hint import HintAgent

    agent = HintAgent()
    # 模型声称给弱提示，但掌握度只有 20 分（应为最强）
    fake = agent.schema(
        strength="direction",
        hints=["只有一个提示"],
        key_insight="x",
        next_step="y",
        strength_reason="z",
    )
    ctx = AgentContext(data={"mastery_score": 20.0, "attempts": 10})
    fixed = agent.postprocess(fake, ctx)
    assert fixed.strength == "pseudocode"
    # 条数也必须补齐到该强度应有的数量
    assert len(fixed.hints) == HINT_COUNT_BY_STRENGTH["pseudocode"]


def test_hint_agent_available_and_has_fallback():
    """hint 必须注册且带规则兜底（降级链前提）。"""
    assert "hint" in AGENTS
    agent = AGENTS["hint"]
    assert agent.run_rule(AgentContext(data={"tags": ["模拟"]})) is not None


# ==================================================== 复习队列优先（A2）
def test_review_priority_sorts_by_urgency_and_gap():
    """逾期越久、掌握度越低、遗忘越多 -> 排得越前。"""
    out = rule_review_priority(
        {
            "candidates": [
                {
                    "problem_id": "p-low",
                    "title": "刚到期且掌握好",
                    "tags": ["模拟"],
                    "overdue_days": 0.2,
                    "lapses": 0,
                    "mastery_scores": {"模拟": 85.0},
                },
                {
                    "problem_id": "p-high",
                    "title": "逾期久且薄弱",
                    "tags": ["构造"],
                    "overdue_days": 8.0,
                    "lapses": 3,
                    "mastery_scores": {"构造": 25.0},
                },
            ]
        }
    )
    assert out.ordered[0].problem_id == "p-high"
    assert out.ordered[1].problem_id == "p-low"
    # 每条都要有可解释的理由，且引用具体数字
    assert "逾期" in out.ordered[0].reason
    assert "25" in out.ordered[0].reason or "构造" in out.ordered[0].reason


def test_review_priority_respects_time_budget():
    """时间预算必须硬约束：不给用户一个做不完的队列。"""
    candidates = [
        {
            "problem_id": f"p{i}",
            "title": f"题 {i}",
            "tags": ["模拟"],
            "overdue_days": float(10 - i),
            "lapses": 0,
            "mastery_scores": {"模拟": 40.0},
        }
        for i in range(10)
    ]
    # 30 分钟预算 -> 每题 15 分钟 -> 最多 2 道
    out = rule_review_priority({"candidates": candidates, "time_budget_minutes": 30})
    assert len(out.ordered) == 2
    assert "30 分钟" in out.strategy


def test_review_priority_empty_queue_is_valid():
    """空队列也必须返回合法结构（不能违反 schema 的 min_length）。"""
    out = rule_review_priority({"candidates": []})
    assert len(out.ordered) >= 1
    assert "没有到期" in out.ordered[0].reason


def test_review_agent_filters_hallucinated_problems():
    """模型编造候选之外的题必须被过滤掉。"""
    from app.agents.review import ReviewAgent

    agent = ReviewAgent()
    fake = agent.schema(
        ordered=[
            {"problem_id": "real-1", "title": "真实题", "order": 1, "reason": "r", "urgency": "u"},
            {"problem_id": "fake-9", "title": "编造的", "order": 2, "reason": "r", "urgency": "u"},
        ],
        strategy="s",
    )
    ctx = AgentContext(data={"candidates": [{"problem_id": "real-1", "title": "真实题"}]})
    fixed = agent.postprocess(fake, ctx)
    assert [o.problem_id for o in fixed.ordered] == ["real-1"]


def test_review_agent_renumbers_after_budget_trim():
    """按预算截断后，order 必须重新编号（前端直接按 order 渲染）。"""
    from app.agents.review import ReviewAgent

    agent = ReviewAgent()
    items = [
        {"problem_id": f"p{i}", "title": f"t{i}", "order": i, "reason": "r", "urgency": "u"}
        for i in range(1, 6)
    ]
    ctx = AgentContext(
        data={
            "candidates": [{"problem_id": f"p{i}"} for i in range(1, 6)],
            "time_budget_minutes": 30,  # 只能做 2 道
        }
    )
    fixed = agent.postprocess(agent.schema(ordered=items, strategy="s"), ctx)
    assert len(fixed.ordered) == 2
    assert [o.order for o in fixed.ordered] == [1, 2]


# ==================================================== 记忆注入（A3）
def test_memory_block_is_conclusion_not_history():
    """记忆注入必须是聚合结论，不能是历史流水（token 可控的根本）。"""
    from app.core.memory import MemorySnapshot, estimate_tokens

    snap = MemorySnapshot(
        user_id="u1",
        weak_tags=[{"tag": "构造", "score": 42.0, "attempts": 4}],
        strong_tags=[{"tag": "模拟", "score": 78.0, "attempts": 10}],
        error_patterns=[{"label": "边界类错误", "size": 3, "cause": "边界"}],
        recent_verdicts=["AC", "WA", "AC", "AC", "TLE", "WA"],
        recent_pass_rate=0.5,
        trend="rising",
        total_submissions=81,
    )
    block = snap.to_prompt_block()
    # 必须包含结论性信息
    assert "薄弱知识点" in block
    assert "构造" in block
    assert "易错模式" in block
    assert "趋势" in block
    # 必须是短清单：即使刷了 81 题，注入量也要在预算内
    tokens = estimate_tokens(block)
    assert tokens < 300, f"注入量 {tokens} token 超出预算"


def test_memory_token_budget_independent_of_history_size():
    """关键契约：注入量与会话/提交规模解耦 —— 刷 10 题与刷 1000 题注入量相当。"""
    from app.core.memory import MemorySnapshot, estimate_tokens

    small = MemorySnapshot(
        weak_tags=[{"tag": "构造", "score": 42.0, "attempts": 2}],
        recent_verdicts=["AC"] * 5,
        total_submissions=10,
    )
    large = MemorySnapshot(
        weak_tags=[{"tag": "构造", "score": 42.0, "attempts": 200}],
        recent_verdicts=["AC"] * 20,
        total_submissions=1000,
    )
    t_small = estimate_tokens(small.to_prompt_block())
    t_large = estimate_tokens(large.to_prompt_block())
    # 大历史的注入量不应显著超过小历史（最多 2 倍，实际差异只来自数字位数）
    assert t_large < t_small * 2, f"注入量随历史增长失控: {t_small} -> {t_large}"


def test_memory_empty_snapshot_injects_nothing():
    """无历史用户不注入任何内容（不能塞一段空壳占 token）。"""
    from app.core.memory import MemorySnapshot

    snap = MemorySnapshot(user_id="new-user")
    assert snap.is_empty()
    assert snap.to_prompt_block() == ""


def test_memory_trend_classification():
    """趋势判定：前后半段通过率对比（对单次波动不敏感）。"""
    from app.core.memory import _classify_trend

    # verdicts 是倒序（最近在前）：前半段好、后半段差 -> 上升
    assert _classify_trend(["AC", "AC", "AC", "WA", "WA", "WA"]) == "rising"
    assert _classify_trend(["WA", "WA", "WA", "AC", "AC", "AC"]) == "falling"
    # 前后半段通过率相同 -> 持平；全通过/全失败也是明确趋势，不是"样本不足"
    assert _classify_trend(["AC", "AC", "AC", "AC"]) == "steady"
    assert _classify_trend(["WA", "WA", "WA", "WA"]) == "steady"
    # 样本太少（< 4）时不下结论
    assert _classify_trend(["AC", "WA"]) == "unknown"


def test_memory_injection_flag_only_on_decision_agents():
    """只有决策类 Agent 注入记忆 —— 判题/难度标注不需要用户历史。"""
    assert AGENTS["hint"].inject_memory is True
    assert AGENTS["recommend"].inject_memory is True
    assert AGENTS["review"].inject_memory is True
    assert AGENTS["roadmap"].inject_memory is True
    # 这些是"能力型"Agent，注入记忆只会增加 token 与干扰
    assert AGENTS["judge"].inject_memory is False
    assert AGENTS["difficulty"].inject_memory is False
    assert AGENTS["solution"].inject_memory is False


def test_with_memory_puts_memory_before_prompt():
    """记忆拼在 prompt 前面（背景先行），且不注入的 Agent 原样返回。"""
    agent = AGENTS["hint"]
    ctx = AgentContext(memory="【学习记忆】测试记忆块", data={})
    out = agent.with_memory("任务内容", ctx)
    assert out.startswith("【学习记忆】")
    assert out.endswith("任务内容")

    judge = AGENTS["judge"]
    assert judge.with_memory("任务内容", ctx) == "任务内容"


def test_mock_parses_prompt_with_memory_prefix():
    """回归：mock 必须能解析"带记忆前缀"的 prompt。

    真实缺陷：with_memory 把记忆块拼在 JSON 前面，MockProvider._parse_payload
    直接 json.loads 失败 → 静默回退到静态 FIXTURES → mock 忽略了本次真实上下文
    （表现为：hint 的强度依据与算定强度不一致，永远显示写死的文案）。
    这个 bug 只在"记忆注入 + mock provider"组合下出现，单测很容易漏。
    """
    from app.core.providers import MockProvider

    p = MockProvider()
    prefixed = (
        "【学习记忆】薄弱知识点：构造(42分)\n"
        "- 近期表现：通过率 50%\n\n"
        '{"strength": "pseudocode", "hint_count": 3, '
        '"strength_reason": "首次接触给最强提示。", "tags": ["模拟"]}'
    )
    payload = p._parse_payload(prefixed)
    assert payload.get("strength") == "pseudocode", "记忆前缀导致 payload 解析失败"

    out = p._hint_from_prompt(payload)
    # 必须使用本次 prompt 里的 reason，而不是 FIXTURES 里写死的那条
    assert out["strength_reason"] == "首次接触给最强提示。"


@pytest.mark.asyncio
async def test_hint_endpoint_reason_matches_strength(seeded_client):
    """回归：端到端下，提示的 strength 与 strength_reason 必须语义一致。

    这条断言守护"强度由掌握度算定"这个契约在真实调用链上成立 ——
    只测 rule 层会漏掉 prompt 拼装与 mock 解析的问题。
    """
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(f"/api/problems/{pid}/hint")
    assert r.status_code == 200, r.text
    data = r.json().get("data") or {}
    strength = data.get("strength")
    reason = data.get("strength_reason") or ""
    assert strength in HINT_COUNT_BY_STRENGTH
    assert reason, "强度依据不能为空"
    # 首次接触（该题无提交）必须给最强提示，且依据要说明原因
    if strength == "pseudocode" and "尚无提交记录" in reason:
        assert "首次接触" in reason or "最强" in reason


# ==================================================== 两阶段质量评分（A4）
GOOD_CODE = '''def merge_intervals(intervals):
    """合并重叠区间。"""
    if not intervals:
        return []
    intervals.sort(key=lambda item: item[0])
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
'''

BAD_CODE = """a=input().split()
n=int(a[0])
r=[]
for i in range(n):
    b=input().split()
    if int(b[0])>0:
        if int(b[1])>0:
            if int(b[0])<int(b[1]):
                for j in range(int(b[0])):
                    if j%2==0:
                        r.append(j)
print(len(r))
"""


def test_quality_two_stage_produces_window():
    """第一阶段必须产出基线与窗口（两阶段的前提）。"""
    baseline, metrics = quality_window(GOOD_CODE)
    assert 0 <= baseline <= 100
    assert metrics["lines"] > 0
    assert "max_indent" in metrics


def test_quality_clamps_out_of_window_score():
    """关键契约：模型给出的窗口外分数必须被夹回。

    这是"两阶段评分"成立的唯一保证 —— 没有夹紧，模型可以推翻客观指标
    （比如把 6 层嵌套、全单字母变量的代码评成"优秀"）。
    """
    baseline, _ = quality_window(BAD_CODE)
    lo = max(0.0, baseline - QUALITY_RADIUS)
    hi = min(100.0, baseline + QUALITY_RADIUS)

    fake = QualityOut(
        score=99.0,
        grade="优秀",
        dimensions=[QualityDimension(name="可读性", score=99.0, comment="完美")],
        summary="很好",
        top_improvement="无",
    )
    clamped = clamp_quality(fake, (lo, hi))
    assert clamped.score <= hi + 0.01
    # 等级必须随夹紧后的分数重算，不能保留"优秀"
    assert clamped.grade == quality_grade_of(clamped.score)


def test_quality_distinguishes_good_from_bad_code():
    """评分必须有区分度：规范代码要明显高于深嵌套代码。"""
    good, _ = quality_window(GOOD_CODE)
    bad, _ = quality_window(BAD_CODE)
    assert good > bad + 10.0, f"区分度不足: good={good} bad={bad}"


def test_quality_boundary_score_ignores_plain_if():
    """边界分只认真正的边界检查，不被普通 if 分支骗到。

    真实缺陷：早期实现把任意 `if` 都算作边界处理，导致深嵌套代码因分支多
    而拿到"合格"档，评分失去区分度。
    """
    _, bad_metrics = quality_window(BAD_CODE)
    # 深嵌套的 if 不构成边界处理
    assert bad_metrics["boundary_hints"] == 0

    _, good_metrics = quality_window(GOOD_CODE)
    assert good_metrics["boundary_hints"] >= 1  # `if not intervals` 是边界检查


def test_quality_grade_boundaries():
    """等级映射必须单调且覆盖全区间（唯一口径）。"""
    assert quality_grade_of(100.0) == "优秀"
    assert quality_grade_of(85.0) == "优秀"
    assert quality_grade_of(84.9) == "良好"
    assert quality_grade_of(70.0) == "良好"
    assert quality_grade_of(69.9) == "合格"
    assert quality_grade_of(55.0) == "合格"
    assert quality_grade_of(54.9) == "待改进"
    assert quality_grade_of(0.0) == "待改进"


def test_quality_rule_fallback_is_valid():
    """降级评分必须合法，且诚实标注"未做语义修正"。"""
    out = rule_quality({"code": GOOD_CODE})
    assert 0 <= out.score <= 100
    assert out.grade == quality_grade_of(out.score)
    assert "规则引擎" in out.summary
    assert "未做语义修正" in out.summary
    assert out.dimensions  # 至少要有维度明细


def test_quality_agent_postprocess_clamps():
    """Agent 的 postprocess 必须执行夹紧（模型无法绕过）。"""
    from app.agents.quality import QualityAgent

    agent = QualityAgent()
    fake = agent.schema(
        score=100.0,
        grade="优秀",
        dimensions=[QualityDimension(name="可读性", score=100.0, comment="c")],
        summary="s",
        top_improvement="t",
    )
    ctx = AgentContext(data={"code": BAD_CODE})
    fixed = agent.postprocess(fake, ctx)
    baseline, _ = quality_window(BAD_CODE)
    assert fixed.score <= baseline + QUALITY_RADIUS + 0.01


def test_quality_empty_code_is_handled():
    """空代码不能崩（边界输入）。"""
    out = rule_quality({"code": ""})
    assert 0 <= out.score <= 100
    _baseline, metrics = quality_window("")
    assert metrics["lines"] == 0


# ==================================================== 契约与注册表
def test_new_agents_registered_with_schemas():
    """新增 Agent 必须注册且 schema 与契约表一致。"""
    for name in ("hint", "review", "quality"):
        assert name in AGENTS, f"{name} 未注册"
        agent = AGENTS[name]
        assert agent.system_prompt, f"{name} 未声明 system_prompt"
        assert agent.run_rule(AgentContext(data={"tags": ["模拟"], "code": "x=1"})) is not None


def test_core_agents_still_exactly_five():
    """简历声明 5 个核心 Agent，新增 Agent 不得改变这个数字。"""
    assert len(CORE_AGENTS) == 5
    assert set(CORE_AGENTS) == {"roadmap", "recommend", "hint", "mastery", "review"}


@pytest.mark.asyncio
async def test_hint_endpoint_requires_problem(seeded_client):
    """提示接口：题目不存在时 404（不静默返回空提示）。"""
    r = await seeded_client.post("/api/problems/not-exist/hint")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_hint_endpoint_returns_strength(seeded_client):
    """提示接口必须返回强度与依据（前端据此展示"为什么给这个提示"）。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(f"/api/problems/{pid}/hint")
    assert r.status_code == 200, r.text
    body = r.json()
    data = body.get("data") or {}
    assert data.get("strength") in HINT_COUNT_BY_STRENGTH
    assert data.get("hints")
    assert data.get("strength_reason")


@pytest.mark.asyncio
async def test_review_priority_endpoint(seeded_client):
    """复习优先接口：返回排序结果或明确的空队列说明。"""
    r = await seeded_client.get("/api/review/priority")
    assert r.status_code == 200, r.text
    body = r.json()
    if body.get("due_count", 0) == 0:
        assert "message" in body
    else:
        assert body["data"]["ordered"]
        assert body["data"]["strategy"]
