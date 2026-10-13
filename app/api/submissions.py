"""③ 提交与评测 —— 真实沙箱判题 + Agent 判错归因。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status

from ..config import settings
from ..core.pipeline import judge_submission, solve_problem
from ..deps import DbSession, ModelConfigDep, PageDep, UserDep
from ..judge_service import (
    check_and_reserve,
    code_fingerprint,
    count_testcases,
    judge_submission_in_sandbox,
)
from ..models import Submission
from ..services import (
    add_review,
    create_submission,
    get_problem,
    get_problem_tags,
    get_submission,
    list_reviews,
    list_submissions,
    log_agent_run,
    upsert_wrong_book,
)
from .schemas import JudgeOut, SubmissionIn, SubmissionOut

router = APIRouter(prefix="/api/submissions", tags=["③ 提交与评测"])


def _to_out(sub) -> SubmissionOut:
    return SubmissionOut(
        id=sub.id,
        problem_id=sub.problem_id,
        language=sub.language,
        verdict=sub.verdict,
        passed_cases=sub.passed_cases,
        total_cases=sub.total_cases,
        runtime_ms=sub.runtime_ms,
        memory_kb=sub.memory_kb,
        judged_by=getattr(sub, "judged_by", "caller"),
        created_at=sub.created_at,
    )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def submit(
    body: SubmissionIn,
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    """13/27 提交代码 → 真实沙箱判题 → Agent 判错归因。

    判题有三条路径，由 body.judge_mode 控制：
      * "sandbox"（默认）：真实执行测试用例，verdict 以沙箱结论为准；
      * "caller"：沿用调用方传入的 verdict（离线演示、外部判题机对接）；
      * "auto"：有测试用例就走沙箱，否则退回 caller。

    提交记录先落库（verdict=PENDING），再异步补判题结果 —— 这样前端能立刻
    拿到 submission_id，判题与 Agent 归因在后台继续。
    """
    problem = await get_problem(db, body.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")

    fingerprint = code_fingerprint(body.code)

    # ---- 判题模式决策（在临界区外完成：判题可能耗秒级，不能占着锁） ----
    case_count = await count_testcases(db, body.problem_id)
    mode = body.judge_mode
    if mode == "auto":
        mode = "sandbox" if case_count > 0 else "caller"

    verdict_fields: dict[str, Any]
    if mode == "sandbox":
        verdict_fields = await judge_submission_in_sandbox(db, problem, body.language, body.code)
    else:
        verdict_fields = {
            "verdict": body.verdict,
            "passed_cases": body.passed_cases,
            "total_cases": body.total_cases,
            "runtime_ms": body.runtime_ms,
            "memory_kb": body.memory_kb,
            "failed_case": body.failed_case,
            "judge_detail": body.judge_detail,
            "judged_by": "caller",
        }
        # auto 模式因"该题没有测试用例"回退到 caller 时，必须说明原因 ——
        # 否则用户会误以为调用方传来的 verdict 是判题机的真实结论。
        # 注意判断的是**请求时的原始模式**（body.judge_mode）：上面的
        # mode 变量此刻已被改写为 "caller"，拿它比较是恒假的死代码。
        if case_count == 0 and body.judge_mode == "auto":
            verdict_fields["judge_detail"] = {
                **body.judge_detail,
                "message": "该题未配置测试用例，已回退为调用方判定",
            }

    # ---- 幂等 + 去重 + 创建：必须在同一临界区内完成 ----
    # 三者都是"先查后写"，分开做会有竞态：压测实测 10 个并发相同提交
    # 全部新建记录，去重完全失效（check-then-act 的经典问题）。
    # check_and_reserve 用一把锁把这一段串行化。
    # verdict_fields 已在上方就绪，闭包捕获的是最终值，无晚绑定风险。
    async def _create() -> Submission:
        return await create_submission(
            db,
            {
                "user_id": user.id,
                "problem_id": body.problem_id,
                "language": body.language,
                "code": body.code,
                "idempotency_key": idempotency_key,
                "code_hash": fingerprint,
                **verdict_fields,
            },
        )

    sub, outcome = await check_and_reserve(
        db,
        user_id=user.id,
        problem_id=body.problem_id,
        code_hash=fingerprint,
        idempotency_key=idempotency_key,
        dedupe=body.dedupe,
        create=_create,
    )
    assert sub is not None  # check_and_reserve 在 create 失败时会抛异常

    if outcome != "created":
        return {
            "submission": _to_out(sub).model_dump(),
            "judge": None,
            "judge_mode": mode,
            "verdict": sub.verdict,
            "judge_detail": {},
            "deduplicated": True,
            "reason": (
                "命中幂等键，返回已存在的提交"
                if outcome == "idempotent"
                else (
                    f"{settings.DEDUP_WINDOW_SECONDS}s 内已有相同代码的提交，"
                    "已复用该记录（避免重复消耗模型额度）"
                )
            ),
        }

    verdict = verdict_fields["verdict"]

    result_payload: dict = {
        "submission": _to_out(sub).model_dump(),
        "judge": None,
        "judge_mode": mode,
        "verdict": verdict,
        "judge_detail": verdict_fields.get("judge_detail", {}),
        "deduplicated": False,
    }
    if verdict != "AC":
        await upsert_wrong_book(db, user.id, body.problem_id, verdict)
    if not body.run_agents:
        return result_payload

    tag_map = await get_problem_tags(db, [problem.id])
    problem_payload = {
        "id": problem.id,
        "title": problem.title,
        "statement": problem.statement,
        "difficulty_score": problem.difficulty_score,
        "tags": [t["name"] for t in tag_map.get(problem.id, [])],
    }
    judged = await judge_submission(
        user.id,
        cfg,
        {
            "verdict": verdict,
            "passed_cases": verdict_fields.get("passed_cases", 0),
            "total_cases": verdict_fields.get("total_cases", 0),
            "failed_case": verdict_fields.get("failed_case", ""),
            "judge_detail": verdict_fields.get("judge_detail", {}),
            "code": body.code,
            "language": body.language,
        },
        problem_payload,
    )
    data = judged.get("data") or {}
    await add_review(
        db,
        {
            "submission_id": sub.id,
            "kind": "judge",
            "content": data.get("explanation", ""),
            "structured": data,
            "confidence": judged.get("confidence", 0.0),
            "degraded": judged.get("degraded", False),
            "tokens": judged.get("tokens", 0),
            "latency_ms": judged.get("latency_ms", 0),
        },
    )
    await log_agent_run(db, {**judged, "user_id": user.id, "agent": "judge"})
    if data.get("root_cause") and body.verdict != "AC":
        await upsert_wrong_book(db, user.id, body.problem_id, body.verdict, data["root_cause"])

    result_payload["judge"] = JudgeOut(
        submission_id=sub.id,
        verdict=body.verdict,
        root_cause=data.get("root_cause", ""),
        explanation=data.get("explanation", ""),
        fix_suggestion=data.get("fix_suggestion", ""),
        degraded=judged.get("degraded", False),
        confidence=judged.get("confidence", 0.0),
    ).model_dump()
    return result_payload


@router.get("", response_model=dict)
async def list_submissions_api(
    db: DbSession,
    user: UserDep,
    page: PageDep,
    problem_id: str | None = None,
    verdict: str | None = None,
):
    """14/27 我的提交记录。"""
    rows, total = await list_submissions(
        db,
        user_id=user.id,
        problem_id=problem_id,
        verdict=verdict,
        offset=page.offset,
        limit=page.size,
    )
    return {
        "items": [_to_out(s).model_dump() for s in rows],
        "total": total,
        "page": page.page,
        "size": page.size,
    }


@router.get("/{submission_id}", response_model=dict)
async def submission_detail(submission_id: str, db: DbSession, user: UserDep):
    """15/27 提交详情（含 Agent 评审）。"""
    sub = await get_submission(db, submission_id)
    if sub is None or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提交不存在")
    reviews = await list_reviews(db, submission_id)
    return {
        "submission": _to_out(sub).model_dump(),
        "code": sub.code,
        "judge_detail": json.loads(sub.judge_detail or "{}"),
        "reviews": [
            {
                "id": r.id,
                "kind": r.kind,
                "content": r.content,
                "structured": json.loads(r.structured or "{}"),
                "confidence": round(r.confidence, 4),
                "degraded": r.degraded,
                "created_at": r.created_at,
            }
            for r in reviews
        ],
    }


@router.post("/{submission_id}/solution", response_model=dict)
async def generate_solution(submission_id: str, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """16/27 生成结构化题解并落库。"""
    sub = await get_submission(db, submission_id)
    if sub is None or sub.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提交不存在")
    problem = await get_problem(db, sub.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    tag_map = await get_problem_tags(db, [problem.id])
    solved = await solve_problem(
        user.id,
        cfg,
        {
            "title": problem.title,
            "statement": problem.statement,
            "input_desc": problem.input_desc,
            "output_desc": problem.output_desc,
            "samples": json.loads(problem.samples or "[]"),
            "constraints": problem.constraints,
            "tags": [t["name"] for t in tag_map.get(problem.id, [])],
            "difficulty": problem.difficulty,
            "time_limit_ms": problem.time_limit_ms,
            "preset_solution": None,
            "starter_code": sub.code if sub.verdict != "AC" else None,
        },
        language=sub.language,
    )
    data = solved.get("data") or {}
    await add_review(
        db,
        {
            "submission_id": sub.id,
            "kind": "solution",
            "content": data.get("summary", ""),
            "structured": data,
            "confidence": solved.get("confidence", 0.0),
            "degraded": solved.get("degraded", False),
            "tokens": solved.get("tokens", 0),
            "latency_ms": solved.get("latency_ms", 0),
        },
    )
    await log_agent_run(db, {**solved, "user_id": user.id, "agent": "solution"})
    return solved
