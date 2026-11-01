"""内置题包导入：把 problem_pack.py 里的题导入数据库（增量，跳过已有标题）。

用法：
    python -m scripts.seed_problems           # 增量导入
    python -m scripts.seed_problems --verify  # 先验证所有用例再导入
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.problem_pack import PROBLEMS


async def seed_problems(*, reset: bool = False, verify: bool = False) -> dict:
    from sqlalchemy import select as sa_select

    from app.db import init_db, session_scope
    from app.judge_service import add_testcase
    from app.services import create_problem

    await init_db()

    # verify 模式：先跑全部用例
    if verify:
        print("== 验证所有用例 ==")
        all_ok = True
        for prob in PROBLEMS:
            for stdin_text, expected in prob["testcases"]:
                actual = prob["solve"](stdin_text)
                if actual != expected:
                    print(
                        f"  FAIL {prob['title']}: solve({stdin_text!r}) = {actual!r} != {expected!r}"
                    )
                    all_ok = False
        if all_ok:
            print(f"  全部 {sum(len(p['testcases']) for p in PROBLEMS)} 组用例验证通过")
        else:
            return {"error": "用例验证失败，请检查 solver"}

    imported, skipped = 0, 0

    async with session_scope() as db:
        # 已有标题集合
        from app.models import Problem as P

        existing = {r for (r,) in (await db.execute(sa_select(P.title))).all()}
        for prob in PROBLEMS:
            if prob["title"] in existing:
                skipped += 1
                continue
            problem = await create_problem(
                db,
                {
                    "title": prob["title"],
                    "statement": prob["statement"],
                    "input_desc": prob["input_desc"],
                    "output_desc": prob["output_desc"],
                    "constraints": prob["constraints"],
                    "samples": json.dumps(
                        [{"input": tc[0], "output": tc[1]} for tc in prob["testcases"][:2]],
                        ensure_ascii=False,
                    ),
                    "source": "pack",
                    "difficulty": prob["difficulty"],
                },
                tag_names=prob["tags"],
            )
            for stdin_text, expected in prob["testcases"]:
                await add_testcase(
                    db,
                    problem.id,
                    {"input": stdin_text, "output": expected, "hidden": True},
                )
            imported += 1
    return {"imported": imported, "skipped": skipped}


async def main() -> int:
    reset = "--reset" in sys.argv
    verify = "--verify" in sys.argv or True  # 默认总是验证

    if reset:
        print("--reset 暂不支持（避免误删），请手动清库后再导入")
        return 1

    result = await seed_problems(reset=reset, verify=verify)
    if "error" in result:
        print(result["error"])
        return 1
    print(f"导入 {result['imported']} 道，跳过 {result['skipped']} 道（已存在）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
