"""种子数据：标签树 + 12 道题 + 演示用户。

用法：
    python -m scripts.seed          # 幂等，已存在则跳过
    python -m scripts.seed --reset  # 清库重建
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

from app.db import init_db, reset_db, session_scope
from app.models import Problem, ReviewQueue, Tag, User
from app.security import hash_password
from app.services import create_problem, create_submission

# (标签名, 先修标签名列表, 算法深度)
TAGS: list[tuple[str, list[str], int]] = [
    ("模拟", [], 1),
    ("枚举", [], 1),
    ("排序", [], 2),
    ("前缀和", ["枚举"], 2),
    ("二分", ["排序"], 3),
    ("贪心", ["排序"], 3),
    ("双指针", ["排序"], 3),
    ("搜索", ["模拟"], 3),
    ("动态规划", ["枚举", "贪心"], 4),
    ("图论", ["搜索"], 4),
    ("并查集", ["图论"], 4),
    ("线段树", ["二分", "前缀和"], 5),
]

PROBLEMS: list[dict] = [
    {
        "title": "两数之和",
        "statement": "给定 n 个整数与目标值 t，找出和为 t 的两个数的下标（保证恰有一组解）。",
        "input_desc": "第一行 n t；第二行 n 个整数。",
        "output_desc": "两个下标，空格分隔（从 0 开始）。",
        "samples": [{"input": "4 9\n2 7 11 15", "output": "0 1"}],
        "constraints": "2 <= n <= 10^5",
        "tags": ["枚举", "模拟"],
        "difficulty_score": 20.0,
        "statement_len": 40,
        "algo_depth": 1,
    },
    {
        "title": "区间合并",
        "statement": "给定 n 个闭区间，合并所有重叠的区间，输出合并后的区间列表。",
        "input_desc": "第一行 n；接下来 n 行每行两个整数 l r。",
        "output_desc": "每行一个合并后的区间。",
        "samples": [{"input": "4\n1 3\n2 6\n8 10\n15 18", "output": "1 6\n8 10\n15 18"}],
        "constraints": "1 <= n <= 10^5, -10^9 <= l <= r <= 10^9",
        "tags": ["排序", "贪心"],
        "difficulty_score": 42.0,
        "statement_len": 60,
        "algo_depth": 3,
    },
    {
        "title": "静态区间和",
        "statement": "给定数组，多次询问区间 [l, r] 的元素和。",
        "input_desc": "第一行 n q；第二行 n 个整数；接下来 q 行每行 l r。",
        "output_desc": "每行一个区间和。",
        "samples": [{"input": "5 3\n1 2 3 4 5\n1 3\n2 5\n1 1", "output": "6\n14\n1"}],
        "constraints": "1 <= n, q <= 10^5",
        "tags": ["前缀和"],
        "difficulty_score": 32.0,
        "statement_len": 40,
        "algo_depth": 2,
    },
    {
        "title": "数的三次方根",
        "statement": "给定浮点数 n，求其三次方根，保留 6 位小数。",
        "input_desc": "一个浮点数 n。",
        "output_desc": "一个浮点数，保留 6 位小数。",
        "samples": [{"input": "27.0", "output": "3.000000"}],
        "constraints": "-10^4 <= n <= 10^4",
        "tags": ["二分"],
        "difficulty_score": 45.0,
        "statement_len": 45,
        "algo_depth": 3,
    },
    {
        "title": "活动安排",
        "statement": "给定 n 个活动的开始与结束时间，求最多能参加多少个不冲突的活动。",
        "input_desc": "第一行 n；接下来 n 行每行 s e。",
        "output_desc": "一个整数，最多活动数。",
        "samples": [{"input": "3\n1 3\n2 4\n3 5", "output": "2"}],
        "constraints": "1 <= n <= 10^5",
        "tags": ["贪心", "排序"],
        "difficulty_score": 48.0,
        "statement_len": 55,
        "algo_depth": 3,
    },
    {
        "title": "最长无重复子串",
        "statement": "给定字符串，求不含重复字符的最长子串长度。",
        "input_desc": "一行字符串 s。",
        "output_desc": "一个整数。",
        "samples": [{"input": "abcabcbb", "output": "3"}],
        "constraints": "0 <= |s| <= 10^5",
        "tags": ["双指针"],
        "difficulty_score": 50.0,
        "statement_len": 40,
        "algo_depth": 3,
    },
    {
        "title": "迷宫最短路",
        "statement": "给定 n×m 的 0/1 网格，0 可走，求从左上到右下的最短步数，不可达输出 -1。",
        "input_desc": "第一行 n m；接下来 n 行每行 m 个字符。",
        "output_desc": "一个整数。",
        "samples": [{"input": "3 3\n000\n110\n000", "output": "4"}],
        "constraints": "1 <= n, m <= 1000",
        "tags": ["搜索", "图论"],
        "difficulty_score": 58.0,
        "statement_len": 60,
        "algo_depth": 4,
    },
    {
        "title": "01 背包",
        "statement": "给定 n 件物品的体积与价值以及背包容量 V，求最大总价值。",
        "input_desc": "第一行 n V；接下来 n 行每行 v w。",
        "output_desc": "一个整数。",
        "samples": [{"input": "3 5\n1 2\n2 4\n3 4", "output": "8"}],
        "constraints": "1 <= n <= 1000, 1 <= V <= 10^4",
        "tags": ["动态规划"],
        "difficulty_score": 62.0,
        "statement_len": 50,
        "algo_depth": 4,
    },
    {
        "title": "最长上升子序列",
        "statement": "给定序列，求最长严格上升子序列的长度。",
        "input_desc": "第一行 n；第二行 n 个整数。",
        "output_desc": "一个整数。",
        "samples": [{"input": "6\n1 7 3 5 9 4", "output": "4"}],
        "constraints": "1 <= n <= 10^5",
        "tags": ["动态规划", "二分"],
        "difficulty_score": 68.0,
        "statement_len": 40,
        "algo_depth": 4,
    },
    {
        "title": "连通块计数",
        "statement": "给定无向图，统计连通块个数。",
        "input_desc": "第一行 n m；接下来 m 行每行 u v。",
        "output_desc": "一个整数。",
        "samples": [{"input": "5 3\n1 2\n2 3\n4 5", "output": "2"}],
        "constraints": "1 <= n, m <= 2*10^5",
        "tags": ["并查集", "图论"],
        "difficulty_score": 70.0,
        "statement_len": 35,
        "algo_depth": 4,
    },
    {
        "title": "区间加区间和",
        "statement": "给定数组，支持区间加与区间求和两种操作。",
        "input_desc": "第一行 n q；第二行 n 个整数；接下来 q 行操作。",
        "output_desc": "每个查询一行答案。",
        "samples": [{"input": "5 3\n1 2 3 4 5\n1 1 3 2\n2 1 5\n2 2 3", "output": "25\n9"}],
        "constraints": "1 <= n, q <= 2*10^5",
        "tags": ["线段树", "前缀和"],
        "difficulty_score": 82.0,
        "statement_len": 45,
        "algo_depth": 5,
    },
    {
        "title": "构造合法括号序列",
        "statement": "给定 n，构造一个长度为 2n 的合法括号序列，要求字典序最小。",
        "input_desc": "一个整数 n。",
        "output_desc": "一个字符串。",
        "samples": [{"input": "3", "output": "((()))"}],
        "constraints": "1 <= n <= 10^5",
        "tags": ["构造", "贪心"],
        "difficulty_score": 66.0,
        "statement_len": 50,
        "algo_depth": 3,
    },
]


async def seed(reset: bool = False) -> dict:
    """幂等灌入种子数据。

    幂等性不靠内存锁（锁绑定事件循环，跨 loop 会失效，测试里已踩到），
    而是靠两件事：
      1. 每一步写入前先查存在性；
      2. 数据库唯一约束兜底（users.username / tags.name / problems.title）。
    即使并发调用出现竞争，唯一约束也会拒绝重复插入，而不是产生重复行。
    """
    if reset:
        await reset_db()
    else:
        await init_db()

    async with session_scope() as db:
        # --- 用户 ---
        from sqlalchemy import select

        existing = (await db.execute(select(User).where(User.username == "demo"))).scalars().first()
        if existing is None:
            demo = User(
                username="demo",
                password_hash=hash_password("demo123456"),
                nickname="演示用户",
                level=3,
                daily_goal=3,
                # 演示账号直接给管理员角色：否则新库首个用户是 demo 时，
                # 它会因为"首个用户自动成为管理员"以外的路径而拿到 user 角色，
                # 导致管理后台无法演示（踩过这个坑）。
                role="admin",
                timezone="Asia/Shanghai",
            )
            db.add(demo)
            await db.flush()
            user_id = demo.id
        else:
            user_id = existing.id
            # 已有库升级场景：demo 若还是 user，补成 admin（幂等）
            if existing.role != "admin":
                existing.role = "admin"
                await db.flush()

        # --- 标签（带先修关系） ---
        tag_ids: dict[str, str] = {}
        for name, _prereqs, _depth in TAGS:
            tag = (await db.execute(select(Tag).where(Tag.name == name))).scalars().first()
            if tag is None:
                tag = Tag(name=name, category="algorithm")
                db.add(tag)
                await db.flush()
            tag_ids[name] = tag.id
        # 回填 prereq_ids
        for name, prereqs, _depth in TAGS:
            tag = await db.get(Tag, tag_ids[name])
            tag.prereq_ids = json.dumps(
                [tag_ids[p] for p in prereqs if p in tag_ids], ensure_ascii=False
            )
        await db.flush()

        # --- 题目 ---
        created = 0
        problem_ids: list[str] = []
        for item in PROBLEMS:
            title = item["title"]
            exists = (
                (await db.execute(select(Problem).where(Problem.title == title))).scalars().first()
            )
            if exists is not None:
                problem_ids.append(exists.id)
                continue
            data = dict(item)
            data["source"] = "seed"
            data["difficulty_source"] = "human"
            data["difficulty"] = _difficulty_of(float(item.get("difficulty_score", 0.0)))
            problem = await create_problem(db, data, tag_names=item["tags"])
            problem_ids.append(problem.id)
            created += 1

        # --- 演示提交记录（造出有意义的掌握度画像） ---
        from app.services import recent_submissions

        subs = await recent_submissions(db, user_id, limit=1)
        made_subs = 0
        if not subs:
            rng = random.Random(42)
            now = datetime.now(timezone.utc)
            for i, pid in enumerate(problem_ids):
                # 前 4 题表现好，中间波动，后 3 题表现差 -> 画像有梯度
                if i < 4:
                    verdicts = ["AC", "AC", "WA", "AC"]
                elif i < 8:
                    verdicts = ["AC", "WA", "TLE", "AC"]
                else:
                    verdicts = ["WA", "TLE", "WA", "RE"]
                for j, v in enumerate(verdicts):
                    await create_submission(
                        db,
                        {
                            "user_id": user_id,
                            "problem_id": pid,
                            "language": "python",
                            "code": "print(0)\n",
                            "verdict": v,
                            "passed_cases": 3 if v == "AC" else 1,
                            "total_cases": 4,
                            "runtime_ms": rng.randint(20, 900),
                            "judge_detail": {"case": j, "expected": "1", "got": "0"}
                            if v != "AC"
                            else {},
                        },
                    )
                    made_subs += 1
                    # 手动回填时间，制造时间衰减差异
                    from app.models import Submission as Sub

                    row = (
                        await db.execute(
                            select(Sub)
                            .where(Sub.user_id == user_id)
                            .order_by(Sub.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one()
                    row.created_at = now - timedelta(days=(len(problem_ids) - i) * 1.5 + j * 0.2)
            await db.flush()

        # --- 复习队列 ---
        for i, pid in enumerate(problem_ids[:5]):
            exists = (
                await db.execute(
                    select(ReviewQueue).where(
                        ReviewQueue.user_id == user_id, ReviewQueue.problem_id == pid
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            db.add(
                ReviewQueue(
                    user_id=user_id,
                    problem_id=pid,
                    stability=float(1 + i),
                    difficulty_fsrs=5.0,
                    interval_days=float(1 + i),
                    reps=1,
                    due_at=datetime.now(timezone.utc) - timedelta(days=(5 - i)),
                )
            )
        await db.flush()

        return {
            "user_id": user_id,
            "username": "demo",
            "password": "demo123456",
            "tags": len(TAGS),
            "problems_created": created,
            "problems_total": len(problem_ids),
            "submissions_created": made_subs,
        }


def _difficulty_of(score: float) -> str:
    if score < 30:
        return "入门"
    if score < 50:
        return "普及-"
    if score < 62:
        return "普及/提高-"
    if score < 78:
        return "普及+/提高"
    return "提高+/省选"


def main() -> None:
    parser = argparse.ArgumentParser(description="AIgoTutor 种子数据")
    parser.add_argument("--reset", action="store_true", help="清库重建")
    args = parser.parse_args()
    result = asyncio.run(seed(reset=args.reset))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
