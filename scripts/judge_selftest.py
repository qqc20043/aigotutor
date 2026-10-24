"""判题后端实测：验证各种 verdict 判定是否真实生效。

用法：
    python scripts/judge_selftest.py
    python scripts/judge_selftest.py --language java
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.judge import JudgeRequest, TestCase, build_backend

PY_AC = "a, b = map(int, input().split())\nprint(a + b)\n"
PY_WA = "a, b = map(int, input().split())\nprint(a * b)\n"
PY_TLE = "while True:\n    pass\n"
PY_RE = "a, b = map(int, input().split())\nprint(1 / 0)\n"
PY_MLE = "x = [0] * (400 * 1024 * 1024)\nprint(len(x))\n"
PY_CE = "def broken(\n"
PY_BIG_OUT = "print('x' * 200000)\n"

JA_AC = """import java.util.*;
public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        int a = sc.nextInt(), b = sc.nextInt();
        System.out.println(a + b);
    }
}
"""
JA_WA = """import java.util.*;
public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        int a = sc.nextInt(), b = sc.nextInt();
        System.out.println(a * b);
    }
}
"""
JA_CE = "public class Main { public static void main(String[] a) { int x = ; } }"
# -Xmx 由判题后端按 memory_limit_mb 注入（512MB 堆装不下 4GB 数组），
# JVM 会抛 OutOfMemoryError 退出 —— 用于验证 java 的内存上限真实生效。
JA_MLE = """import java.util.*;
public class Main {
    public static void main(String[] args) {
        int[] big = new int[Integer.MAX_VALUE / 2];
        System.out.println(big.length);
    }
}
"""

CPP_AC = """#include <iostream>
int main() {
    int a, b;
    std::cin >> a >> b;
    std::cout << a + b << std::endl;
    return 0;
}
"""
CPP_WA = """#include <iostream>
int main() {
    int a, b;
    std::cin >> a >> b;
    std::cout << a * b << std::endl;
    return 0;
}
"""
CPP_CE = "int main() { int x = ; }"

CASES = [
    TestCase(id="c1", input_data="1 2\n", expected_output="3\n"),
    TestCase(id="c2", input_data="100 200\n", expected_output="300\n"),
]

# 场景格式：(名称, 代码, 期望 verdict, 时间限制 ms, 内存限制 MB)
#
# 为什么每个场景要自带内存限制：不同语言的运行时基线内存差异极大 ——
# C++ 动态链接程序加载后基线就有数十 MB，Java 更是上百 MB。若所有场景共用
# 一个 64MB 上限，C++ 的正常程序会在启动阶段就被判 MLE（CI 实测踩到）。
# 需要触发 MLE 的场景给一个"略高于运行时基线、但远低于分配量"的值。
SCENARIOS: dict[str, list[tuple[str, str, str, int, int]]] = {
    "python": [
        ("AC  正确实现", PY_AC, "AC", 1000, 256),
        ("WA  错误实现", PY_WA, "WA", 1000, 256),
        ("TLE 死循环", PY_TLE, "TLE", 1000, 256),
        ("RE  除零异常", PY_RE, "RE", 1000, 256),
        ("MLE 超内存", PY_MLE, "MLE", 1000, 64),
        ("CE  语法错误", PY_CE, "CE", 1000, 256),
        ("OLE 超大输出", PY_BIG_OUT, "OLE", 1000, 256),
    ],
    "java": [
        ("AC  正确实现", JA_AC, "AC", 3000, 512),
        ("WA  错误实现", JA_WA, "WA", 3000, 512),
        ("MLE 超内存", JA_MLE, "MLE", 3000, 512),
        ("CE  语法错误", JA_CE, "CE", 3000, 512),
    ],
    "cpp": [
        ("AC  正确实现", CPP_AC, "AC", 1000, 256),
        ("WA  错误实现", CPP_WA, "WA", 1000, 256),
        ("CE  语法错误", CPP_CE, "CE", 1000, 256),
    ],
}


def _scenarios_for(language: str) -> list[tuple[str, str, str, int, int]]:
    """取某语言的场景清单；没有定义场景时返回空列表而不是抛 KeyError。

    真实故障：脚本按后端上报的 languages 遍历，而 SCENARIOS 只覆盖 python/java；
    在装了 g++ 的机器（如 CI 的 ubuntu runner）上直接 KeyError: 'cpp' 崩溃。
    本机没有 g++ 所以一直没暴露 —— 缺少场景应当"跳过并说明"，而不是让整个自测挂掉。
    """
    return SCENARIOS.get(language, [])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", default=os.environ.get("AIGOTUTOR_JUDGE_BACKEND", "subprocess")
    )
    parser.add_argument("--language", default=None, help="只测某语言")
    args = parser.parse_args()

    backend = build_backend(args.backend)
    info = backend.availability()
    print("== 判题后端 ==")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    if not info["available"]:
        print("\n后端不可用，无法实测。")
        return 1

    languages = [args.language] if args.language else info["languages"]
    failures: list[str] = []
    checks = 0

    for lang in languages:
        if lang not in info["languages"]:
            print(f"\n跳过 {lang}（本机不可用）")
            continue
        scenarios = _scenarios_for(lang)
        if not scenarios:
            print(f"\n跳过 {lang}（本脚本未定义该语言的测试场景）")
            continue
        print(f"\n== {lang} ==")
        for name, code, expect, tl, mem in scenarios:
            req = JudgeRequest(
                language=lang,
                code=code,
                cases=CASES,
                time_limit_ms=tl,
                memory_limit_mb=mem,
            )
            result = await backend.judge(req)
            checks += 1
            ok = result.verdict == expect
            mark = "PASS" if ok else "FAIL"
            detail = f"{result.verdict}"
            if not ok:
                detail += f" (期望 {expect})"
                failures.append(f"{lang}/{name}")
            extra = ""
            if result.verdict != "CE" and result.cases:
                extra = f" time={result.time_ms}ms {result.passed}/{result.total}"
            print(f"  [{mark}] {name:<16} -> {detail}{extra}")

    print(f"\n== 结果: {checks - len(failures)}/{checks} 通过 ==")
    if failures:
        print("失败: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
