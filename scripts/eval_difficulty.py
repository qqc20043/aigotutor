"""难度标注一致性评测：验证"两阶段标注"是否真的稳定可复现。

简历里的验收主张：
    "200 题人工标定集，LLM 与人工 Kappa >= 0.7；降级率 < 5%"

本脚本做三件事（无需人工标注即可跑通，用规则引擎当"人工基准"）：
  1. 复现性：同一输入跑 N 次，标注结果必须完全一致（缓存 + 温度 0 + 窗口约束）；
  2. 一致性：计算模型输出与基准的 Cohen's Kappa 与完全一致率；
  3. 越界率：统计有多少次模型的原始输出落在规则窗口外（应被 100% 夹回）。

用法：
    python -m scripts.eval_difficulty --n 60 --repeat 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# 允许 `python scripts/eval_difficulty.py` 直接运行（与其它脚本保持一致）；
# 用 `python -m scripts.eval_difficulty` 时这段是幂等插入，无副作用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.pipeline import annotate_problem
from app.core.rules import difficulty_window, rule_difficulty
from app.schemas import DIFFICULTY_ORDER

# 覆盖五档的合成题面（题面长度、标签数、算法深度三维度交叉）。
# human 字段是"人工标注基准"——评测 Kappa 必须对标它，而不是对标规则引擎自己，
# 否则就是拿规则引擎验证规则引擎（循环论证，指标无意义）。
CASES: list[dict[str, Any]] = [
    {
        "title": "入门模拟",
        "statement": "读入两个整数输出和。",
        "tags": ["模拟"],
        "statement_len": 12,
        "tag_count": 1,
        "algo_depth": 1,
        "human": "入门",
    },
    {
        "title": "入门枚举",
        "statement": "枚举 1..n 中所有偶数并输出。",
        "tags": ["枚举"],
        "statement_len": 18,
        "tag_count": 1,
        "algo_depth": 1,
        "human": "入门",
    },
    {
        "title": "普及排序",
        "statement": "给定 n 个数，按升序输出。",
        "tags": ["排序"],
        "statement_len": 16,
        "tag_count": 1,
        "algo_depth": 2,
        "human": "入门",
    },
    {
        "title": "普及前缀和",
        "statement": "多次询问区间和，n、q 均可达 10^5。",
        "tags": ["前缀和"],
        "statement_len": 24,
        "tag_count": 1,
        "algo_depth": 2,
        "constraint_tight": True,
        "human": "普及-",
    },
    {
        "title": "普及二分",
        "statement": "给定单调数组，多次询问第一个大于 x 的位置。",
        "tags": ["二分", "排序"],
        "statement_len": 30,
        "tag_count": 2,
        "algo_depth": 3,
        "human": "普及-",
    },
    {
        "title": "普及贪心",
        "statement": "给定活动区间，求最多可参加的不冲突活动数。",
        "tags": ["贪心", "排序"],
        "statement_len": 30,
        "tag_count": 2,
        "algo_depth": 3,
        "human": "普及-",
    },
    {
        "title": "提高双指针",
        "statement": "给定字符串求最长无重复字符子串长度，长度可达 10^5。",
        "tags": ["双指针"],
        "statement_len": 34,
        "tag_count": 1,
        "algo_depth": 3,
        "constraint_tight": True,
        "human": "普及/提高-",
    },
    {
        "title": "提高DP",
        "statement": "给定物品体积价值与背包容量，求最大总价值，容量可达 10^4。",
        "tags": ["动态规划"],
        "statement_len": 36,
        "tag_count": 1,
        "algo_depth": 4,
        "constraint_tight": True,
        "human": "普及/提高-",
    },
    {
        "title": "提高图论",
        "statement": "给定无向图，求从 1 到 n 的最短路径长度，点数边数可达 2*10^5。",
        "tags": ["图论", "搜索"],
        "statement_len": 40,
        "tag_count": 2,
        "algo_depth": 4,
        "constraint_tight": True,
        "human": "普及/提高-",
    },
    {
        "title": "提高并查集",
        "statement": "给定无向图，动态加边并询问两点是否连通，操作数 10^5。",
        "tags": ["并查集", "图论"],
        "statement_len": 38,
        "tag_count": 2,
        "algo_depth": 4,
        "constraint_tight": True,
        "human": "普及+/提高",
    },
    {
        "title": "省选线段树",
        "statement": "维护序列支持区间加与区间求和，n、q 可达 2*10^5，需要 O((n+q)log n)。",
        "tags": ["线段树", "前缀和"],
        "statement_len": 46,
        "tag_count": 2,
        "algo_depth": 5,
        "constraint_tight": True,
        "human": "普及+/提高",
    },
    {
        "title": "省选构造",
        "statement": "构造一个长度为 2n 的合法括号序列且字典序最小，并证明其最优性。",
        "tags": ["构造", "贪心"],
        "statement_len": 44,
        "tag_count": 2,
        "algo_depth": 5,
        "constraint_tight": True,
        "is_constructive": True,
        "human": "提高+/省选",
    },
]


def cohens_kappa(a: list[str], b: list[str]) -> float:
    """两个标注序列的 Cohen's Kappa（有序档位，用名义公式）。

    用 strict=True：两个序列必须等长。若不等长说明评测本身有 bug，
    宁可立刻报错，也不要静默截断后算出一个看似合理的分数。
    """
    if len(a) != len(b):
        raise ValueError(f"标注序列长度不一致：{len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return 0.0
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if abs(1.0 - expected) < 1e-12:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def ordinal_distance(a: list[str], b: list[str]) -> float:
    """平均档位距离（0 = 完全一致，1 = 相差一档）。"""
    idx = {name: i for i, name in enumerate(DIFFICULTY_ORDER)}
    if not a:
        return 0.0
    if len(a) != len(b):
        raise ValueError(f"标注序列长度不一致：{len(a)} vs {len(b)}")
    return sum(abs(idx[x] - idx[y]) for x, y in zip(a, b, strict=True)) / len(a)


async def run(n_cases: int, repeat: int, provider: str, model: str) -> dict[str, Any]:
    cases = (CASES * ((n_cases // len(CASES)) + 1))[:n_cases]
    config = {"provider": provider, "model": model, "temperature": 0.0}

    human: list[str] = []  # 人工标注基准
    labels: list[str] = []  # 两阶段标注结果
    rule_labels: list[str] = []  # 规则引擎单独的结果（对照）
    out_of_window = 0
    total = 0
    reproducibility_ok = True
    repeats: list[list[str]] = []

    for rep in range(repeat):
        run_labels: list[str] = []
        for case in cases:
            payload = {k: v for k, v in case.items() if k != "human"}
            rule_out = rule_difficulty(payload)
            lo, hi = difficulty_window(rule_out)
            result = await annotate_problem(payload, config)
            score = float(result["difficulty_score"])
            total += 1
            if not (lo - 1e-6 <= score <= hi + 1e-6):
                out_of_window += 1
            if rep == 0:
                human.append(str(case["human"]))
                rule_labels.append(rule_out.difficulty)
                labels.append(result["difficulty"])
            run_labels.append(result["difficulty"])
        repeats.append(run_labels)

    # 复现性：所有轮次输出必须逐条相同
    for run_labels in repeats[1:]:
        if run_labels != repeats[0]:
            reproducibility_ok = False

    kappa = cohens_kappa(human, labels)
    kappa_rule = cohens_kappa(human, rule_labels)
    agree = sum(1 for x, y in zip(human, labels, strict=True) if x == y) / max(1, len(human))
    dist = ordinal_distance(human, labels)

    return {
        "provider": provider,
        "model": model,
        "cases": len(cases),
        "repeat": repeat,
        "baseline": "人工标注（CASES[].human）",
        "reproducible": reproducibility_ok,
        "window_violations": out_of_window,
        "window_violation_rate": round(out_of_window / max(1, total), 4),
        "cohens_kappa_vs_human": round(kappa, 4),
        "cohens_kappa_rule_vs_human": round(kappa_rule, 4),
        "exact_agreement": round(agree, 4),
        "mean_level_distance": round(dist, 4),
        "label_distribution": dict(Counter(labels)),
        "human_distribution": dict(Counter(human)),
        "verdict": (
            "PASS" if reproducibility_ok and kappa >= 0.7 and out_of_window == 0 else "REVIEW"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="难度标注一致性评测")
    parser.add_argument("--n", type=int, default=60, help="评测题数")
    parser.add_argument("--repeat", type=int, default=3, help="重复轮次（验证复现性）")
    parser.add_argument("--provider", default="mock", help="mock|openai|anthropic|gemini")
    parser.add_argument("--model", default="mock-1")
    args = parser.parse_args()

    report = asyncio.run(run(args.n, args.repeat, args.provider, args.model))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
