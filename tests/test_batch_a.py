"""批次 A 新能力测试：判题沙箱、用例管理、并发与幂等、任务对账、写锁。

这些测试对应本轮修复与新增的功能，防止后续回归。
"""

from __future__ import annotations

import asyncio

import pytest

from app.db import reset_write_stats, write_stats
from app.judge import (
    JudgeRequest,
    TestCase,
    build_backend,
    describe_backend,
    language_available,
    normalize_output,
    strip_bom,
)


# ============================================================ 判题后端
def test_normalize_output_ignores_trailing_whitespace():
    assert normalize_output("1 2  \n") == normalize_output("1 2\n")
    assert normalize_output("a\n\n\n") == normalize_output("a\n")
    assert normalize_output("a\r\nb") == normalize_output("a\nb")
    assert normalize_output("a b") != normalize_output("ab")


def test_strip_bom():
    assert strip_bom("\ufeffprint(1)") == "print(1)"
    assert strip_bom("print(1)") == "print(1)"
    assert strip_bom("") == ""


async def test_null_backend_reports_ie_not_silent_pass():
    """没配后端时必须明确报错，绝不能静默判 AC。"""
    backend = build_backend("none")
    result = await backend.judge(JudgeRequest(language="python", code="print(1)", cases=[]))
    assert result.verdict == "IE"
    assert "无法真实判题" in result.message


def test_describe_backend_reports_isolation_strength():
    """隔离强度必须如实上报 —— 不能让用户误以为本机跑是安全的。"""
    info = describe_backend()
    assert info["backend"] in ("subprocess", "docker", "none")
    if info["backend"] == "subprocess":
        assert info["isolation"] == "weak"
        assert "docker" in info["note"].lower()


def test_unsupported_language_returns_ie():
    backend = build_backend("subprocess")
    result = asyncio.run(
        backend.judge(
            JudgeRequest(
                language="brainfuck",
                code="+++",
                cases=[TestCase(id="c1", input_data="", expected_output="x")],
            )
        )
    )
    assert result.verdict == "IE"


def test_no_testcases_returns_ie():
    backend = build_backend("subprocess")
    result = asyncio.run(backend.judge(JudgeRequest(language="python", code="print(1)", cases=[])))
    assert result.verdict == "IE"
    assert "测试用例" in result.message


@pytest.mark.skipif(not language_available("python"), reason="本机无 python")
async def test_sandbox_python_ac_and_wa():
    backend = build_backend("subprocess")
    cases = [
        TestCase(id="c1", input_data="1 2\n", expected_output="3\n"),
        TestCase(id="c2", input_data="5 7\n", expected_output="12\n"),
    ]
    ok = await backend.judge(
        JudgeRequest(
            language="python",
            code="a, b = map(int, input().split())\nprint(a + b)\n",
            cases=cases,
        )
    )
    assert ok.verdict == "AC"
    assert ok.passed == 2 and ok.total == 2
    assert ok.score == 100.0

    bad = await backend.judge(
        JudgeRequest(
            language="python",
            code="a, b = map(int, input().split())\nprint(a * b)\n",
            cases=cases,
        )
    )
    assert bad.verdict == "WA"
    assert bad.passed == 0


@pytest.mark.skipif(not language_available("python"), reason="本机无 python")
async def test_sandbox_detects_syntax_error_as_ce_not_re():
    """解释型语言的语法错误必须判 CE，不能混成 RE（实测踩过的坑）。"""
    backend = build_backend("subprocess")
    result = await backend.judge(
        JudgeRequest(
            language="python",
            code="def broken(\n",
            cases=[TestCase(id="c1", input_data="", expected_output="3\n")],
        )
    )
    assert result.verdict == "CE"


@pytest.mark.skipif(not language_available("python"), reason="本机无 python")
async def test_sandbox_detects_timeout():
    backend = build_backend("subprocess")
    result = await backend.judge(
        JudgeRequest(
            language="python",
            code="while True:\n    pass\n",
            cases=[TestCase(id="c1", input_data="", expected_output="")],
            time_limit_ms=1000,
        )
    )
    assert result.verdict == "TLE"


@pytest.mark.skipif(not language_available("python"), reason="本机无 python")
async def test_sandbox_kills_and_does_not_hang():
    """超时后必须在合理时间内返回，不能挂死事件循环（实测踩过 40s 卡死）。"""
    backend = build_backend("subprocess")
    started = asyncio.get_event_loop().time()
    result = await asyncio.wait_for(
        backend.judge(
            JudgeRequest(
                language="python",
                code="while True:\n    pass\n",
                cases=[TestCase(id="c1", input_data="", expected_output="")],
                time_limit_ms=500,
            )
        ),
        timeout=20,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert result.verdict == "TLE"
    assert elapsed < 15, f"超时处理耗时 {elapsed:.1f}s，疑似挂死"


async def test_sandbox_cleans_workdir():
    """判题临时目录必须清理，否则磁盘会被跑满。"""
    import tempfile
    from pathlib import Path

    backend = build_backend("subprocess")
    if not language_available("python"):
        pytest.skip("本机无 python")
    before = set(Path(tempfile.gettempdir()).glob("aigotutor-judge-*"))
    await backend.judge(
        JudgeRequest(
            language="python",
            code="print(1)\n",
            cases=[TestCase(id="c1", input_data="", expected_output="1\n")],
        )
    )
    after = set(Path(tempfile.gettempdir()).glob("aigotutor-judge-*"))
    assert after <= before, f"残留临时目录: {after - before}"


# ======================================================== 用例管理 API
async def test_testcase_crud_flow(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]

    # 初始应有用例（种子数据未配，所以可能是 0）
    r = await seeded_client.get(f"/api/problems/{pid}/testcases")
    assert r.status_code == 200

    # 批量设置
    r = await seeded_client.put(
        f"/api/problems/{pid}/testcases",
        json={
            "replace": True,
            "cases": [
                {"input": "1 2\n", "output": "3\n", "hidden": False, "score": 10},
                {"input": "10 20\n", "output": "30\n", "hidden": True, "score": 10},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 2
    assert r.json()["total"] == 2

    # 列表：隐藏用例内容不回显
    r = await seeded_client.get(f"/api/problems/{pid}/testcases")
    items = r.json()["items"]
    assert len(items) == 2
    visible = [c for c in items if not c["hidden"]]
    hidden = [c for c in items if c["hidden"]]
    assert visible[0]["input_data"] == "1 2\n"
    assert hidden[0]["input_data"] == "", "隐藏用例不该回显输入"
    assert hidden[0]["expected_output"] == "", "隐藏用例不该回显期望输出"

    # 新增单个
    r = await seeded_client.post(
        f"/api/problems/{pid}/testcases",
        json={"input": "0 0\n", "output": "0\n", "hidden": False},
    )
    assert r.status_code == 201
    case_id = r.json()["id"]

    # 删除
    r = await seeded_client.delete(f"/api/problems/{pid}/testcases/{case_id}")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # 重复删除 -> 404
    r = await seeded_client.delete(f"/api/problems/{pid}/testcases/{case_id}")
    assert r.status_code == 404


async def test_testcase_requires_output(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(
        f"/api/problems/{pid}/testcases", json={"input": "1\n", "output": ""}
    )
    assert r.status_code == 422


async def test_judge_backend_info_endpoint(auth_client):
    r = await auth_client.get("/api/judge")
    assert r.status_code == 200
    body = r.json()
    assert "backend" in body and "languages" in body
    assert "isolation" in body


# ==================================================== 真实沙箱判题闭环
@pytest.mark.skipif(not language_available("python"), reason="本机无 python")
async def test_submit_runs_real_sandbox(seeded_client):
    """端到端：建题 → 配用例 → 提交代码 → 真实判题得出 verdict。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]

    await seeded_client.put(
        f"/api/problems/{pid}/testcases",
        json={
            "replace": True,
            "cases": [
                {"input": "1 2\n", "output": "3\n", "hidden": False},
                {"input": "7 8\n", "output": "15\n", "hidden": True},
            ],
        },
    )

    # 正确代码 -> AC
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "language": "python",
            "code": "a, b = map(int, input().split())\nprint(a + b)\n",
            "judge_mode": "sandbox",
            "run_agents": False,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["judge_mode"] == "sandbox"
    assert body["verdict"] == "AC", body
    assert body["submission"]["judged_by"] == "sandbox"
    assert body["submission"]["passed_cases"] == 2

    # 错误代码 -> WA
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "language": "python",
            "code": "a, b = map(int, input().split())\nprint(a - b)\n",
            "judge_mode": "sandbox",
            "run_agents": False,
            "dedupe": False,
        },
    )
    assert r.json()["verdict"] == "WA"


async def test_judge_mode_caller_still_works(seeded_client):
    """保留调用方判定模式，兼容外部判题机对接。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "code": "print(1)\n",
            "judge_mode": "caller",
            "verdict": "AC",
            "passed_cases": 3,
            "total_cases": 3,
            "run_agents": False,
        },
    )
    assert r.status_code == 201
    assert r.json()["verdict"] == "AC"
    assert r.json()["submission"]["judged_by"] == "caller"


async def test_judge_mode_auto_falls_back_without_cases(seeded_client):
    """auto 模式：该题没有用例时回退 caller，并说明原因。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][1]["id"]
    await seeded_client.put(f"/api/problems/{pid}/testcases", json={"replace": True, "cases": []})
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "code": "print(1)\n",
            "judge_mode": "auto",
            "verdict": "WA",
            "run_agents": False,
        },
    )
    assert r.status_code == 201
    assert r.json()["judge_mode"] == "caller"


async def test_auto_fallback_attaches_reason(seeded_client):
    """回归：auto 回退必须带原因说明。

    旧版判断写的是 `mode == "sandbox"`，而 mode 此时已被改写为 "caller"，
    条件恒假 -> 回退提示永不出现，用户会误以为 caller 的 verdict 是判题机结论。
    """
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][1]["id"]
    await seeded_client.put(f"/api/problems/{pid}/testcases", json={"replace": True, "cases": []})
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "code": "print(2)\n",
            "judge_mode": "auto",
            "verdict": "WA",
            "run_agents": False,
        },
    )
    assert r.status_code == 201, r.text
    detail = r.json()["judge_detail"]
    assert "回退" in detail.get("message", ""), f"回退原因缺失: {detail}"


async def test_sandbox_reports_failed_case_detail(seeded_client):
    """回归：判错时必须给出失败用例详情（failed_case）。

    旧版条件 `not first_fail.case_id.startswith("")` 恒为 False（空串的
    startswith 永远是 True），整块赋值成了死代码 —— failed_case 永远为空，
    错题本与 judge Agent 的归因都拿不到失败用例内容。
    """
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    await seeded_client.put(
        f"/api/problems/{pid}/testcases",
        json={
            "replace": True,
            "cases": [{"input": "1 2\n", "output": "3\n", "hidden": False}],
        },
    )
    r = await seeded_client.post(
        "/api/submissions",
        json={
            "problem_id": pid,
            "language": "python",
            "code": "a, b = map(int, input().split())\nprint(a * b)\n",
            "judge_mode": "sandbox",
            "run_agents": False,
            "dedupe": False,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["verdict"] == "WA"

    # 从库里取回该提交，确认 failed_case 真的落了内容
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Submission

    async with session_scope() as db:
        sub = (
            await db.execute(
                select(Submission)
                .where(Submission.problem_id == pid)
                .order_by(Submission.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert "用例" in sub.failed_case, f"failed_case 为空: {sub.failed_case!r}"
        assert "3" in sub.failed_case  # 期望输出


def test_loop_supports_subprocess_check_exists():
    """回归：判题后端必须能识别"事件循环不支持子进程"（Windows + reload 的坑）。

    真实故障：`python run.py` 默认开 reload，uvicorn 因此在 Windows 上选用
    SelectorEventLoop；该循环不支持 create_subprocess_exec，判题直接抛
    NotImplementedError，所有提交 500。
    """
    from app.judge.subprocess_backend import loop_supports_subprocess

    ok, msg = loop_supports_subprocess()
    assert isinstance(ok, bool)
    if not ok:
        # 不支持时必须给出可操作的指引，而不是空字符串
        assert "run.py" in msg or "Proactor" in msg


async def test_api_timestamps_carry_utc_marker(seeded_client):
    """回归：API 输出的时间必须带时区标记（Z 后缀）。

    真实缺陷：库里存 naive UTC，API 原样输出 "2026-10-13T09:33:40"。
    前端 `new Date()` 把无时区字符串按**本地时间**解析，东八区用户看到
    刚提交的记录显示"8 小时前"——全站时间统一偏 8 小时。
    修法见 models.UTCDateTime（读取时补 UTC 时区）。
    """
    r = await seeded_client.get("/api/submissions?limit=1")
    items = r.json()["items"]
    if items:
        ts = items[0]["created_at"]
        assert ts.endswith("Z") or "+00:00" in ts, f"时间缺时区标记: {ts}"

    r = await seeded_client.get("/api/auth/me")
    ts = r.json()["created_at"]
    assert ts.endswith("Z") or "+00:00" in ts, f"时间缺时区标记: {ts}"


async def test_utc_datetime_roundtrip_is_aware():
    """回归：ORM 读出的时间必须是 aware（带 tzinfo），否则前端会按本地时间解析。"""
    from datetime import timezone as _tz

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Problem

    async with session_scope() as db:
        row = (await db.execute(select(Problem).limit(1))).scalars().first()
        if row is None:
            return
        assert row.created_at.tzinfo is not None, "读出的时间必须是 aware"
        assert row.created_at.utcoffset() == _tz.utc.utcoffset(None), "时区必须是 UTC"


async def test_health_available_on_both_paths(auth_client):
    """回归：/health 与 /api/health 都必须可用。

    真实缺陷：前端 API 层统一加 /api 前缀（http.get('/health') → /api/health），
    而后端只注册了裸 /health → 设置页"服务健康"永远显示异常并弹出 Not Found。
    探针路径不能改（Docker/compose/CI 都依赖），因此两个路径都要保留。
    """
    r = await auth_client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    r = await auth_client.get("/api/health")
    assert r.status_code == 200, f"/api/health 不可用（设置页会误报异常）: {r.status_code}"
    assert r.json()["db"] is True


# ================================================== 幂等与并发保护
async def test_idempotency_key_returns_same_submission(seeded_client):
    """清单 N3：同一幂等键重复提交，返回同一条记录。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][0]["id"]
    payload = {
        "problem_id": pid,
        "code": "print(42)\n",
        "judge_mode": "caller",
        "verdict": "WA",
        "run_agents": False,
    }
    h = {"Idempotency-Key": "fixed-key-001"}
    first = await seeded_client.post("/api/submissions", json=payload, headers=h)
    second = await seeded_client.post("/api/submissions", json=payload, headers=h)
    assert first.json()["submission"]["id"] == second.json()["submission"]["id"]
    assert second.json()["deduplicated"] is True


async def test_dedupe_window_reuses_recent_submission(seeded_client):
    """清单 N2：窗口期内同题同代码不重复触发 Agent。"""
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][1]["id"]
    payload = {
        "problem_id": pid,
        "code": "print(999)\n",
        "judge_mode": "caller",
        "verdict": "WA",
        "run_agents": False,
        "dedupe": True,
    }
    first = await seeded_client.post("/api/submissions", json=payload)
    second = await seeded_client.post("/api/submissions", json=payload)
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert first.json()["submission"]["id"] == second.json()["submission"]["id"]

    # 显式关闭去重则应新建记录
    third = await seeded_client.post("/api/submissions", json={**payload, "dedupe": False})
    assert third.json()["deduplicated"] is False
    assert third.json()["submission"]["id"] != first.json()["submission"]["id"]


async def test_different_code_not_deduped(seeded_client):
    r = await seeded_client.get("/api/problems")
    pid = r.json()["items"][2]["id"]
    base = {"problem_id": pid, "judge_mode": "caller", "verdict": "WA", "run_agents": False}
    a = await seeded_client.post("/api/submissions", json={**base, "code": "print(1)\n"})
    b = await seeded_client.post("/api/submissions", json={**base, "code": "print(2)\n"})
    assert a.json()["deduplicated"] is False
    assert b.json()["deduplicated"] is False
    assert a.json()["submission"]["id"] != b.json()["submission"]["id"]


# ============================================================ 任务对账
async def test_reconcile_marks_orphan_tasks_interrupted():
    """清单 N5：进程重启后残留 running 任务应被标记 interrupted。"""
    from app.db import session_scope
    from app.models import BackgroundTask
    from app.services import reconcile_tasks

    async with session_scope() as db:
        db.add(
            BackgroundTask(
                id="orphan-task-1",
                user_id="u1",
                kind="agent_pipeline",
                status="running",
                progress=0.5,
            )
        )
        db.add(
            BackgroundTask(
                id="orphan-task-2",
                user_id="u1",
                kind="agent_pipeline",
                status="pending",
            )
        )
    n = await reconcile_tasks()
    assert n >= 2

    async with session_scope() as db:
        a = await db.get(BackgroundTask, "orphan-task-1")
        b = await db.get(BackgroundTask, "orphan-task-2")
        assert a.status == "interrupted"
        assert b.status == "interrupted"
        assert "启动对账" in a.error


# ============================================================== 写锁
async def test_write_stats_counts_transactions():
    """写锁观测：写事务应被统计，便于压测断言。"""
    from app.db import session_scope
    from app.models import User
    from app.security import hash_password

    reset_write_stats()
    async with session_scope() as db:
        db.add(
            User(
                username=f"wtest-{asyncio.get_event_loop().time()}",
                password_hash=hash_password("x" * 8),
            )
        )
    stats = write_stats()
    assert stats["write_tx"] >= 1


async def test_concurrent_writes_no_lock_error():
    """并发写不再触发 database is locked（F01 的核心主张）。"""
    import uuid as _uuid

    from app.db import session_scope
    from app.models import Tag

    async def write_one(i: int) -> None:
        async with session_scope() as db:
            db.add(Tag(name=f"conc-{_uuid.uuid4().hex[:8]}-{i}"))

    await asyncio.gather(*[write_one(i) for i in range(20)])

    async with session_scope() as db:
        from sqlalchemy import func, select

        count = (
            await db.execute(select(func.count()).select_from(Tag).where(Tag.name.like("conc-%")))
        ).scalar_one()
        assert count >= 20


# ========================================================== Agent 自检
def test_registry_selfcheck_passes():
    from app.agents import validate_registry

    assert validate_registry() == []


def test_registry_selfcheck_detects_missing_schema(monkeypatch):
    """自检必须能发现问题，而不是永远返回空。"""
    from app.agents import validate_registry
    from app.schemas import SCHEMA_BY_AGENT

    broken = dict(SCHEMA_BY_AGENT)
    broken.pop("judge", None)
    monkeypatch.setattr("app.schemas.SCHEMA_BY_AGENT", broken)
    problems = validate_registry()
    assert any("judge" in p for p in problems)
