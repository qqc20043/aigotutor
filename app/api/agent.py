"""⑦ Agent 与后台任务 —— 6 个接口（含 SSE 流式与中断）。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ..agents import AGENTS, CORE_AGENTS
from ..core.agent import AgentContext
from ..core.cache import cache_clear, cache_stats
from ..core.pipeline import practice_pipeline, run_agent
from ..core.tasks import (
    cancel,
    events,
    list_tasks,
    sse_format,
    submit,
    task_owner,
    wait,
)
from ..core.tasks import (
    stats as task_stats,
)
from ..core.tasks import (
    task_detail as task_detail_snapshot,
)
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import (
    agent_run_stats,
    build_tag_stats,
    get_problem,
    get_problem_tags,
    list_persisted_tasks,
    list_problems,
    list_tags,
    log_agent_run,
    mastery_scores_map,
    persist_task,
    recent_submissions,
)
from .schemas import AgentRunIn, PipelineIn

router = APIRouter(prefix="/api/agent", tags=["⑦ Agent 与任务"])


@router.get("", response_model=dict)
async def list_agents(user: UserDep):
    """28 列出可用 Agent 及其契约（5 个核心 + 1 个标注）。"""
    return {
        "core_agents": CORE_AGENTS,
        "agents": [
            {
                "name": name,
                "schema": agent.schema.__name__,
                "cacheable": agent.cacheable,
                "has_rule_fallback": agent.run_rule(AgentContext()) is not None
                if name != "difficulty"
                else True,
            }
            for name, agent in AGENTS.items()
        ],
    }


@router.post("/run", response_model=dict)
async def run_single(body: AgentRunIn, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """29 单 Agent 直跑（调试与评测集用）。"""
    if body.agent not in AGENTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知 Agent: {body.agent}")
    ctx = AgentContext(
        user_id=user.id,
        provider=cfg["provider"],
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        temperature=cfg["temperature"],
        use_cache=body.use_cache,
        data=dict(body.payload),
    )
    result = await run_agent(body.agent, ctx)
    await log_agent_run(db, {**result.to_dict(), "user_id": user.id, "agent": body.agent})
    return result.to_dict()


@router.post("/stream", response_class=StreamingResponse)
async def stream_agent(
    body: AgentRunIn,
    user: UserDep,
    cfg: ModelConfigDep,
):
    """单 Agent token 级流式（SSE）。

    事件序列：
      start -> token* -> field* -> done
    失败时以 degraded + done 收尾，保证前端总能拿到终态。

    注意：这个接口不写数据库（SSE 连接期间持有 session 会长时间占连接），
    需要落库请用 /api/agent/run。
    """
    if body.agent not in AGENTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知 Agent: {body.agent}")

    ctx = AgentContext(
        user_id=user.id,
        provider=cfg["provider"],
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        temperature=cfg["temperature"],
        use_cache=body.use_cache,
        data=dict(body.payload),
    )
    agent = AGENTS[body.agent]

    async def gen():
        yield sse_format({"event": "open", "data": {"agent": body.agent}})
        try:
            async for evt in agent.run_stream(ctx):
                yield sse_format({"event": evt.get("event", "message"), "data": evt})
        except asyncio.CancelledError:
            # 客户端断开：立即停止，不再产生 token 消耗
            yield sse_format({"event": "closed", "data": {"reason": "客户端断开"}})
            raise
        except Exception as e:  # 双保险，绝不让流裸崩
            yield sse_format({"event": "error", "data": {"error": f"{type(e).__name__}: {e}"}})
        yield sse_format({"event": "closed", "data": {"agent": body.agent}})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/pipeline", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
async def run_pipeline(body: PipelineIn, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """30 提交后完整链路（异步后台任务，立刻返回 task_id）。"""
    problem = await get_problem(db, body.submission.problem_id)
    if problem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")

    tag_map = await get_problem_tags(db, [problem.id])
    tags = await list_tags(db) if body.include_roadmap else []
    problems, _ = await list_problems(db, offset=0, limit=20)
    cand_tag_map = await get_problem_tags(db, [p.id for p in problems])
    recent = await recent_submissions(db, user.id, limit=20)
    tag_stats = await build_tag_stats(db, user.id, 30)
    mastery = await mastery_scores_map(db, user.id)

    submission_payload = body.submission.model_dump()
    submission_payload.update(
        user_id=user.id,
        total_submissions=sum(int(v.get("attempts", 0)) for v in tag_stats.values()),
        ac_count=sum(int(v.get("ac_count", 0)) for v in tag_stats.values()),
        recent_verdicts=[s.verdict for s in recent],
        recent_problem_ids=[s.problem_id for s in recent],
    )
    problem_payload = {
        "id": problem.id,
        "title": problem.title,
        "statement": problem.statement,
        "difficulty_score": problem.difficulty_score,
        "tags": [t["name"] for t in tag_map.get(problem.id, [])],
    }
    candidates = [
        {
            "id": p.id,
            "title": p.title,
            "difficulty": p.difficulty,
            "difficulty_score": p.difficulty_score,
            "tags": [t["name"] for t in cand_tag_map.get(p.id, [])],
        }
        for p in problems
    ]
    tag_payload = [{"id": t.id, "name": t.name, "prereq_ids": t.prereq_ids} for t in tags]

    async def pipeline(task):
        # 关键：后台任务必须在自己的 session 里写库。
        # 请求级 session 会在响应返回后关闭，复用它会导致
        # "this session is provisioning a new connection; concurrent operations"
        # 或任务写入静默失败。
        from ..db import session_scope

        async def on_result(name: str, result: dict):
            async with session_scope() as task_db:
                await persist_task(
                    task_db,
                    {
                        "id": task.id,
                        "user_id": user.id,
                        "kind": "agent_pipeline",
                        "status": "running",
                        "progress": task.progress,
                        "stage": task.stage,
                        "payload": {"submission_id": body.submission.problem_id},
                    },
                )
                await log_agent_run(
                    task_db, {**result, "user_id": user.id, "task_id": task.id, "agent": name}
                )

        summary = await practice_pipeline(
            task,
            user_id=user.id,
            config=cfg,
            submission=submission_payload,
            problem=problem_payload,
            tag_stats=tag_stats,
            tags=tag_payload or None,
            candidates=candidates,
            mastery_scores=mastery,
            weeks=body.weeks,
            on_result=on_result,
        )
        async with session_scope() as task_db:
            await persist_task(
                task_db,
                {
                    "id": task.id,
                    "user_id": user.id,
                    "kind": "agent_pipeline",
                    "status": "done",
                    "progress": 1.0,
                    "stage": "finished",
                    "result": summary,
                },
            )
        return summary

    task = submit(
        "agent_pipeline",
        pipeline,
        user_id=user.id,
        payload={"problem_id": problem.id, "include_roadmap": body.include_roadmap},
    )
    await persist_task(
        db,
        {
            "id": task.id,
            "user_id": user.id,
            "kind": "agent_pipeline",
            "status": "pending",
            "payload": {"problem_id": problem.id},
        },
    )
    return task.snapshot()


@router.get("/stream", response_class=StreamingResponse)
async def stream_events(
    user: UserDep,
    task_id: str = Query(min_length=1),
    from_index: int = 0,
):
    """31 SSE 事件流：进度 / 单 Agent 结果 / 完成 / 取消。

    必须登录且只能是自己的任务：事件里会流出该用户的代码与 Agent 输出，
    此前这里完全没有鉴权（匿名可订阅任意 task_id）。前端用 fetch 消费
    SSE（stream.ts），Authorization header 天然可用，无需 token 查询参数。
    """

    owner = task_owner(task_id)
    if owner is not None and owner != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")

    async def gen():
        yield sse_format({"event": "open", "data": {"task_id": task_id}})
        async for evt in events(task_id, from_index):
            yield sse_format(evt)
            if evt["event"] in ("closed", "error"):
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/stats", response_model=dict)
async def agent_stats(db: DbSession, user: UserDep, days: int = Query(default=7, ge=1, le=90)):
    """32 Agent 运行统计：降级率 / 失败率 / token / P95 延迟。"""
    return {
        "db": await agent_run_stats(db, user.id, days),
        "runtime": task_stats(),
        "cache": cache_stats(),
    }


@router.post("/cache/clear", response_model=dict)
async def clear_cache(user: UserDep):
    cache_clear()
    return {"ok": True, "message": "缓存已清空"}


task_router = APIRouter(prefix="/api/tasks", tags=["⑦ Agent 与任务"])


@task_router.get("", response_model=dict)
async def list_tasks_api(db: DbSession, user: UserDep):
    """33 任务列表（运行态 + 持久化）。

    运行态列表已合并 Redis 镜像：多 worker 部署下也能看到"别的 worker
    创建的任务"，不再只有本进程的那几条。
    """
    return {"runtime": list_tasks(user.id), "persisted": await list_persisted_tasks(db, user.id)}


@task_router.get("/{task_id}", response_model=dict)
async def task_detail(task_id: str, user: UserDep):
    """34 任务详情（轮询用）。

    必须走 task_detail() 而不是 get_task()：后者只查本进程字典，
    多 worker 下轮询被负载均衡到别的 worker 会随机 404。
    只能查自己的任务（结果含本人代码与 Agent 输出）。
    """
    snapshot = task_detail_snapshot(task_id)
    owner = task_owner(task_id)
    if snapshot is None or (owner is not None and owner != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return snapshot


@task_router.post("/{task_id}/cancel", response_model=dict)
async def task_cancel(task_id: str, user: UserDep):
    """35 中断任务：立刻停止后续 Agent 调用，不再产生 token 消耗。

    任务在别的 worker 上时，cancel() 会写跨进程取消信号，由 owner 侧的
    轮询任务感知并取消本地句柄（asyncio 句柄无法跨进程传递）。
    只能取消自己的任务。
    """
    owner = task_owner(task_id)
    if owner is not None and owner != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    ok = cancel(task_id)
    return {"ok": ok, "task": task_detail_snapshot(task_id)}


@task_router.post("/{task_id}/wait", response_model=dict)
async def task_wait(task_id: str, user: UserDep, timeout: float = Query(default=60.0, le=300.0)):
    """36 同步等待任务结束（测试与脚本用）。只能等自己的任务。

    timeout 设上界（300s）：无上界时一个 ?timeout=1e6 的请求能长期占用
    worker 连接。
    """
    owner = task_owner(task_id)
    if owner is not None and owner != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    try:
        return await wait(task_id, timeout)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在") from None
