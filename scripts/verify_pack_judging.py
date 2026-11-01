"""题包判题自检：把每题的 solve 源码包装成可执行脚本，提交真实沙箱判题。

用法：python -m scripts.verify_pack_judging [--title 题名]
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.problem_pack import PROBLEMS

WRAPPER = """\
import heapq
import json
import math
import sys
from collections import deque

data = sys.stdin.read()

def _main():
{body}
    return {name}(data)

sys.stdout.write(_main())
"""


def wrap_solver(fn) -> str:
    src = inspect.getsource(fn)
    name = fn.__name__
    body = "\n".join("    " + line for line in src.splitlines())
    return WRAPPER.format(body=body, name=name)


async def main() -> int:
    from sqlalchemy import select as sa_select

    from app.db import init_db, session_scope
    from app.judge_service import judge_submission_in_sandbox
    from app.models import Problem

    only = None
    if "--title" in sys.argv:
        only = sys.argv[sys.argv.index("--title") + 1]

    await init_db()
    fails = 0
    async with session_scope() as db:
        for prob in PROBLEMS:
            if only and prob["title"] != only:
                continue
            row = (
                await db.execute(sa_select(Problem).where(Problem.title == prob["title"]))
            ).scalar_one_or_none()
            if row is None:
                print(f"[缺题] {prob['title']} 不在数据库，先跑 seed_problems")
                fails += 1
                continue
            code = wrap_solver(prob["solve"])
            result = await judge_submission_in_sandbox(db, row, "python", code)
            verdict = result["verdict"]
            mark = "✓" if verdict == "AC" else "✗"
            print(
                f"{mark} {prob['title']}: {verdict} "
                f"({result['passed_cases']}/{result['total_cases']} 用例, {result['runtime_ms']}ms)"
            )
            if verdict != "AC":
                fails += 1
                detail = result.get("judge_detail") or {}
                print(f"   详情: {detail.get('message', '')}")
    print(f"\n结果: {'全部 AC' if fails == 0 else f'{fails} 题未 AC'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
