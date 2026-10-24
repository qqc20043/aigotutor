"""相似题检索（"举一反三"）：FTS5 召回 + 标签/难度混合排序。

定位是**轻量 RAG 的检索层**：零外部服务（SQLite 内建 FTS5）、零向量库，
题库规模（几十~几千题）下这比向量检索更可控、可解释，也不破坏项目
"clone 即跑"的定位。

两路召回（互补，单路失效不影响另一路）：
  * 标签路：同标签的题（SQL 精确匹配）——"同型题"的最强信号，
    不依赖任何分词；
  * 全文路：题面/标题的 trigram 子串匹配 —— 中文没有分词器也能召回
    "提法相近"的题（trigram 是子串语义，查询词需 ≥3 字）。

排序（各项量级刻意对齐，都实际影响结果）：
    score = 2.0 × 标签 Jaccard + 0.6 × 全文命中归一分 − 0.02 × 难度差

FTS 维护：虚拟表**不进 ORM metadata 也不进 Alembic**（迁移一致性检查
比较的是 metadata/迁移两侧，应用层表会让两边不一致）。这里在每次检索前
惰性校验（行数或 max(updated_at) 漂移即全量重建）——题库规模下全量重建
是毫秒级操作，换来的是永不脏读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text

# 权重集中在规则层（可复现、可测试），与 hint/review 的排序权重同一哲学
W_TAG_JACCARD = 2.0
W_FTS_HIT = 0.6
W_DIFF_GAP = 0.02

# trigram 查询词最短长度（分词器限制，不足 3 字无法构造 trigram）
_MIN_TERM_LEN = 3


@dataclass
class SimilarItem:
    """一条相似题结果：题目摘要 + 为什么相似（可解释性）。"""

    problem_id: str
    title: str
    difficulty: str
    difficulty_score: float
    tags: list[str]
    shared_tags: list[str]
    score: float
    reason: str


def _fts_terms(source: dict[str, Any]) -> list[str]:
    """从源题构造 trigram 查询词：标题 + 标签 + 题面前 200 字的候选词。

    trigram 是子串语义：直接用标题整体（长度通常 ≥3）效果最好；题面太长，
    只取若干"信息密度高"的片段（连续汉字段 ≥4 字、英文词 ≥4 字符）。
    """
    terms: list[str] = []

    def _add(value: str) -> None:
        value = (value or "").strip()
        if len(value) >= _MIN_TERM_LEN:
            terms.append(value)

    _add(str(source.get("title", "")))
    for tag in source.get("tags") or []:
        _add(str(tag))
    statement = str(source.get("statement", ""))[:200]
    for chunk in re.findall(r"[\u4e00-\u9fff]{4,}|[A-Za-z]{4,}", statement):
        _add(chunk[:24])
    # 去重且限量（OR 查询过长会拖慢匹配）
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:12]


async def ensure_fts(db) -> dict[str, Any]:
    """确保 FTS 索引存在且与题库同步（幂等；漂移即全量重建）。

    返回维护信息（是否重建/行数），供接口的 retrieval 字段透出 ——
    检索系统的可观测性：调用方应能知道索引是否刚被重建过。
    """
    await db.execute(
        text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS problems_fts USING fts5("
            "title, statement, problem_id UNINDEXED, updated_at UNINDEXED, "
            "tokenize='trigram')"
        )
    )
    drift = (
        await db.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM problems), "
                "(SELECT COUNT(*) FROM problems_fts), "
                "(SELECT COALESCE(MAX(updated_at), 0) FROM problems_fts)"
            )
        )
    ).fetchone()
    n_problems, n_fts, fts_max_updated = drift
    stale = n_problems != n_fts
    if not stale and n_problems:
        newer = (
            await db.execute(
                text("SELECT COUNT(*) FROM problems WHERE updated_at > :m"),
                {"m": fts_max_updated},
            )
        ).scalar_one()
        stale = bool(newer)

    rebuilt = False
    if stale and n_problems:
        await db.execute(text("DELETE FROM problems_fts"))
        await db.execute(
            text(
                "INSERT INTO problems_fts(title, statement, problem_id, updated_at) "
                "SELECT title, COALESCE(statement, ''), id, updated_at FROM problems"
            )
        )
        rebuilt = True
    return {"rebuilt": rebuilt, "rows": n_problems}


async def _recall_by_tags(db, source_id: str, tag_names: list[str]) -> dict[str, float]:
    """标签路召回：返回 problem_id -> 重叠标签数（同源题自身已排除）。"""
    if not tag_names:
        return {}
    rows = (
        await db.execute(
            text(
                "SELECT pt.problem_id, COUNT(*) AS overlap "
                "FROM problem_tags pt JOIN tags t ON pt.tag_id = t.id "
                "WHERE t.name IN :names AND pt.problem_id != :src "
                "GROUP BY pt.problem_id"
            ).bindparams(bindparam("names", expanding=True)),
            {"names": tag_names, "src": source_id},
        )
    ).fetchall()
    return {row[0]: float(row[1]) for row in rows}


async def _recall_by_fts(db, source: dict[str, Any]) -> dict[str, float]:
    """全文路召回：problem_id -> bm25 归一命中分（0~1，越小越相关→翻转）。"""
    terms = _fts_terms(source)
    if not terms:
        return {}
    match_expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT problem_id, bm25(problems_fts) AS rank "
                    "FROM problems_fts WHERE problems_fts MATCH :q LIMIT 50"
                ),
                {"q": match_expr},
            )
        ).fetchall()
    except Exception:
        # 查询串仍可能触发分词器边界（如纯符号）：全文路失效时静默退化为标签路
        return {}
    if not rows:
        return {}
    ranks = {row[0]: float(row[1]) for row in rows}
    # bm25 越负越相关；归一到 0~1（最相关=1）
    lo, hi = min(ranks.values()), max(ranks.values())
    span = hi - lo
    if span <= 0:
        return dict.fromkeys(ranks, 1.0)
    return {pid: (hi - r) / span for pid, r in ranks.items()}


async def find_similar(
    db, source: dict[str, Any], *, limit: int = 5, exclude_ids: list[str] | None = None
) -> dict[str, Any]:
    """检索同型题。source 需含 id/title/statement/tags/difficulty_score。

    返回 {"items": [SimilarItem...], "retrieval": {...}}；无候选时 items 为空
    （诚实返回，不硬凑）。
    """
    maintenance = await ensure_fts(db)
    tag_names = [str(t) for t in source.get("tags") or []]
    by_tags = await _recall_by_tags(db, str(source["id"]), tag_names)
    by_fts = await _recall_by_fts(db, source)
    exclude = {str(source["id"]), *(exclude_ids or [])}

    candidate_ids = (set(by_tags) | set(by_fts)) - exclude
    if not candidate_ids:
        return {"items": [], "retrieval": {**maintenance, "candidates": 0}}

    rows = (
        await db.execute(
            text(
                "SELECT p.id, p.title, p.difficulty, p.difficulty_score "
                "FROM problems p WHERE p.id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": sorted(candidate_ids)},
        )
    ).fetchall()
    tag_sets = await _tags_of(db, candidate_ids)

    src_score = float(source.get("difficulty_score") or 0.0)
    src_tags = set(tag_names)
    scored: list[SimilarItem] = []
    for pid, title, difficulty, diff_score in rows:
        tags = tag_sets.get(pid, [])
        shared = sorted(src_tags & set(tags))
        overlap = by_tags.get(pid, 0.0)
        union = len(src_tags | set(tags)) or 1
        jaccard = overlap / union
        fts_hit = by_fts.get(pid, 0.0)
        gap = abs(float(diff_score or 0.0) - src_score)
        score = W_TAG_JACCARD * jaccard + W_FTS_HIT * fts_hit - W_DIFF_GAP * min(gap, 100.0) / 10.0
        bits = []
        if shared:
            bits.append(f"同标签 {'、'.join(shared[:3])}")
        if fts_hit > 0:
            bits.append("题面相近")
        bits.append(f"难度 {float(diff_score or 0.0):.0f}")
        scored.append(
            SimilarItem(
                problem_id=pid,
                title=title,
                difficulty=difficulty,
                difficulty_score=float(diff_score or 0.0),
                tags=tags,
                shared_tags=shared,
                score=round(score, 4),
                reason="；".join(bits),
            )
        )
    scored.sort(key=lambda x: x.score, reverse=True)
    return {
        "items": scored[:limit],
        "retrieval": {**maintenance, "candidates": len(candidate_ids)},
    }


async def _tags_of(db, problem_ids: set[str]) -> dict[str, list[str]]:
    """批量取候选题的标签（一次查询，避免 N+1）。"""
    if not problem_ids:
        return {}
    rows = (
        await db.execute(
            text(
                "SELECT pt.problem_id, t.name FROM problem_tags pt "
                "JOIN tags t ON pt.tag_id = t.id "
                "WHERE pt.problem_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": sorted(problem_ids)},
        )
    ).fetchall()
    out: dict[str, list[str]] = {}
    for pid, name in rows:
        out.setdefault(pid, []).append(str(name))
    return out
