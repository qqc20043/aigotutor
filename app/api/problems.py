"""② 题库 —— 题目 + 测试用例管理。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status

from ..core.memory import estimate_tokens, memory_block_for
from ..core.pipeline import annotate_problem, generate_hint, route
from ..core.similar import find_similar
from ..deps import DbSession, ModelConfigDep, PageDep, UserDep
from ..judge_service import (
    add_testcase,
    delete_testcase,
    judge_availability,
    list_testcases,
    replace_testcases,
)
from ..services import (
    bind_tags,
    create_problem,
    get_problem,
    get_problem_tags,
    list_problems,
    list_tags,
    mastery_scores_map,
    problem_attempt_stats,
    random_problem,
)
from .schemas import (
    ProblemCreateIn,
    ProblemDetail,
    ProblemOut,
    TagOut,
    TestCaseBatchIn,
    TestCaseIn,
    TestCaseOut,
)

router = APIRouter(prefix="/api/problems", tags=["② 题库"])


def _case_out(case) -> TestCaseOut:
    """隐藏用例不回显内容。"""
    return TestCaseOut(
        id=case.id,
        order_no=case.order_no,
        hidden=case.hidden,
        score=case.score,
        note=case.note,
        input_data="" if case.hidden else case.input_data,
        expected_output="" if case.hidden else case.expected_output,
    )


def _to_out(problem, tag_names: list[str]) -> ProblemOut:
    return ProblemOut(
        id=problem.id,
        title=problem.title,
        difficulty=problem.difficulty,
        difficulty_score=problem.difficulty_score,
        difficulty_source=problem.difficulty_source,
        tags=tag_names,
        source=problem.source,
        time_limit_ms=problem.time_limit_ms,
        memory_limit_mb=problem.memory_limit_mb,
        accepted_count=problem.accepted_count,
        submit_count=problem.submit_count,
    )


@router.get("", response_model=dict)
async def list_problems_api(
    db: DbSession,
    user: UserDep,
    page: PageDep,
    difficulty: str | None = None,
    tag_id: str | None = None,
    keyword: str | None = None,
    source: str | None = None,
):
    """6/27 题库列表（分页 + 筛选）。"""
    rows, total = await list_problems(
        db,
        difficulty=difficulty,
        tag_id=tag_id,
        keyword=keyword,
        source=source,
        offset=page.offset,
        limit=page.size,
    )
    tag_map = await get_problem_tags(db, [p.id for p in rows])
    return {
        "items": [
            _to_out(p, [t["name"] for t in tag_map.get(p.id, [])]).model_dump() for p in rows
        ],
        "total": total,
        "page": page.page,
        "size": page.size,
    }


@router.get("/random", response_model=ProblemDetail)
async def random_problem_api(db: DbSession, user: UserDep, difficulty: str | None = None):
    """7/27 随机抽题。"""
    problem = await random_problem(db, difficulty)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题库为空")
    tag_map = await get_problem_tags(db, [problem.id])
    detail = _to_out(problem, [t["name"] for t in tag_map.get(problem.id, [])]).model_dump()
    detail.update(
        statement=problem.statement,
        input_desc=problem.input_desc,
        output_desc=problem.output_desc,
        samples=json.loads(problem.samples or "[]"),
        constraints=problem.constraints,
    )
    return ProblemDetail(**detail)


@router.get("/tags", response_model=list[TagOut])
async def list_tags_api(db: DbSession, user: UserDep):
    """8/27 知识点标签树。"""
    tags = await list_tags(db)
    return [
        TagOut(
            id=t.id,
            name=t.name,
            parent_id=t.parent_id,
            category=t.category,
            description=t.description,
            prereq_ids=json.loads(t.prereq_ids or "[]"),
        )
        for t in tags
    ]


@router.get("/search", response_model=dict)
async def search_problems(
    db: DbSession,
    user: UserDep,
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    """9/27 关键词检索（标题 + 题面）。"""
    rows, total = await list_problems(db, keyword=q, offset=0, limit=limit)
    tag_map = await get_problem_tags(db, [p.id for p in rows])
    return {
        "items": [
            _to_out(p, [t["name"] for t in tag_map.get(p.id, [])]).model_dump() for p in rows
        ],
        "total": total,
        "query": q,
    }


@router.get("/{problem_id}", response_model=ProblemDetail)
async def problem_detail(problem_id: str, db: DbSession, user: UserDep):
    """10/27 题目详情。"""
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    tag_map = await get_problem_tags(db, [problem.id])
    detail = _to_out(problem, [t["name"] for t in tag_map.get(problem.id, [])]).model_dump()
    detail.update(
        statement=problem.statement,
        input_desc=problem.input_desc,
        output_desc=problem.output_desc,
        samples=json.loads(problem.samples or "[]"),
        constraints=problem.constraints,
    )
    return ProblemDetail(**detail)


@router.get("/{problem_id}/similar", response_model=dict)
async def problem_similar(
    problem_id: str, db: DbSession, user: UserDep, limit: int = Query(default=5, ge=1, le=10)
):
    """相似题检索（举一反三）：FTS5 召回 + 标签/难度混合排序。

    检索层刻意零外部依赖（SQLite 内建 FTS5，trigram 分词适配中文子串），
    LLM 重排是演进方向 —— 检索可解释性优先（每条结果带"为什么相似"）。
    """
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    tag_map = await get_problem_tags(db, [problem.id])
    source = {
        "id": problem.id,
        "title": problem.title,
        "statement": problem.statement,
        "tags": [t["name"] for t in tag_map.get(problem.id, [])],
        "difficulty_score": problem.difficulty_score,
    }
    result = await find_similar(db, source, limit=limit)
    return {
        "problem_id": problem_id,
        "items": [item.__dict__ for item in result["items"]],
        "retrieval": result["retrieval"],
    }


@router.post("/{problem_id}/hint", response_model=dict)
async def problem_hint(
    problem_id: str,
    db: DbSession,
    user: UserDep,
    cfg: ModelConfigDep,
    language: str = Query(default="python", max_length=16),
    current_code: str = Query(
        default="", max_length=8000, description="用户当前代码，用于定位卡点"
    ),
):
    """渐进式解题提示：强度由该题知识点掌握度自动决定。

    强度三档（掌握度越低给越强）：
      * direction        —— 只点方向（掌握度 >= 65 或样本充足）
      * key_observation  —— 点出关键观察（掌握度 40-65）
      * pseudocode       —— 给出伪代码级骨架（掌握度 < 40 或首次接触）

    强度在服务端算定后写进 prompt，模型无权升降档 —— 这样同一用户在同一
    掌握度下拿到的提示强度是可复现、可测试的。
    """
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")

    tag_map = await get_problem_tags(db, [problem.id])
    tags = [t["name"] for t in tag_map.get(problem.id, [])]

    # 该题知识点掌握度：取最弱的一项（最短板决定该给多强的提示）
    mastery = await mastery_scores_map(db, user.id)
    weakest = min(float(mastery.get(t, 50.0)) for t in tags) if tags and mastery else 50.0

    # 该题历史提交次数：样本不足时强度会保守升档（见 rules.hint_strength_for）
    stats = await problem_attempt_stats(db, user.id, problem.id)

    # 学习记忆清单：让提示能引用用户的历史薄弱点（而不是每次从零开始）
    memory = await memory_block_for(db, user.id)

    result = await generate_hint(
        user.id,
        cfg,
        problem={
            "title": problem.title,
            "statement": problem.statement,
            "tags": tags,
            "difficulty": problem.difficulty,
            "language": language,
        },
        mastery_score=weakest,
        attempts=stats["attempts"],
        current_code=current_code,
        verdict=stats["last_verdict"],
        memory=memory,
    )
    result["memory_tokens"] = estimate_tokens(memory) if memory else 0
    return result


@router.post("", response_model=ProblemOut, status_code=status.HTTP_201_CREATED)
async def create_problem_api(
    body: ProblemCreateIn, db: DbSession, user: UserDep, cfg: ModelConfigDep
):
    """11/27 新建题目（可选两阶段难度自动标注）。"""
    data = body.model_dump()
    data["samples"] = body.samples
    data["created_by"] = user.id  # 用例写权限按创建者判定（见 _ensure_can_edit_testcases）
    if body.auto_annotate:
        annotation = await annotate_problem(
            {
                "title": body.title,
                "statement": body.statement,
                "constraints": body.constraints,
                "samples": body.samples,
                "tags": body.tags,
                "statement_len": len(body.statement),
                "tag_count": max(1, len(body.tags)),
                "algo_depth": 2,
                "constraint_tight": any(
                    x in body.constraints for x in ("10^5", "1e5", "10^9", "1e9")
                ),
                "is_constructive": "构造" in body.statement,
            },
            cfg,
            user_id=user.id,
        )
        data["difficulty"] = annotation["difficulty"]
        data["difficulty_score"] = annotation["difficulty_score"]
        data["difficulty_source"] = annotation["difficulty_source"]
        tag_names = annotation["tags"]
    else:
        tag_names = body.tags

    problem = await create_problem(db, data, tag_names=tag_names)
    if body.tags:
        await bind_tags(db, problem.id, body.tags)
    return _to_out(problem, tag_names)


@router.post("/{problem_id}/route", response_model=dict)
async def route_problem(problem_id: str, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """12/27 成本路由预览：这道题该不该走模型。"""
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    return route(
        {"statement": problem.statement, "difficulty_score": problem.difficulty_score}, cfg
    )


# ======================================================== 测试用例管理
@router.get("/{problem_id}/testcases", response_model=dict)
async def get_testcases(problem_id: str, db: DbSession, user: UserDep):
    """列出该题的测试用例（隐藏用例只回编号与状态，不回内容）。"""
    problem = await get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    cases = await list_testcases(db, problem_id)
    return {
        "problem_id": problem_id,
        "items": [_case_out(c).model_dump() for c in cases],
        "total": len(cases),
        "hidden_count": sum(1 for c in cases if c.hidden),
    }


@router.post("/{problem_id}/testcases", response_model=dict, status_code=status.HTTP_201_CREATED)
async def add_testcase_api(problem_id: str, body: TestCaseIn, db: DbSession, user: UserDep):
    """新增单个测试用例（仅题目创建者或管理员）。"""
    problem = await get_problem(db, problem_id)
    _ensure_can_edit_testcases(problem, user)
    if not body.output.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "期望输出不能为空")
    case = await add_testcase(db, problem_id, body.model_dump())
    return _case_out(case).model_dump()


@router.put("/{problem_id}/testcases", response_model=dict)
async def batch_testcases(problem_id: str, body: TestCaseBatchIn, db: DbSession, user: UserDep):
    """批量设置测试用例（replace=true 时整批替换，导入场景最常用）。

    仅题目创建者或管理员：用例含隐藏期望输出，任何登录用户可改任意题
    的用例等于能伪造判题结果（PoC 已复现）。
    """
    problem = await get_problem(db, problem_id)
    _ensure_can_edit_testcases(problem, user)
    if not body.cases:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "用例列表不能为空")
    for c in body.cases:
        if not c.output.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "每个用例的期望输出都不能为空"
            )
    if body.replace:
        rows = await replace_testcases(db, problem_id, [c.model_dump() for c in body.cases])
    else:
        rows = [await add_testcase(db, problem_id, c.model_dump()) for c in body.cases]
    return {
        "problem_id": problem_id,
        "saved": len(rows),
        "total": await _count(db, problem_id),
    }


async def _count(db: DbSession, problem_id: str) -> int:
    from ..judge_service import count_testcases

    return await count_testcases(db, problem_id)


@router.delete("/{problem_id}/testcases/{case_id}", response_model=dict)
async def delete_testcase_api(problem_id: str, case_id: str, db: DbSession, user: UserDep):
    """删除指定测试用例（仅题目创建者或管理员）。"""
    problem = await get_problem(db, problem_id)
    _ensure_can_edit_testcases(problem, user)
    ok = await delete_testcase(db, problem_id, case_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用例不存在")
    return {"ok": True, "deleted": case_id}


def _ensure_can_edit_testcases(problem, user) -> None:
    """用例写权限：题目创建者或管理员；否则 404（不泄露题目存在性）。

    created_by 为空（种子/导入的题）只有管理员可改。非创建者返回 404 而
    不是 403 —— 与"题目不存在"同响应，避免探测到他人私有题目。
    """
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    if user.is_admin or (problem.created_by and problem.created_by == user.id):
        return
    raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")


testcase_router = APIRouter(prefix="/api/judge", tags=["② 题库"])


@testcase_router.get("", response_model=dict)
async def judge_backend_info(user: UserDep):
    """判题后端能力：可用语言、隔离强度、资源限制。"""
    return await judge_availability()
