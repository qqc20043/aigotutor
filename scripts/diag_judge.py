"""诊断：单跑各个用例，找出卡住的那个。"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.judge import JudgeRequest, TestCase, build_backend

CASES = [
    TestCase(id="c1", input_data="1 2\n", expected_output="3\n"),
    TestCase(id="c2", input_data="100 200\n", expected_output="300\n"),
]

SAMPLES = [
    ("AC", "a, b = map(int, input().split())\nprint(a + b)\n"),
    ("WA", "a, b = map(int, input().split())\nprint(a * b)\n"),
    ("TLE", "while True:\n    pass\n"),
    ("RE", "a, b = map(int, input().split())\nprint(1 / 0)\n"),
    ("MLE", "x = [0] * (400 * 1024 * 1024)\nprint(len(x))\n"),
    ("CE", "def broken(\n"),
    ("OLE", "print('x' * 200000)\n"),
]


async def main() -> None:
    backend = build_backend("subprocess")
    for label, code in SAMPLES:
        req = JudgeRequest(
            language="python",
            code=code,
            cases=CASES,
            time_limit_ms=1000,
            memory_limit_mb=64,
        )
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(backend.judge(req), timeout=40)
            dt = (time.perf_counter() - t0) * 1000
            print(f"{label:<4} -> {result.verdict:<4} ({dt:.0f}ms) msg={result.message[:60]}")
        except TimeoutError:
            dt = (time.perf_counter() - t0) * 1000
            print(f"{label:<4} -> !!! 外层超时 {dt:.0f}ms  <-- 卡在这里")
            return
        sys.stdout.flush()


asyncio.run(main())
