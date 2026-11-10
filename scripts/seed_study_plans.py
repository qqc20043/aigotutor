"""学习计划种子：6 个计划（对标 LeetCode Study Plan），题目按标题挂载。

用法：python -m scripts.seed_study_plans   # 增量：按计划标题跳过已存在
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (title, subtitle, category, theme, sort, [problem titles...])
PLANS: list[tuple[str, str, str, str, int, list[str]]] = [
    (
        "编程基础 0 → 1",
        "12 题打通语言与基础算法：数组、哈希、排序、二分、前缀和",
        "精选",
        "green",
        10,
        [
            "爬楼梯",
            "最大公约数与最小公倍数",
            "成绩排序",
            "两数之和",
            "约瑟夫环",
            "二分查找",
            "质数筛",
            "区间合并计数",
            "子段和查询",
            "无重复字符的最长子串",
            "合法括号序列计数",
            "岛屿数量",
        ],
    ),
    (
        "动态规划专项",
        "6 题吃透 DP 五类模型：线性、双序列、编辑、背包、进阶背包",
        "精选",
        "blue",
        20,
        [
            "爬楼梯",
            "最长上升子序列",
            "最长公共子序列",
            "编辑距离",
            "零一背包",
            "零一背包（进阶）",
        ],
    ),
    (
        "图论与搜索",
        "BFS / 0-1 BFS / DFS 连通块 / 拓扑排序 / 回溯剪枝四板斧",
        "精选",
        "purple",
        30,
        ["01 网格最短路", "岛屿数量", "N 皇后计数", "拓扑排序"],
    ),
    (
        "数据结构精练",
        "二分、前缀和、线段树、区间合并——静态查询的四种武器",
        "精选",
        "orange",
        40,
        ["二分查找", "子段和查询", "区间合并计数", "区间和查询"],
    ),
    (
        "面试冲刺 15 题",
        "高频题型一网打尽：哈希、滑窗、括号、DP、图论、模拟全覆盖",
        "面试准备",
        "pink",
        50,
        [
            "两数之和",
            "无重复字符的最长子串",
            "合法括号序列计数",
            "最长上升子序列",
            "编辑距离",
            "零一背包",
            "拓扑排序",
            "岛屿数量",
            "区间合并计数",
            "二分查找",
            "质数筛",
            "爬楼梯",
            "01 网格最短路",
            "子段和查询",
            "约瑟夫环",
        ],
    ),
    (
        "数学与构造",
        "数论、计数与模拟：GCD、筛法、Catalan、约瑟夫环",
        "基础",
        "teal",
        60,
        ["最大公约数与最小公倍数", "约瑟夫环", "合法括号序列计数", "质数筛"],
    ),
]


async def seed_plans() -> dict:
    from sqlalchemy import select as sa_select

    from app.db import init_db, session_scope
    from app.models import Problem, StudyPlan, StudyPlanItem

    await init_db()
    created, skipped, missing_titles = 0, 0, []
    async with session_scope() as db:
        existing = {t for (t,) in (await db.execute(sa_select(StudyPlan.title))).all()}
        title_to_pid: dict[str, str] = {
            t: pid for pid, t in (await db.execute(sa_select(Problem.id, Problem.title))).all()
        }
        for title, subtitle, category, theme, sort_order, problem_titles in PLANS:
            if title in existing:
                skipped += 1
                continue
            plan = StudyPlan(
                title=title,
                subtitle=subtitle,
                category=category,
                theme=theme,
                sort_order=sort_order,
            )
            db.add(plan)
            await db.flush()
            order = 0
            for ptitle in problem_titles:
                pid = title_to_pid.get(ptitle)
                if pid is None:
                    missing_titles.append(f"{title}→{ptitle}")
                    continue
                db.add(StudyPlanItem(plan_id=plan.id, problem_id=pid, order_no=order))
                order += 1
            created += 1
    return {"created": created, "skipped": skipped, "missing": missing_titles}


async def main() -> int:
    result = await seed_plans()
    print(f"创建计划 {result['created']} 个，跳过 {result['skipped']} 个（已存在）")
    for m in result["missing"]:
        print(f"  ⚠ 缺题（未导入？）: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
