"""模拟面试引擎：评分 Rubric（规则算客观分）+ 考官人设 + 报告生成。

设计文档：docs/PRD-2026-10-24-MCP服务与模拟面试.md（Feature B）。

与日常辅导（core/chat.py）的分工：
  * tutor：学生求教练，提示强度按掌握度主动算定，目标是"教会"；
  * interview：考官考学生，**默认不主动给任何提示**（用户索要才给且
    计入提示依赖），目标是"评估"——同样的对话基建，不同的策略与口径。

评分哲学与 hint/quality/mastery 的两阶段一致：**分数全部由规则算定
（可复现、可解释），LLM 只在"分数已定"的前提下写叙事点评**（可以失败
降级为模板），杜绝"模型给分"的不可复现性。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .chat import ChatTurn, CoachContext

# 考官人设：目标是对话式考察，不是教学
INTERVIEWER_SYSTEM = """你是一位技术面试官，正在对候选人做一场算法题单题技术面。

你的职责是**考察**，不是教学：
1. 先让候选人自己讲思路，听完后针对模糊或矛盾之处追问（"如果输入是空的呢"
   "这个复杂度怎么推出来的"），一次只问一个问题；
2. 候选人讲对时不要过度夸奖，简单确认后推进（"可以，那我们看代码"）；
3. 候选人讲错时，不要直接说答案，用反例或追问暴露矛盾，给他修正的机会；
4. 【最重要】你**绝不给出解题步骤、伪代码或完整代码**——这场面试的价值
   在于评估候选人自己能做到什么程度；
5. 候选人明确索要提示时，可以在当前提示档位内给出最小必要的点拨，但必须
   在回复开头注明「(提示)」，并且说完就该让候选人继续；
6. 语气专业、克制、简洁（2-5 句），用中文。"""

# 面试模式下的提示档：初始 direction（默认不给提示），索要才升档
INTERVIEW_INITIAL_STRENGTH = "direction"

# 沟通质量口径："有效思路陈述"的判定规则（可解释、可测试）
_IDEA_MIN_CHARS = 30
_IDEA_KEYWORDS = (
    "思路",
    "想法",
    "因为",
    "所以",
    "枚举",
    "排序",
    "遍历",
    "复杂度",
    "边界",
    "哈希",
    "贪心",
    "二分",
    "双指针",
    "递归",
    "栈",
    "队列",
    "图",
    "状态",
    "转移",
    "维护",
    "单调",
)

# 各维权重（加总 = 1.0）
WEIGHTS = {
    "correctness": 0.35,
    "code_quality": 0.20,
    "hint_dependency": 0.15,
    "time_efficiency": 0.15,
    "communication": 0.15,
}

# 提示依赖：每索要一次扣分
HINT_PENALTY = 25.0
# 时间效率：超时后的地板分
TIME_FLOOR = 20.0


def score_correctness(verdict: str, passed: int, total: int) -> float:
    """正确性：AC=100；按通过用例比例折算；无从判题（IE/未提交）=0。"""
    if verdict == "AC":
        return 100.0
    if total > 0 and verdict not in ("IE", "CE"):
        return round(max(0.0, min(100.0, passed / total * 100.0)), 2)
    return 0.0


def score_code_quality(quality_baseline: float) -> float:
    """代码质量：直接采用 quality 两阶段评分的规则基线（客观指标）。"""
    return round(max(0.0, min(100.0, quality_baseline)), 2)


def score_hint_dependency(hint_requests: int) -> float:
    """提示依赖：0 次=100，每次索要 -25，下限 0。"""
    return round(max(0.0, 100.0 - HINT_PENALTY * max(0, hint_requests)), 2)


def score_time_efficiency(elapsed_seconds: float, limit_minutes: int) -> float:
    """时间效率：用时 ≤ 时限一半 = 100，线性衰减到限时 = 20，超时钳在 20。"""
    if limit_minutes <= 0:
        return 60.0  # 未限时的场次不奖惩，给中性分
    limit_s = limit_minutes * 60.0
    half = limit_s / 2.0
    if elapsed_seconds <= half:
        return 100.0
    if elapsed_seconds >= limit_s:
        return TIME_FLOOR
    ratio = (elapsed_seconds - half) / half
    return round(100.0 - (100.0 - TIME_FLOOR) * ratio, 2)


def communication_turn_scores(history: list[ChatTurn]) -> tuple[int, int, float]:
    """沟通质量：有效思路陈述占用户发言的比例（返回 有效数, 用户数, 分数）。

    "有效"的口径刻意简单可解释：发言 ≥30 字，或命中任一思路关键词。
    面试语境下"简短确认"不算陈述——这正是考官追问想引出的东西。
    """
    user_msgs = [t for t in history if t.role == "user"]
    valid = 0
    for t in user_msgs:
        text = (t.content or "").strip()
        if len(text) >= _IDEA_MIN_CHARS or any(k in text for k in _IDEA_KEYWORDS):
            valid += 1
    ratio = valid / len(user_msgs) if user_msgs else 0.0
    return valid, len(user_msgs), round(ratio * 100.0, 2)


def compute_scores(
    *,
    verdict: str,
    passed: int,
    total: int,
    quality_baseline: float,
    hint_requests: int,
    elapsed_seconds: float,
    limit_minutes: int,
    history: list[ChatTurn],
) -> dict[str, Any]:
    """五维评分 + 加权总分（全部规则算定，LLM 无权改动）。"""
    valid, user_n, comm = communication_turn_scores(history)
    scores = {
        "correctness": score_correctness(verdict, passed, total),
        "code_quality": score_code_quality(quality_baseline),
        "hint_dependency": score_hint_dependency(hint_requests),
        "time_efficiency": score_time_efficiency(elapsed_seconds, limit_minutes),
        "communication": comm,
        "communication_meta": {"valid_turns": valid, "user_turns": user_n},
    }
    total_score = sum(scores[k] * w for k, w in WEIGHTS.items())
    scores["total"] = round(total_score, 2)
    scores["weights"] = dict(WEIGHTS)
    return scores


def parse_meta(meta_json: str) -> dict[str, Any]:
    try:
        data = json.loads(meta_json or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def build_interview_context(
    problem: dict[str, Any], memory: str, history: list[ChatTurn]
) -> CoachContext:
    """面试上下文：题面 + 用户画像，考官人设由调用方 system prompt 承载。"""
    return CoachContext(problem=problem, memory=memory, history=history)


def interview_messages(ctx: CoachContext, user_message: str, strength: str) -> list[dict[str, str]]:
    """面试消息组装：system = 考官人设（不走 build_messages 的教学 SYSTEM）。"""
    messages = [
        {"role": "system", "content": INTERVIEWER_SYSTEM},
        *_ctx_system_blocks(ctx),
        *_history(ctx),
        {"role": "user", "content": (user_message or "")[:2000]},
    ]
    return messages


def _ctx_system_blocks(ctx: CoachContext) -> list[dict[str, str]]:
    blocks = []
    if ctx.problem:
        tags = "、".join(str(t) for t in (ctx.problem.get("tags") or []))
        blocks.append(
            "【面试题目】\n"
            f"标题：{ctx.problem.get('title', '')}\n"
            f"知识点：{tags or '未标注'}　难度：{ctx.problem.get('difficulty', '')}\n"
            f"题面：{str(ctx.problem.get('statement', ''))[:1200]}"
        )
    if ctx.memory:
        blocks.append(f"【候选人背景（聚合结论）】\n{ctx.memory}")
    return [{"role": "system", "content": b} for b in blocks]


def _history(ctx: CoachContext) -> list[dict[str, str]]:
    out = []
    for t in ctx.history[-12:]:
        out.append(
            {
                "role": "user" if t.role == "user" else "assistant",
                "content": (t.content or "")[:600],
            }
        )
    return out


def narrative_fallback(scores: dict[str, Any], verdict: str) -> str:
    """LLM 叙事降级模板：只引用规则分，不编造细节。"""
    lines = [
        f"本场模拟面试结束，最终判题 {verdict or '未完成'}。",
        f"总分 {scores.get('total', 0)}（正确性 {scores.get('correctness', 0)}、"
        f"代码质量 {scores.get('code_quality', 0)}、提示依赖 {scores.get('hint_dependency', 0)}、"
        f"时间效率 {scores.get('time_efficiency', 0)}、沟通质量 {scores.get('communication', 0)}）。",
        "（本报告为规则模板：叙事生成暂不可用，分数均由规则层算定。）",
    ]
    return "\n".join(lines)


_HINT_MARK = re.compile(r"^\s*[（(]提示[)）]")


async def generate_narrative(
    config: dict[str, Any], scores: dict[str, Any], verdict: str, history: list[ChatTurn]
) -> tuple[str, bool]:
    """LLM 叙事点评：只能引用规则算出的数字；失败降级模板。"""
    if str(config.get("provider", "mock")) == "mock":
        return narrative_fallback(scores, verdict), True
    from .providers import LLMError, build_provider

    provider = build_provider(
        str(config.get("provider", "")),
        str(config.get("base_url", "")),
        str(config.get("api_key", "")),
    )
    facts = {k: v for k, v in scores.items() if k != "communication_meta"}
    user_prompt = (
        "以下是本场算法模拟面试的客观评分（已由规则层算定，不可更改），"
        "以及候选人代码的最终判题结果。请写一段 150-250 字的复盘点评："
        "逐维简要点评，指出最该改进的一个维度并给出 1 条具体建议。"
        "只允许引用给出的数字，不得编造未提供的细节。\n\n"
        f"评分：{json.dumps(facts, ensure_ascii=False)}\n判题结果：{verdict or '未提交'}\n"
        f"对话轮次：{sum(1 for t in history if t.role == 'user')} 个用户发言"
    )
    messages = [
        {"role": "system", "content": "你是严谨的面试复盘官，只依据给定事实写点评，用中文。"},
        {"role": "user", "content": user_prompt},
    ]
    try:
        result = await provider.complete_chat(
            messages,
            str(config.get("model", "")),
            temperature=float(config.get("temperature", 0.3)),
        )
        text = (result.text or "").strip()
        if not text:
            raise ValueError("空回复")
        return text, False
    except (TimeoutError, LLMError, ValueError, Exception):
        return narrative_fallback(scores, verdict), True


def hint_request_flag(reply_text: str) -> bool:
    """考官回复是否携带了提示（以「(提示)」标记为准，便于统计口径稳定）。"""
    return bool(_HINT_MARK.match(reply_text or ""))


def elapsed_seconds_of(meta: dict[str, Any]) -> float:
    started = float(meta.get("started_at") or 0.0)
    if started <= 0:
        return 0.0
    finished = float(meta.get("finished_at") or 0.0) or time.time()
    return max(0.0, finished - started)
