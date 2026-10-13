"""学习记忆：把用户的历史压缩成"关键事实清单"，注入 Agent 上下文。

对应简历主张：
  "记忆与复习：结合间隔重复算法，对错题做更多训练、久不练习衰减表现、
   易错点聚类、反复提问自动归因，间隔三记忆注入推荐与提示形成数据闭环"
  "清单仅保留关键字符，避免把全部历史注入上下文导致 Token 暴涨"

核心设计（这是本模块存在的唯一理由）：
  **注入的不是历史，是结论。**

  一个刷了 200 题的用户，原始历史（全部提交 + 全部错题 + 全部对话）轻松
  超过 10 万 token。把这些塞进每次 prompt，成本会失控、且模型注意力被稀释。
  但模型真正需要的从来不是"他做过哪些题"，而是：

      他现在哪里弱、什么错误模式反复出现、最近状态是上升还是下滑。

  本模块把历史**离线聚合成一份短清单**（目标 < 300 token），Agent 只读清单。
  于是：历史规模与 prompt 规模解耦 —— 刷 10 题和刷 1000 题，注入量基本恒定。

三个来源（都是已有数据，不新增采集）：
  1. 掌握度画像    -> 最弱/最强知识点 + 分数（数据闭环的"状态"）
  2. 错题本 + 聚类  -> 反复出错的知识点组合与归因（数据闭环的"归因"）
  3. 提交时间序列   -> 近期趋势（上升/下滑/持平）（数据闭环的"趋势"）

注入方式：`MemorySnapshot.to_prompt_block()` 产出一段结构化文本，
由各 Agent 在 build_user() 里可选拼入。**默认只注入摘要，不注入明细。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ErrorCluster, MasteryProfile, Submission, Tag, WrongBook

# 清单硬上限：超过就截断（宁可少给信息，也不让注入量失控）
MAX_WEAK_TAGS = 5
MAX_ERROR_PATTERNS = 3
MAX_RECENT_VERDICTS = 20
# token 估算上限：中文按 1.5 字/token 粗算，用于自检注入是否超标
MEMORY_TOKEN_BUDGET = 300


@dataclass
class MemorySnapshot:
    """用户学习记忆快照：一份短清单，不是历史流水。

    字段刻意保持少而精 —— 每多一个字段，注入量就多一份，而模型的注意力
    是有限资源。这里只保留"会改变 Agent 决策"的事实。
    """

    user_id: str = ""
    # 状态：最弱/最强知识点（决定推荐难度与提示强度）
    weak_tags: list[dict[str, Any]] = field(default_factory=list)
    strong_tags: list[dict[str, Any]] = field(default_factory=list)
    # 归因：反复出错的知识点组合（决定该重点练什么）
    error_patterns: list[dict[str, Any]] = field(default_factory=list)
    # 趋势：近期判定序列与通过率（决定状态是上升还是下滑）
    recent_verdicts: list[str] = field(default_factory=list)
    recent_pass_rate: float = 0.0
    trend: str = "unknown"  # rising | falling | steady | unknown
    # 规模指标：用于向用户/前端展示"我们压缩了多少"
    total_submissions: int = 0
    source_bytes: int = 0  # 原始历史规模（字节），用于对比注入量

    def is_empty(self) -> bool:
        return not (
            self.weak_tags or self.strong_tags or self.error_patterns or self.recent_verdicts
        )

    def to_prompt_block(self) -> str:
        """产出注入 prompt 的文本块。

        格式选择说明：用紧凑的 key=value 而不是 JSON —— 同样的信息，
        JSON 的括号引号要多吃 20-30% token，而这里是每次调用都要付的成本。
        """
        if self.is_empty():
            return ""
        lines = ["【学习记忆】以下是你对该用户的既有了解，请据此个性化回答："]

        if self.weak_tags:
            items = "、".join(f"{t['tag']}({t['score']:.0f}分)" for t in self.weak_tags)
            lines.append(f"- 薄弱知识点：{items}")
        if self.strong_tags:
            items = "、".join(f"{t['tag']}({t['score']:.0f}分)" for t in self.strong_tags)
            lines.append(f"- 优势知识点：{items}")
        if self.error_patterns:
            for p in self.error_patterns:
                lines.append(f"- 易错模式：{p['label']}（{p['size']} 题，主要归因：{p['cause']}）")
        if self.recent_verdicts:
            lines.append(
                f"- 近期表现：通过率 {self.recent_pass_rate:.0%}，"
                f"趋势 {self._trend_text()}（近 {len(self.recent_verdicts)} 次提交）"
            )
        lines.append("（以上为聚合结论，非完整历史；请勿询问用户「你之前做过什么」。）")
        return "\n".join(lines)

    def _trend_text(self) -> str:
        return {
            "rising": "上升",
            "falling": "下滑",
            "steady": "持平",
            "unknown": "样本不足",
        }.get(self.trend, "未知")

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "weak_tags": self.weak_tags,
            "strong_tags": self.strong_tags,
            "error_patterns": self.error_patterns,
            "recent_verdicts": self.recent_verdicts,
            "recent_pass_rate": round(self.recent_pass_rate, 4),
            "trend": self.trend,
            "total_submissions": self.total_submissions,
            "source_bytes": self.source_bytes,
            "prompt_tokens_est": estimate_tokens(self.to_prompt_block()),
        }


def estimate_tokens(text: str) -> int:
    """与 providers._approx_tokens 同口径的粗估（中文 1.5 字/token）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def _classify_trend(verdicts: list[str]) -> str:
    """按"前半段 vs 后半段"的通过率对比判断趋势。

    为什么不用线性回归：提交数通常只有 10-20 条，拟合斜率对单次波动过于
    敏感。前后半段对比更鲁棒，且结论容易向用户解释（"你最近比之前稳了"）。

    阈值取 4 而不是更大：4 条就能分成前后各 2 条做对比。早期版本要求 6 条，
    导致"连续 5 次全 AC"这种趋势极其明确的序列被判成"样本不足"——
    全通过/全失败本身就是最清晰的趋势，不该被样本量门槛挡掉。
    """
    if len(verdicts) < 4:
        return "unknown"
    mid = len(verdicts) // 2
    # verdicts 是**倒序**（最近的在前），所以前半段是"最近"的一半
    recent_half = verdicts[:mid]
    older_half = verdicts[mid:]
    recent_rate = sum(1 for v in recent_half if v == "AC") / max(1, len(recent_half))
    older_rate = sum(1 for v in older_half if v == "AC") / max(1, len(older_half))
    delta = recent_rate - older_rate
    if delta > 0.15:
        return "rising"
    if delta < -0.15:
        return "falling"
    return "steady"


async def build_memory_snapshot(db: AsyncSession, user_id: str) -> MemorySnapshot:
    """把用户历史聚合成短清单。

    刻意不复用 services.get_mastery 等函数：那些函数返回的是**给前端渲染**
    的完整数据（含 tag_id、attempts 等前端需要但 prompt 不需要的字段）。
    记忆清单只要"结论"，多带字段就是浪费 token。这里只 SELECT 必要的列。
    """
    snap = MemorySnapshot(user_id=user_id)

    # ---- 1. 掌握度画像：最弱 5 个 + 最强 3 个 ----
    prof_rows = (
        await db.execute(
            select(Tag.name, MasteryProfile.score, MasteryProfile.attempts)
            .join(Tag, Tag.id == MasteryProfile.tag_id)
            .where(MasteryProfile.user_id == user_id)
            .order_by(MasteryProfile.score.asc())
        )
    ).all()
    if prof_rows:
        snap.weak_tags = [
            {"tag": name, "score": float(score), "attempts": int(attempts)}
            for name, score, attempts in prof_rows[:MAX_WEAK_TAGS]
        ]
        # 强项只取分数 >= 70 的，且最多 3 个 —— 强项对决策的价值低于弱项
        strong = [
            {"tag": name, "score": float(score), "attempts": int(attempts)}
            for name, score, attempts in prof_rows
            if float(score) >= 70.0
        ]
        snap.strong_tags = list(reversed(strong))[:3]

    # ---- 2. 易错模式：取严重度最高的几簇 ----
    clusters = (
        (
            await db.execute(
                select(ErrorCluster)
                .where(ErrorCluster.user_id == user_id)
                .order_by(ErrorCluster.severity.desc())
                .limit(MAX_ERROR_PATTERNS)
            )
        )
        .scalars()
        .all()
    )
    for c in clusters:
        snap.error_patterns.append(
            {
                "label": c.label or "未命名错误簇",
                "size": int(c.size),
                "cause": _dominant_cause(c.problem_ids, c.suggestion),
            }
        )

    # ---- 2b. 若没有聚类，退化为"错题本归因统计"（同样能反映错误模式）----
    if not snap.error_patterns:
        cause_rows = (
            await db.execute(
                select(WrongBook.root_cause, func.count())
                .where(WrongBook.user_id == user_id, WrongBook.mastered.is_(False))
                .group_by(WrongBook.root_cause)
                .order_by(func.count().desc())
                .limit(MAX_ERROR_PATTERNS)
            )
        ).all()
        for cause, cnt in cause_rows:
            if not cause:
                continue
            snap.error_patterns.append(
                {"label": f"{cause}类错误", "size": int(cnt), "cause": str(cause)}
            )

    # ---- 3. 近期趋势：最近 N 次判定 ----
    verdicts = (
        (
            await db.execute(
                select(Submission.verdict)
                .where(Submission.user_id == user_id)
                .order_by(Submission.created_at.desc())
                .limit(MAX_RECENT_VERDICTS)
            )
        )
        .scalars()
        .all()
    )
    snap.recent_verdicts = [str(v) for v in verdicts]
    if snap.recent_verdicts:
        ac = sum(1 for v in snap.recent_verdicts if v == "AC")
        snap.recent_pass_rate = ac / len(snap.recent_verdicts)
        snap.trend = _classify_trend(snap.recent_verdicts)

    # ---- 4. 规模指标（用于量化"压缩了多少"）----
    snap.total_submissions = int(
        (
            await db.execute(
                select(func.count()).select_from(Submission).where(Submission.user_id == user_id)
            )
        ).scalar_one()
        or 0
    )
    snap.source_bytes = _estimate_source_bytes(snap)
    return snap


def _dominant_cause(problem_ids_json: str, fallback: str) -> str:
    """从聚类里取主要归因；解析失败时回退到聚类自身的 suggestion。"""
    try:
        ids = json.loads(problem_ids_json or "[]")
    except (json.JSONDecodeError, TypeError):
        ids = []
    if not ids:
        return fallback or "未知"
    return fallback or "未知"


def _estimate_source_bytes(snap: MemorySnapshot) -> int:
    """粗估"若注入原始历史会是多少字节"，用于展示压缩比。

    按每道题平均 800 字节估算（题面 + 代码 + 判定），不是精确值 ——
    它的用途是给用户一个量级感受（"12KB 压到 200 字节"），不是计费依据。
    """
    return snap.total_submissions * 800


def memory_token_budget_ok(snap: MemorySnapshot) -> tuple[bool, int]:
    """自检：注入量是否在预算内。返回 (是否合规, 实际 token 估算)。"""
    tokens = estimate_tokens(snap.to_prompt_block())
    return tokens <= MEMORY_TOKEN_BUDGET, tokens


# --------------------------------------------------------------- 便捷入口
async def memory_block_for(db: AsyncSession, user_id: str) -> str:
    """给 Agent 用的一行入口：拿不到记忆时返回空串（注入是可选项，不能失败）。"""
    try:
        snap = await build_memory_snapshot(db, user_id)
    except Exception:  # 记忆注入失败绝不能拖垮主链路
        return ""
    return snap.to_prompt_block()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(days: int) -> datetime:
    return _now() - timedelta(days=days)
