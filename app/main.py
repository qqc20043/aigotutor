"""FastAPI 应用入口。

启动：
    python -m uvicorn app.main:app --reload --port 8000
或：
    python run.py
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .agents import AGENTS, validate_registry
from .api import (
    admin,
    agent,
    data,
    mastery,
    operations,
    problems,
    recommend,
    review,
    submissions,
    users,
)
from .config import settings
from .core.cache import cache_stats
from .core.tasks import cleanup
from .core.tasks import stats as task_stats
from .db import init_db, write_stats
from .judge import describe_backend
from .middleware import RateLimitMiddleware, rate_limit_stats
from .observability import (
    ObservabilityMiddleware,
    metrics_endpoint,
    metrics_snapshot,
    setup_logging,
)
from .services import reconcile_tasks

setup_logging()
logger = logging.getLogger("aigotutor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动自检：Agent 注册表与结构化输出契约必须对齐，否则降级链可能断
    problems = validate_registry()
    if problems:
        for p in problems:
            logger.error("Agent 注册表自检失败: %s", p)
        raise RuntimeError(f"Agent 注册表自检失败（{len(problems)} 项），拒绝启动")

    await init_db()
    # 对账：上次进程异常退出会留下 running 幽灵任务
    reconciled = await reconcile_tasks()
    if reconciled:
        logger.warning("任务对账：%d 个残留任务已标记为 interrupted", reconciled)
    judge_info = describe_backend()
    logger.info(
        "AIgoTutor 启动完成：db=%s provider=%s agents=%d judge=%s",
        settings.DB_PATH,
        settings.DEFAULT_PROVIDER,
        len(AGENTS),
        judge_info["backend"],
    )
    if not judge_info["available"]:
        # reason 会带上具体原因（缺运行时 / 事件循环不支持子进程等），
        # 避免"看起来正常、一提交就失败"的隐蔽故障
        logger.warning("判题后端不可用：%s（提交将无法真实判题）", judge_info["reason"])
    yield
    logger.info("AIgoTutor 已关闭")


app = FastAPI(
    title="AIgoTutor · 多智能体刷题平台",
    version="0.1.0",
    description=(
        "5 个 Agent（推荐/题解/评测/学习状态/路线图）+ 统一基类 + 结构化输出 + "
        "规则引擎降级 + FSRS 间隔重复 + 后台任务与 SSE 流式。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 白名单来自 AIGOTUTOR_CORS_ORIGINS，默认空 = 不放行任何跨源：
    # 前端由后端同源托管（/），开发模式走 vite 代理。此前 "*" + credentials
    # 的组合会让 Starlette 回显任意 Origin，等效对所有站点放开跨源。
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 中间件顺序（Starlette 规则：最后 add 的最先执行，即最外层）：
#   ObservabilityMiddleware（最外层）-> RateLimitMiddleware -> CORS -> 路由
#
# 这个顺序是有意为之：
#   1. 可观测性在最外层，才能把"被限流拒绝的请求"也计入指标 ——
#      否则 429 计数永远是 0，而这恰恰是最需要监控的指标；
#   2. 限流在 CORS 之外，被拒请求无需走 CORS 处理与业务逻辑，省资源。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(ObservabilityMiddleware)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    """Prometheus 抓取端点（不暴露在 OpenAPI 文档里，避免噪音）。

    访问控制：设置 AIGOTUTOR_METRICS_TOKEN 后必须携带（Bearer 头或
    ?token=）；未设置时仅允许 loopback 访问 —— 运营指标不对外网暴露。
    """
    token = settings.METRICS_TOKEN
    if token:
        supplied = request.query_params.get("token", "")
        if not supplied:
            auth = request.headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        if supplied != token:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
    elif request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        # 未配置 token 的部署（默认）：只允许本机抓取
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await metrics_endpoint(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


# ① 用户与配置 ② 题库 ③ 提交评测 ④ 推荐路线图 ⑤ 复习 ⑥ 掌握度
# ⑦ Agent 与任务 ⑧ 数据 ⑨ 运营（导入/报告/打卡/排行/通知）⑩ 管理后台
for router in (
    users.router,
    users.config_router,
    problems.router,
    problems.testcase_router,
    submissions.router,
    recommend.router,
    review.router,
    mastery.router,
    mastery.wrong_router,
    agent.router,
    agent.task_router,
    data.router,
    data.health_router,
    operations.router,
    operations.report_router,
    operations.checkin_router,
    operations.rank_router,
    operations.notif_router,
    admin.router,
):
    app.include_router(router)


@app.get("/api/stats/global", tags=["⑧ 数据"])
async def global_stats():
    """全局运行态：任务、缓存、写锁、限流、指标、配置。"""
    return {
        "provider": settings.DEFAULT_PROVIDER,
        "model": settings.DEFAULT_MODEL,
        "tasks": task_stats(),
        "cache": cache_stats(),
        "write": write_stats(),
        "rate_limit": rate_limit_stats(),
        "metrics": metrics_snapshot(),
    }


# ------------------------------------------------------------- 前端静态托管
# 必须放在所有 API 路由之后：否则会抢占 /api 路径。
# 前端用 hash 路由（#/xxx），因此不需要 SPA history 回退规则，
# 只要把 index.html 与 assets 提供给根路径即可。
_web_dist = settings.WEB_DIST
if settings.SERVE_FRONTEND and _web_dist.is_dir() and (_web_dist / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
    logger.info("前端静态托管已启用：%s", _web_dist)
else:

    @app.get("/", include_in_schema=False)
    async def _no_web():
        """未构建前端时给出明确指引，而不是 404 让人困惑。"""
        return {
            "app": settings.APP_NAME,
            "hint": "前端未构建。请在 web/ 目录执行 `pnpm install && pnpm build`，"
            "或直接用 /docs 调试 API。",
            "docs": "/docs",
            "web_dist": str(_web_dist),
            "expected_index": str(_web_dist / "index.html"),
        }


@app.post("/api/stats/cleanup", tags=["⑧ 数据"])
async def cleanup_tasks(max_age_seconds: float = 3600.0):
    removed = await cleanup(max_age_seconds)
    return {"ok": True, "removed": removed}
