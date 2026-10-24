"""智能练习集：顽固错题雷达（leech）+ 混排练习生成（interleaving）。

对标案例（2026-10-24 调研）：
  * Anki 的 leech 机制：忘记次数达到阈值的卡片打标，用筛选牌组专项处理
    （重写/拆分/换提示方式），而不是让它继续占用每日队列；
  * Anki 的 Custom Study / 筛选牌组：按条件临时抽卡组成专项练习；
  * 学习科学中的 interleaving（交错练习）：相邻题目来自不同知识点，
    迫使大脑主动区分和检索，是"合意困难"（desirable difficulties）的
    典型应用 —— 比"同类题连刷"的集中练习留存更好。

本模块的三项能力（零新表：识别是查询，混排是合成，干预按需生成）：
  1. find_leeches：顽固错题识别 —— FSRS lapses ≥ 阈值 且 掌握度低于
     干预线（规则可解释），每条带"为什么是顽固卡"的理由；
  2. build_interleaved：混排练习 —— 从 FSRS 到期 / 顽固错题 / 推荐候选
     三路采题，按知识点轮转交错（相邻不同 tag），每题带来源与入选理由；
  3. intervention_plan：顽固错题的 AI 干预方案（为什么反复卡 + 拆解成
     子问题 + 先修建议），LLM 只做诊断表达，输入事实全部来自规则层，
     失败降级为规则模板。
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from sqlalchemy import bindparam, text

LEECH_LAPSES_THRESHOLD = 3
LEECH_MASTERY_BELOW = 40.0
LEECH_WRONG_COUNT = 3


# --------------------------------------------------------------- 顽固错题雷达
async def find_leeches(
    db,
    user_id: str,
    *,
    limit: int = 20,
    tags_filter: list[str] | None = None,
) -> dict[str, Any]:
    """识别顽固错题：lapses ≥ 3 且最弱掌握度 < 40，或错题本连错 ≥ 3 次。

    返回 items（按"顽固程度"排序：lapses 与掌握度缺口加权）+ 阈值说明
    （前端可展示"为什么它们被标记"）。items 带 suspended 状态（Anki
    搁置语义：搁置卡移出每日队列，但仍在雷达可见、可恢复）。
    tags_filter：只保留命中任一知识点的题（Custom Study 的自定义条件）。
    """
    queue_rows = (
        await db.execute(
            text(
                "SELECT rq.problem_id, p.title, p.difficulty, rq.lapses, "
                "rq.reps, rq.retrievability, rq.suspended "
                "FROM review_queue rq JOIN problems p ON rq.problem_id = p.id "
                "WHERE rq.user_id = :uid AND rq.lapses >= :lt "
                "ORDER BY rq.lapses DESC, rq.due_at ASC LIMIT :lim"
            ),
            {"uid": user_id, "lt": LEECH_LAPSES_THRESHOLD, "lim": limit},
        )
    ).fetchall()

    wrong_rows = (
        await db.execute(
            text(
                "SELECT wb.problem_id, p.title, p.difficulty, wb.wrong_count "
                "FROM wrong_book wb JOIN problems p ON wb.problem_id = p.id "
                "WHERE wb.user_id = :uid AND wb.mastered = 0 AND wb.wrong_count >= :wc "
                "ORDER BY wb.wrong_count DESC LIMIT :lim"
            ),
            {"uid": user_id, "wc": LEECH_WRONG_COUNT, "lim": limit},
        )
    ).fetchall()

    # 合并两路信号：同一题取更"顽固"的表述
    merged: dict[str, dict[str, Any]] = {}
    for pid, title, difficulty, lapses, reps, retrievability, suspended in queue_rows:
        merged[pid] = {
            "problem_id": pid,
            "title": title,
            "difficulty": difficulty,
            "lapses": int(lapses or 0),
            "reps": int(reps or 0),
            "retrievability": float(retrievability or 0.0),
            "wrong_count": 0,
            "suspended": bool(suspended),
        }
    for pid, title, difficulty, wrong_count in wrong_rows:
        item = merged.setdefault(
            pid,
            {
                "problem_id": pid,
                "title": title,
                "difficulty": difficulty,
                "lapses": 0,
                "reps": 0,
                "retrievability": None,
                "wrong_count": 0,
                "suspended": False,
            },
        )
        item["wrong_count"] = max(item["wrong_count"], int(wrong_count or 0))

    if not merged:
        return {"items": [], "thresholds": _thresholds(), "note": "当前没有顽固错题——保持节奏"}

    from ..services import get_problem_tags, mastery_scores_map

    pids = list(merged)
    # 搁置状态统一从 review_queue 真值回填：wrong-book 路进来的条目
    # （只连错、lapses 未达标的顽固卡）不该显示默认的"调度中"——
    # 它可能已被搁置（suspend API 会为无队列记录的顽固卡补建条目）。
    suspended_map: dict[str, bool] = {}
    sus_rows = (
        await db.execute(
            text(
                "SELECT problem_id, suspended FROM review_queue "
                "WHERE user_id = :uid AND problem_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"uid": user_id, "ids": pids},
        )
    ).fetchall()
    for spid, sflag in sus_rows:
        suspended_map[spid] = bool(sflag)
    tag_map = await get_problem_tags(db, pids)
    mastery = await mastery_scores_map(db, user_id)

    items: list[dict[str, Any]] = []
    for pid in pids:
        item = merged[pid]
        item["suspended"] = suspended_map.get(pid, False)
        tags = [t["name"] for t in tag_map.get(pid, [])]
        weakest = min((float(mastery.get(t, 50.0)) for t in tags), default=50.0)
        item["tags"] = tags
        item["weakest_mastery"] = round(weakest, 1)
        if tags_filter and not (set(tags) & set(tags_filter)):
            continue
        item["is_leech"] = bool(
            item["lapses"] >= LEECH_LAPSES_THRESHOLD or item["wrong_count"] >= LEECH_WRONG_COUNT
        )
        bits = []
        if item["lapses"]:
            bits.append(f"已遗忘 {item['lapses']} 次")
        if item["wrong_count"]:
            bits.append(f"连错 {item['wrong_count']} 次")
        bits.append(f"最弱掌握度 {weakest:.0f}")
        item["reason"] = "；".join(bits)
        # 顽固程度排序：遗忘多 + 掌握度缺口大优先
        item["urgency"] = round(item["lapses"] * 3 + (100.0 - weakest) * 0.5, 2)
        items.append(item)

    items.sort(key=lambda x: x["urgency"], reverse=True)
    return {"items": items[:limit], "thresholds": _thresholds()}


def _thresholds() -> dict[str, Any]:
    return {
        "lapses": LEECH_LAPSES_THRESHOLD,
        "wrong_count": LEECH_WRONG_COUNT,
        "mastery_below": LEECH_MASTERY_BELOW,
        "note": "遗忘/连错达阈值即标记；掌握度低于干预线时建议生成 AI 干预方案",
    }


# --------------------------------------------------------------- 混排练习
async def build_interleaved(
    db,
    user_id: str,
    size: int,
    *,
    include_review: bool = True,
    include_leech: bool = True,
    include_fresh: bool = True,
    tags_filter: list[str] | None = None,
) -> dict[str, Any]:
    """生成混排练习集：三路采题 -> 按知识点轮转交错 -> 可解释的顺序。

    交错算法（贪心轮转）：把候选按"主知识点"分桶，每轮从不同桶各取一题；
    相邻两题主知识点不同即满足交错语义。某桶耗尽时允许跳过 —— 宁可少
    交错也不硬凑。
    tags_filter（Custom Study 自定义条件）：只保留命中任一知识点的候选。
    搁置语义：review 来源由 get_review_queue 默认排除搁置卡 —— 被搁置的
    顽固卡已移出每日队列，不再自动进混排。
    """
    from ..services import (
        get_problem_tags,
        get_review_queue,
        list_problems,
        mastery_scores_map,
    )

    size = max(1, min(size, 20))
    candidates: list[dict[str, Any]] = []

    if include_review:
        due = await get_review_queue(db, user_id, due_only=True, limit=15)
        for i in due:
            candidates.append(
                {
                    "problem_id": i["problem_id"],
                    "title": i["title"],
                    "difficulty": i["difficulty"],
                    "source": "review",
                    "reason": f"FSRS 到期（逾期 {i['overdue_days']} 天，保留率 {i['retrievability']:.0%}）",
                    "lapses": i["lapses"],
                }
            )

    if include_leech:
        leech = await find_leeches(db, user_id, limit=10)
        for i in leech["items"]:
            if not i["is_leech"]:
                continue
            if i.get("suspended"):
                continue  # 搁置的顽固卡已移出日常练习（雷达可见、手动恢复）
            if any(c["problem_id"] == i["problem_id"] for c in candidates):
                continue
            candidates.append(
                {
                    "problem_id": i["problem_id"],
                    "title": i["title"],
                    "difficulty": i["difficulty"],
                    "source": "leech",
                    "reason": f"顽固错题专项（{i['reason']}）",
                    "lapses": i["lapses"],
                }
            )

    if include_fresh and len(candidates) < size:
        rows, _total = await list_problems(db, offset=0, limit=30)
        mastery = await mastery_scores_map(db, user_id)
        have = {c["problem_id"] for c in candidates}
        # "新题"语义 = 没提交过的题：搁置卡与一切已有复习记录的题都不算新。
        # 修复前 fresh 路只排除当前 candidates —— 搁置卡会从这条路漏回混排
        # （测试实测抓到），已做过的题也会被当成"新题"重复推。
        full_queue = await get_review_queue(db, user_id, include_suspended=True, limit=500)
        seen = {i["problem_id"] for i in full_queue}
        tag_map_all = await get_problem_tags(db, [p.id for p in rows])
        fresh: list[tuple[float, Any]] = []
        for p in rows:
            if p.id in have or p.id in seen:
                continue
            tags = [t["name"] for t in tag_map_all.get(p.id, [])]
            weakest = min((float(mastery.get(t, 50.0)) for t in tags), default=50.0)
            # 缺口大（薄弱知识点的新题）优先，其次由易到难
            fresh.append((weakest, -float(p.difficulty_score or 0.0), p, tags))
        fresh.sort(key=lambda x: (x[0], x[1]))
        for _w, _d, p, tags in fresh[: size * 2]:
            if len(candidates) >= size * 2:
                break
            candidates.append(
                {
                    "problem_id": p.id,
                    "title": p.title,
                    "difficulty": p.difficulty,
                    "source": "fresh",
                    "reason": "新题：薄弱知识点优先、由易到难",
                    "lapses": 0,
                    "tags": tags,
                }
            )

    if not candidates:
        return {"items": [], "note": "没有可用的练习候选（队列空且题库为空）"}

    # 主知识点分桶 -> 轮转交错
    tag_map = await get_problem_tags(db, [c["problem_id"] for c in candidates])
    for c in candidates:
        c.setdefault("tags", [t["name"] for t in tag_map.get(c["problem_id"], [])])
        c["primary_tag"] = c["tags"][0] if c["tags"] else "未标注"

    # Custom Study 自定义条件：按知识点筛选（在交错前过滤，宁缺毋滥）
    if tags_filter:
        wanted = {t.strip() for t in tags_filter if t.strip()}
        candidates = [c for c in candidates if set(c["tags"]) & wanted]
        if not candidates:
            return {"items": [], "note": f"筛选条件下没有可用候选（tags={sorted(wanted)}）"}

    buckets: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        buckets.setdefault(c["primary_tag"], []).append(c)
    # 桶内排序：leech/review 优先于 fresh（先巩固再扩展）
    for b in buckets.values():
        b.sort(key=lambda c: 0 if c["source"] in ("leech", "review") else 1)

    ordered: list[dict[str, Any]] = []
    last_tag: str | None = None
    while len(ordered) < size and any(buckets.values()):
        # 从"非上一个知识点"的桶里挑剩余最多的（轮转保交错）
        pick_bucket = None
        for tag, b in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            if tag == last_tag or not b:
                continue
            pick_bucket = tag
            break
        if pick_bucket is None:  # 只剩同知识点：接受相邻重复
            pick_bucket = next(t for t, b in buckets.items() if b)
        item = buckets[pick_bucket].pop(0)
        item["order"] = len(ordered) + 1
        ordered.append(item)
        last_tag = pick_bucket

    interleaved = sum(
        1 for a, b in itertools.pairwise(ordered) if a["primary_tag"] != b["primary_tag"]
    )
    return {
        "items": ordered,
        "stats": {
            "size": len(ordered),
            "interleaved_pairs": interleaved,
            "sources": {
                src: sum(1 for i in ordered if i["source"] == src)
                for src in ("review", "leech", "fresh")
            },
        },
    }


# --------------------------------------------------------------- AI 干预方案
def rule_intervention(item: dict[str, Any]) -> dict[str, Any]:
    """顽固错题的规则模板干预：不假装知道题目细节，给可执行的通用动作。"""
    tags = item.get("tags") or []
    tag_text = "、".join(tags[:3]) if tags else "本题知识点"
    return {
        "diagnosis": (
            f"规则引擎诊断：该题已遗忘/连错多次（{item.get('reason', '')})，"
            f"说明「{tag_text}」的解法模式没有真正建立，继续同强度重做收益有限。"
        ),
        "sub_problems": [
            f"先不写代码：把「{tag_text}」这道题的输入规模与暴力解法复杂度写出来",
            "手算一个最小样例，逐步记录每一步的状态变化",
            "对照提示档里的关键观察，确认自己缺的是方向还是实现",
        ],
        "prerequisite_advice": f"若手算仍卡住，先做 1-2 道「{tag_text}」的入门题重建模式",
        "next_action": "带着手算过程回来重做本题；再次 AC 后立即标记复习（rating=4）",
        "degraded": True,
    }


async def intervention_plan(
    config: dict[str, Any], item: dict[str, Any], history_summary: str
) -> dict[str, Any]:
    """顽固错题的 AI 干预方案：输入全来自规则层，LLM 只做诊断表达。

    与 hint 的区别：hint 服务"这道题怎么往下想"；干预方案服务
    "为什么这道题反复卡、策略要怎么变"。
    """
    if str(config.get("provider", "mock")) == "mock":
        plan = rule_intervention(item)
        plan["degraded"] = False  # mock = 规则路径，不算降级
        return plan

    from .providers import LLMError, build_provider

    provider = build_provider(
        str(config.get("provider", "")),
        str(config.get("base_url", "")),
        str(config.get("api_key", "")),
    )
    prompt = json.dumps(
        {
            "role": "你是学习诊断师。学生有一道反复失败的算法题，请输出针对性干预方案。",
            "problem": {
                "title": item.get("title", ""),
                "tags": item.get("tags", []),
                "difficulty": item.get("difficulty", ""),
            },
            "facts": {
                "lapses": item.get("lapses", 0),
                "wrong_count": item.get("wrong_count", 0),
                "weakest_mastery": item.get("weakest_mastery"),
                "history": history_summary[:600],
            },
            "output_schema": {
                "diagnosis": "为什么反复卡（一句话，引用事实数字）",
                "sub_problems": ["拆解出的 2-3 个子问题/子步骤"],
                "prerequisite_advice": "先修补什么基础",
                "next_action": "现在立刻做的一步",
            },
        },
        ensure_ascii=False,
    )
    messages = [
        {
            "role": "system",
            "content": "你是学习诊断师，只依据给定事实输出 JSON 干预方案，用中文。只输出 JSON。",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        result = await provider.complete_chat(
            messages,
            str(config.get("model", "")),
            temperature=float(config.get("temperature", 0.3)),
        )
        data = json.loads(result.text[result.text.find("{") : result.text.rfind("}") + 1])
        plan = {
            "diagnosis": str(data.get("diagnosis", ""))[:300],
            "sub_problems": [str(x)[:200] for x in (data.get("sub_problems") or [])][:4],
            "prerequisite_advice": str(data.get("prerequisite_advice", ""))[:300],
            "next_action": str(data.get("next_action", ""))[:200],
        }
        if not plan["diagnosis"]:
            raise ValueError("空诊断")
        plan["degraded"] = False
        return plan
    except (TimeoutError, LLMError, ValueError, json.JSONDecodeError, Exception):
        return rule_intervention(item)
