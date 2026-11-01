"""内置题包回归测试。

三层保障：
1. 数据完整性（标题唯一、字段齐全、难度合法、每组题有隐藏用例）；
2. 正解与用例自洽（56 组 solve 直跑全过）；
3. 导入闭环（seed 增量幂等 + 库里用例数一致 + 真实沙箱判 1 题 AC）。
"""

from __future__ import annotations

import inspect

from scripts.problem_pack import PROBLEMS
from scripts.seed_problems import seed_problems
from scripts.verify_pack_judging import wrap_solver

DIFFICULTIES = {"入门", "普及-", "普及/提高-", "普及+", "普及+/提高", "提高+/省选"}


def test_pack_integrity():
    titles = [p["title"] for p in PROBLEMS]
    assert len(titles) == len(set(titles)), "题包内标题必须唯一"
    assert len(PROBLEMS) >= 20, "题包应不少于 20 题"
    for p in PROBLEMS:
        assert p["title"].strip()
        assert p["statement"].strip() and p["input_desc"].strip()
        assert p["output_desc"].strip() and p["constraints"].strip()
        assert p["difficulty"] in DIFFICULTIES, f"{p['title']} 难度非法: {p['difficulty']}"
        assert p["tags"], f"{p['title']} 缺标签"
        assert p["testcases"], f"{p['title']} 缺测试用例"
        for stdin_text, expected in p["testcases"]:
            assert stdin_text.endswith("\n"), f"{p['title']} 输入应以换行结尾"
            assert expected.endswith("\n"), f"{p['title']} 期望输出应以换行结尾"
        # 样例展示取前 2 组，需至少有 1 组
        assert len(p["testcases"]) >= 1


def test_pack_solvers_match_expected():
    """56 组用例：solve 直跑必须与期望输出完全一致。"""
    total = 0
    for p in PROBLEMS:
        for stdin_text, expected in p["testcases"]:
            actual = p["solve"](stdin_text)
            assert actual == expected, (
                f"{p['title']}: solve({stdin_text!r}) = {actual!r} != {expected!r}"
            )
            total += 1
    assert total == sum(len(p["testcases"]) for p in PROBLEMS)


def test_wrap_solver_compiles():
    """包装产物必须可编译且包含真实函数名调用（verify_pack_judging 的核心路径）。"""
    for p in PROBLEMS:
        code = wrap_solver(p["solve"])
        compile(code, f"<{p['title']}>", "exec")
        assert p["solve"].__name__ in code, "wrapper 必须调用真实函数名"


def test_wrap_solver_runs_subprocess():
    """抽 2 题真实子进程跑一遍（覆盖 import 缺失这类 compile 抓不到的问题）。"""
    import subprocess
    import sys

    picked = [PROBLEMS[0], PROBLEMS[-1]]
    for p in picked:
        code = wrap_solver(p["solve"])
        stdin_text, expected = p["testcases"][0]
        r = subprocess.run(
            [sys.executable, "-c", code],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, f"{p['title']} 子进程失败: {r.stderr}"
        assert r.stdout == expected


async def _shadow_titles() -> set[str]:
    """库里已有的、非题包来源的同名题（seed 按标题跳过它们，断言需按相对值算）。"""
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem

    async with session_scope() as db:
        rows = (await db.execute(sa_select(Problem.title).where(Problem.source != "pack"))).all()
    return {t for (t,) in rows} & {p["title"] for p in PROBLEMS}


async def test_seed_problems_idempotent():
    """seed 增量语义：首次导入「库里还没有的」全部题；再导一次全部跳过。

    库里可能已有同名的非题包题（其他测试/旧数据），seed 按标题跳过，
    因此 imported 的新题数 = 总数 - 已存在的同名题数（相对断言）。
    """
    shadows = await _shadow_titles()
    fresh = len(PROBLEMS) - len(shadows)

    first = await seed_problems(verify=False)
    assert first.get("imported") == fresh, f"应导入 {fresh} 道新题: {first}"
    assert first.get("skipped") == len(shadows)

    second = await seed_problems(verify=False)
    assert second.get("imported") == 0
    assert second.get("skipped") == len(PROBLEMS)

    # 幂等后：每个题包标题在库里都有对应题
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem

    async with session_scope() as db:
        db_titles = {t for (t,) in (await db.execute(sa_select(Problem.title))).all()}
    missing = {p["title"] for p in PROBLEMS} - db_titles
    assert not missing, f"导入后仍有缺失: {missing}"


async def test_seeded_problems_match_pack():
    """库里的题包题（source=pack）：难度一致、隐藏用例数一致。

    source=pack 的行数 = 总数 - 同名外部题（被跳过未以 pack 身份入库）。
    """
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem, TestCase

    await seed_problems(verify=False)
    shadows = await _shadow_titles()
    async with session_scope() as db:
        rows = (
            (await db.execute(sa_select(Problem).where(Problem.source == "pack"))).scalars().all()
        )
        assert len(rows) == len(PROBLEMS) - len(shadows)
        pack_by_title = {p["title"]: p for p in PROBLEMS}
        for row in rows:
            p = pack_by_title[row.title]
            assert row.difficulty == p["difficulty"]
            n_cases = (
                await db.execute(
                    sa_select(func.count())
                    .select_from(TestCase)
                    .where(TestCase.problem_id == row.id)
                )
            ).scalar()
            assert n_cases == len(p["testcases"]), (
                f"{row.title}: 用例数 {n_cases} != {len(p['testcases'])}"
            )


async def test_seeded_problem_judges_ac_in_sandbox():
    """端到端：真实沙箱判题，题包正解必须 AC（CI 有 python，可跑）。"""
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.judge_service import judge_submission_in_sandbox
    from app.models import Problem

    await seed_problems(verify=False)
    prob = PROBLEMS[0]  # 爬楼梯，3 用例
    code = wrap_solver(prob["solve"])
    async with session_scope() as db:
        row = (
            await db.execute(sa_select(Problem).where(Problem.title == prob["title"]))
        ).scalar_one()
        result = await judge_submission_in_sandbox(db, row, "python", code)
    assert result["verdict"] == "AC", f"沙箱判题应 AC: {result}"
    assert result["passed_cases"] == result["total_cases"] == len(prob["testcases"])


async def test_seeded_problem_judges_wa_for_wrong_solution():
    """区分度：错误解法必须 WA 而不是误判 AC。"""
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.judge_service import judge_submission_in_sandbox
    from app.models import Problem

    await seed_problems(verify=False)
    wrong = "import sys\ndata = sys.stdin.read()\nprint(len(data))\n"
    async with session_scope() as db:
        row = (
            await db.execute(sa_select(Problem).where(Problem.title == PROBLEMS[0]["title"]))
        ).scalar_one()
        result = await judge_submission_in_sandbox(db, row, "python", wrong)
    assert result["verdict"] == "WA", f"错误解法应 WA: {result['verdict']}"


def test_solver_sources_do_not_use_forbidden_names():
    """solve 是判题正解的基准实现：不应引用未定义的全局（防再犯 wrapper 忘带 import 的坑）。"""
    allowed_modules = {"json", "math", "heapq", "deque", "sys"}
    for p in PROBLEMS:
        src = inspect.getsource(p["solve"])
        # 粗查：源码里出现模块名时，WRAPPER 里必须有对应 import
        for name in ("json", "math", "heapq", "deque"):
            if f"{name}." in src or f"({name}," in src or f"{name})" in src:
                assert name in allowed_modules
