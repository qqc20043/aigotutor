"""判题服务：测试用例管理 + 真实沙箱判题 + 提交去重。

把 app/judge 的沙箱能力接到业务层：
  * 从 testcases 表取用例（含隐藏用例）；
  * 调用 JudgeBackend 真实执行；
  * 判题结果写回 Submission；
  * 幂等键 + 代码指纹去重（清单 N2/N3）。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .judge import JudgeRequest, get_backend
from .judge import TestCase as JudgeTestCase
from .models import Problem, Submission, TestCase

# 去重检查的互斥锁。
#
# 为什么需要它：去重语义是"窗口期内同用户同题同代码复用记录"，
# 无法用数据库唯一约束表达（窗口是时间条件）。而"先查后插"在并发下
# 会让所有请求同时查不到、各自插入 —— 压测实测 10 个并发相同提交
# 产生了 10 条记录，去重完全失效。
#
# 用一把锁把"检查 + 插入"变成临界区即可根治；因为只锁这一小段，
# 不影响其他请求的并发度。锁与事件循环绑定，循环变化时自动重建。
_dedupe_lock: asyncio.Lock | None = None
_dedupe_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_dedupe_lock() -> asyncio.Lock:
    global _dedupe_lock, _dedupe_lock_loop
    loop = asyncio.get_running_loop()
    if _dedupe_lock is None or _dedupe_lock_loop is not loop:
        _dedupe_lock = asyncio.Lock()
        _dedupe_lock_loop = loop
    return _dedupe_lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def code_fingerprint(code: str) -> str:
    """代码指纹：用于同用户同题短时去重。"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:32]


# ============================================================== 测试用例
async def list_testcases(db: AsyncSession, problem_id: str) -> list[TestCase]:
    """列出测试用例（含隐藏用例的记录，内容由 API 层决定是否回显）。

    注意：这里不过滤隐藏用例的记录，只过滤内容。调用方需要知道"共 N 个测试点"
    才能展示进度，但隐藏用例的具体输入输出不能下发（否则用户可打印期望输出
    骗过评测）。
    """
    stmt = select(TestCase).where(TestCase.problem_id == problem_id).order_by(TestCase.order_no)
    return list((await db.execute(stmt)).scalars().all())


async def list_all_testcases(db: AsyncSession, problem_id: str) -> list[TestCase]:
    """判题用：需要完整内容。"""
    return await list_testcases(db, problem_id)


async def count_testcases(db: AsyncSession, problem_id: str) -> int:
    stmt = select(func.count()).select_from(TestCase).where(TestCase.problem_id == problem_id)
    return int((await db.execute(stmt)).scalar_one())


async def replace_testcases(
    db: AsyncSession, problem_id: str, cases: Sequence[dict[str, Any]]
) -> list[TestCase]:
    """整批替换某题的测试用例（导入场景最常用）。"""
    await db.execute(delete(TestCase).where(TestCase.problem_id == problem_id))
    rows: list[TestCase] = []
    for i, c in enumerate(cases[:200], start=1):
        row = TestCase(
            problem_id=problem_id,
            order_no=i,
            input_data=str(c.get("input", c.get("input_data", ""))),
            expected_output=str(c.get("output", c.get("expected_output", ""))),
            hidden=bool(c.get("hidden", True)),
            score=int(c.get("score", 10)),
            note=str(c.get("note", ""))[:200],
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def add_testcase(db: AsyncSession, problem_id: str, data: dict[str, Any]) -> TestCase:
    stmt = select(func.max(TestCase.order_no)).where(TestCase.problem_id == problem_id)
    max_no = (await db.execute(stmt)).scalar_one()
    row = TestCase(
        problem_id=problem_id,
        order_no=int(max_no or 0) + 1,
        input_data=str(data.get("input", "")),
        expected_output=str(data.get("output", "")),
        hidden=bool(data.get("hidden", True)),
        score=int(data.get("score", 10)),
        note=str(data.get("note", ""))[:200],
    )
    db.add(row)
    await db.flush()
    return row


async def delete_testcase(db: AsyncSession, problem_id: str, case_id: str) -> bool:
    stmt = delete(TestCase).where(TestCase.problem_id == problem_id, TestCase.id == case_id)
    result = await db.execute(stmt)
    return result.rowcount > 0


# ================================================================== 判题
async def judge_submission_in_sandbox(
    db: AsyncSession, problem: Problem, language: str, code: str
) -> dict[str, Any]:
    """真实沙箱判题。返回可直接写入 Submission 的字段。"""
    cases = await list_all_testcases(db, problem.id)
    if not cases:
        return {
            "verdict": "IE",
            "passed_cases": 0,
            "total_cases": 0,
            "runtime_ms": 0,
            "memory_kb": 0,
            "failed_case": "",
            "judge_detail": {
                "message": "该题尚未配置测试用例，无法判题",
                "hint": "先通过 POST /api/problems/{id}/testcases 添加用例",
            },
            "judged_by": "sandbox",
        }

    backend = get_backend()
    req = JudgeRequest(
        language=language,
        code=code,
        cases=[
            JudgeTestCase(
                id=c.id,
                input_data=c.input_data,
                expected_output=c.expected_output,
                hidden=c.hidden,
            )
            for c in cases
        ],
        time_limit_ms=problem.time_limit_ms,
        memory_limit_mb=problem.memory_limit_mb,
        problem_id=problem.id,
    )
    result = await backend.judge(req)

    # 指标：判题结果分布（AC/WA/TLE 占比是判题系统最关键的观测项）
    from .observability import record_judge_metric

    record_judge_metric(result.verdict)

    first_fail = result.first_failure()
    failed_case = ""
    if first_fail is not None:
        # 只有非隐藏用例才把具体输入回给用户，隐藏用例仅给编号。
        # （旧版这里写的是 `not first_fail.case_id.startswith("")` —— 空串的
        # startswith 恒为 True，条件恒假，整块成了死代码，failed_case 永远为空。）
        case_obj = next((c for c in cases if c.id == first_fail.case_id), None)
        if case_obj is not None and not case_obj.hidden:
            failed_case = (
                f"[用例 {case_obj.order_no}]\n输入:\n{case_obj.input_data}\n"
                f"期望:\n{case_obj.expected_output}\n实际:\n{first_fail.stdout}"
            )
        else:
            failed_case = f"[隐藏用例 {case_obj.order_no if case_obj else '?'}] 未通过"

    return {
        "verdict": result.verdict,
        "passed_cases": result.passed,
        "total_cases": result.total,
        "runtime_ms": result.time_ms,
        "memory_kb": result.memory_kb,
        "failed_case": failed_case,
        "judge_detail": {
            "backend": result.backend,
            "message": result.message,
            "score": result.score,
            "compile_output": result.compile_output[:4000],
            "cases": [
                {
                    "case_id": c.case_id,
                    "verdict": c.verdict,
                    "time_ms": c.time_ms,
                    "memory_kb": c.memory_kb,
                    "message": c.message,
                    # 隐藏用例不回显 stdout，防止用户据此伪造输出
                    "stdout": c.stdout if not _is_hidden(c.case_id, cases) else "",
                }
                for c in result.cases
            ],
        },
        "judged_by": "sandbox",
    }


def _is_hidden(case_id: str, cases: Sequence[TestCase]) -> bool:
    for c in cases:
        if c.id == case_id:
            return c.hidden
    return True


# ============================================================== 提交去重
async def find_recent_duplicate(
    db: AsyncSession, user_id: str, problem_id: str, code_hash: str
) -> Submission | None:
    """同用户 + 同题 + 同代码 + 窗口期内 -> 视为重复提交。

    目的（清单 N2）：防止前端连点或脚本狂刷触发多次 Agent 链路，白烧 token。
    用 first() 而非 scalar_one_or_none()：并发竞争窗口里可能出现多行，
    此时取最新一条即可，不该让整个请求 500。

    注意：单独调用本函数**不保证**并发安全（check-then-act 有竞态），
    提交路径应使用 check_and_reserve()。
    """
    window = settings.DEDUP_WINDOW_SECONDS
    if window <= 0:
        return None
    cutoff = _now() - timedelta(seconds=window)
    stmt = (
        select(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.problem_id == problem_id,
            Submission.code_hash == code_hash,
            Submission.created_at >= cutoff,
        )
        .order_by(Submission.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def check_and_reserve(
    db: AsyncSession,
    *,
    user_id: str,
    problem_id: str,
    code_hash: str,
    idempotency_key: str | None,
    create: Any,
    dedupe: bool = True,
) -> tuple[Submission | None, str]:
    """在临界区内完成"去重检查 + 创建"，返回 (既有记录或新建记录, 结果类型)。

    结果类型：
      "idempotent" —— 命中幂等键，返回既有记录
      "duplicate"  —— 窗口期内同代码，返回既有记录
      "created"    —— 新建记录

    为什么必须放进临界区：去重是时间窗口语义，数据库唯一约束无法表达；
    而"先查后插"在并发下会让所有请求同时查不到、各自插入（压测实测
    10 个并发相同提交产生 10 条记录）。用锁把这一段串行化即可根治。

    dedupe=False 时**跳过窗口去重**（但仍校验幂等键）—— 调用方需要
    "同一份代码重复提交也要各留一条记录"（如对比不同判题结果）时用。

    create 是一个 async 回调（无参、返回已 flush 的 Submission），
    由调用方提供，从而把"创建时带哪些字段"的决策留在业务层。
    """
    lock = _get_dedupe_lock()
    async with lock:
        # 1) 幂等键优先（与 dedupe 开关无关：显式幂等键意味着调用方要求复用）
        if idempotency_key:
            existing = await find_by_idempotency_key(db, user_id, idempotency_key)
            if existing is not None:
                return existing, "idempotent"

        # 2) 窗口期同代码去重
        if dedupe and settings.DEDUP_WINDOW_SECONDS > 0:
            dup = await find_recent_duplicate(db, user_id, problem_id, code_hash)
            if dup is not None:
                return dup, "duplicate"

        # 3) 新建
        created = await create()
        return created, "created"


async def find_by_idempotency_key(db: AsyncSession, user_id: str, key: str) -> Submission | None:
    """按幂等键查已存在的提交（清单 N3）。"""
    if not key:
        return None
    stmt = (
        select(Submission)
        .where(Submission.user_id == user_id, Submission.idempotency_key == key)
        .order_by(Submission.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def judge_availability() -> dict[str, Any]:
    """判题后端可用性，供健康检查与管理接口使用。"""
    backend = get_backend()
    info = backend.availability()
    info["configured"] = settings.JUDGE_BACKEND
    info["timeout_s"] = settings.JUDGE_TIMEOUT_SECONDS
    info["memory_mb"] = settings.JUDGE_MEMORY_MB
    return info
