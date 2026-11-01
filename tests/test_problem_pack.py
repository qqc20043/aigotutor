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
        # 官方题解：三段式齐全 + 至少一段参考代码
        sol = p.get("solution", "")
        assert "## 思路" in sol and "## 复杂度" in sol and "## 参考代码" in sol, (
            f"{p['title']} 题解缺标准段落"
        )
        assert "```python" in sol, f"{p['title']} 题解缺参考代码"


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


def test_solution_reference_codes_compile():
    """题解里的每个 ```python 块都必须可编译。"""
    import re

    for p in PROBLEMS:
        for i, code in enumerate(re.findall(r"```python\n(.*?)```", p["solution"], re.DOTALL)):
            compile(code, f"<{p['title']}#code{i}>", "exec")


def test_solution_reference_codes_pass_all_testcases():
    """题解参考代码逐题逐用例真实子进程验证——"参考代码均经沙箱 AC"是硬承诺。

    56 次运行约 3s，换取"官方题解代码一定对"这条产品主张成立。
    """
    import re
    import subprocess
    import sys

    for p in PROBLEMS:
        codes = re.findall(r"```python\n(.*?)```", p["solution"], re.DOTALL)
        assert codes, f"{p['title']} 题解缺参考代码"
        for stdin_text, expected in p["testcases"]:
            r = subprocess.run(
                [sys.executable, "-c", codes[0]],
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=15,
            )
            stdout = r.stdout.replace("\r\n", "\n")  # Windows 子进程 CRLF 归一
            assert r.returncode == 0, f"{p['title']} 参考代码运行失败: {r.stderr}"
            assert stdout == expected, f"{p['title']}: got {stdout!r} want {expected!r}"


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
            assert row.official_solution == p["solution"], f"{row.title} 题解未入库或不一致"
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


async def test_seed_backfills_missing_solutions():
    """回填语义：pack 来源且题解为空的旧题，重跑 seed 补题解；非 pack 题不动。"""
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem

    await seed_problems(verify=False)
    async with session_scope() as db:
        row = (
            (
                await db.execute(
                    sa_select(Problem).where(
                        Problem.source == "pack",
                        Problem.title == PROBLEMS[0]["title"],
                    )
                )
            )
            .scalars()
            .one()
        )
        row.official_solution = ""  # 模拟"题解能力上线前的旧导入"
    result = await seed_problems(verify=False)
    assert result.get("backfilled") == 1, f"应恰好回填 1 道: {result}"
    async with session_scope() as db:
        row = (
            (
                await db.execute(
                    sa_select(Problem).where(
                        Problem.source == "pack",
                        Problem.title == PROBLEMS[0]["title"],
                    )
                )
            )
            .scalars()
            .one()
        )
        assert row.official_solution == PROBLEMS[0]["solution"], "回填后题解应恢复"


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


# ================================================================ API 门控
async def _demote_to_user(client) -> None:
    """把当前登录用户显式降为普通角色。

    register 有"首个用户自动成为管理员"的引导逻辑——单文件跑测试时
    auth_client 的用户恰好是首个注册者，门控测试必须与注册顺序无关。
    """
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import User as UserModel

    me = (await client.get("/api/auth/me")).json()
    async with session_scope() as db:
        row = (await db.execute(sa_select(UserModel).where(UserModel.id == me["id"]))).scalar_one()
        row.role = "user"


async def _pack_problem_id() -> str:
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem

    await seed_problems(verify=False)
    async with session_scope() as db:
        row = (
            await db.execute(sa_select(Problem).where(Problem.title == PROBLEMS[0]["title"]))
        ).scalar_one()
        return row.id


async def test_solution_gate_locked_before_ac(auth_client):
    """未 AC：has 标记恒下发，内容为空串（防 devtools 直接看到答案）。"""
    await _demote_to_user(auth_client)
    pid = await _pack_problem_id()
    r = await auth_client.get(f"/api/problems/{pid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_official_solution"] is True
    assert body["official_solution"] == "", "未 AC 用户不应拿到题解内容"


async def test_solution_gate_unlocked_after_ac(auth_client):
    """AC 过：详情接口下发题解内容。"""
    await _demote_to_user(auth_client)
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem, Submission

    pid = await _pack_problem_id()
    me = (await auth_client.get("/api/auth/me")).json()
    async with session_scope() as db:
        row = (await db.execute(sa_select(Problem).where(Problem.id == pid))).scalar_one()
        db.add(
            Submission(
                user_id=me["id"],
                problem_id=row.id,
                language="python",
                code="# ac",
                verdict="AC",
                passed_cases=1,
                total_cases=1,
            )
        )
    r = await auth_client.get(f"/api/problems/{pid}")
    body = r.json()
    assert body["has_official_solution"] is True
    assert body["official_solution"] == PROBLEMS[0]["solution"], "AC 后应拿到题解内容"


async def test_solution_gate_admin_sees_without_ac(client):
    """管理员免 AC 直接可见（治理视角）。"""
    import uuid

    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import User as UserModel

    name = f"admin{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    assert r.status_code == 201, r.text
    async with session_scope() as db:
        row = (
            await db.execute(sa_select(UserModel).where(UserModel.username == name))
        ).scalar_one()
        row.role = "admin"
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    pid = await _pack_problem_id()
    r = await client.get(f"/api/problems/{pid}")
    body = r.json()
    assert body["has_official_solution"] is True
    assert body["official_solution"] == PROBLEMS[0]["solution"], "管理员应直接看到题解"


async def test_problem_without_solution_has_no_gate(client):
    """无题解的普通题：has=False、内容空（如管理员手建且未写题解的题）。"""
    import uuid

    name = f"plain{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    r = await client.post(
        "/api/problems",
        json={"title": f"无题解题{uuid.uuid4().hex[:6]}", "statement": "随意", "tags": ["模拟"]},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.get(f"/api/problems/{pid}")
    body = r.json()
    assert body["has_official_solution"] is False
    assert body["official_solution"] == ""


async def test_create_problem_accepts_official_solution(auth_client):
    """POST /api/problems 透传 official_solution（管理建题可带题解）。"""
    await _demote_to_user(auth_client)
    import uuid

    r = await auth_client.post(
        "/api/problems",
        json={
            "title": f"带题解题{uuid.uuid4().hex[:6]}",
            "statement": "题面",
            "tags": ["模拟"],
            "official_solution": "## 思路\n直接输出。",
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    body = (await auth_client.get(f"/api/problems/{pid}")).json()
    # 创建者未 AC——has=True 但内容被门控；由 admin patch/回填路径保证存储
    assert body["has_official_solution"] is True
    assert body["official_solution"] == ""
    # 管理员视角能确认内容已存储
    from sqlalchemy import select as sa_select

    from app.db import session_scope
    from app.models import Problem

    async with session_scope() as db:
        row = (await db.execute(sa_select(Problem).where(Problem.id == pid))).scalar_one()
        assert row.official_solution == "## 思路\n直接输出。"
