"""校验部署配置：compose YAML 可解析、Dockerfile 关键指令齐全。

本机没有 Docker，无法真正构建镜像，因此这里做静态校验 ——
至少保证配置文件语法正确、关键环境变量与阶段引用没有笔误，
避免 CI 或部署时才暴露低级错误。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    print("== 部署配置校验 ==\n")

    # ---- YAML 可解析 ----
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    compose_files = ["docker-compose.yml", "docker-compose.prod.yml"]
    for name in compose_files:
        path = ROOT / name
        check(f"{name} 存在", path.exists())
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if yaml is not None:
            try:
                data = yaml.safe_load(text)
                check(f"{name} YAML 合法", isinstance(data, dict))
                services = data.get("services") or {}
                check(f"{name} 定义了 services", bool(services), f"{list(services)}")
                for svc, cfg in services.items():
                    check(
                        f"{name}/{svc} 有镜像或构建配置",
                        "image" in cfg or "build" in cfg,
                    )
            except Exception as e:
                check(f"{name} YAML 合法", False, str(e))
        else:
            print(f"  [SKIP] {name} YAML 解析（未安装 pyyaml）")

    # ---- 生产 compose 的关键设置 ----
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    check("生产用 docker 判题后端", "AIGOTUTOR_JUDGE_BACKEND: docker" in prod)
    check("生产开启限流", 'AIGOTUTOR_RATE_LIMIT: "1"' in prod)
    check("生产用 JSON 日志", 'AIGOTUTOR_LOG_JSON: "1"' in prod)
    check("生产关闭 reload", 'RELOAD: "0"' in prod)
    check("生产有健康检查", "healthcheck:" in prod)
    check("生产有资源限制", "limits:" in prod)

    dev = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("开发开启 reload", 'RELOAD: "1"' in dev)
    check("开发用 mock provider", "AIGOTUTOR_PROVIDER: mock" in dev)

    # ---- Dockerfile ----
    dockerfile = ROOT / "Dockerfile"
    check("Dockerfile 存在", dockerfile.exists())
    if dockerfile.exists():
        df = dockerfile.read_text(encoding="utf-8")
        check("多阶段构建", df.count("FROM ") >= 2)
        check("有前端构建阶段", "AS web-builder" in df)
        check("有运行阶段", "AS runtime" in df)
        check("非 root 运行", "USER aigotutor" in df)
        check("有健康检查", "HEALTHCHECK" in df)
        check("容器内关闭 reload", "RELOAD=0" in df)
        check("拷贝前端产物", "--from=web-builder" in df)
        # 运行阶段不应再装 node 依赖
        runtime_part = df.split("AS runtime", 1)[-1]
        check("运行阶段不含 node_modules", "node_modules" not in runtime_part)

    # ---- .dockerignore ----
    di = ROOT / ".dockerignore"
    check(".dockerignore 存在", di.exists())
    if di.exists():
        ignore = di.read_text(encoding="utf-8")
        for pattern in ("data/", "logs/", "web/node_modules/", ".env"):
            check(f".dockerignore 排除 {pattern}", pattern in ignore)

    # ---- .env.example ----
    env = ROOT / ".env.example"
    check(".env.example 存在", env.exists())
    if env.exists():
        content = env.read_text(encoding="utf-8")
        required = [
            "AIGOTUTOR_SECRET",
            "AIGOTUTOR_DB",
            "AIGOTUTOR_PROVIDER",
            "AIGOTUTOR_JUDGE_BACKEND",
            "AIGOTUTOR_RATE_LIMIT",
            "AIGOTUTOR_LOG_JSON",
        ]
        for key in required:
            check(f".env.example 含 {key}", key in content)

    # ---- Caddyfile ----
    caddy = ROOT / "deploy" / "Caddyfile"
    check("Caddyfile 存在", caddy.exists())
    if caddy.exists():
        c = caddy.read_text(encoding="utf-8")
        check("Caddy 关闭 SSE 缓冲", "flush_interval -1" in c)
        check("Caddy 设置安全头", "Content-Security-Policy" in c)
        check("Caddy 传递真实 IP", "X-Forwarded-For" in c)
        check("Caddy 静态资源长缓存", "immutable" in c)

    # ---- CI ----
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    check("CI 配置存在", ci.exists())
    if ci.exists():
        y = ci.read_text(encoding="utf-8")
        for job in ("lint", "test", "migrations", "judge", "frontend", "smoke", "loadtest"):
            check(f"CI 含 {job} job", re.search(rf"^  {job}:", y, re.M) is not None)
        check("CI 有覆盖率阈值", "--cov-fail-under" in y)
        check("CI 用 mock provider", "AIGOTUTOR_PROVIDER: mock" in y)
        # 冒烟套件必须全跑：漏掉任一套件等于某个批次无人守门
        for script in (
            "smoke.py",
            "smoke_batch_a.py",
            "smoke_batch_c.py",
            "smoke_admin.py",
            "smoke_web.py",
        ):
            check(f"CI 运行 {script}", script in y)

    # ---- 依赖清单 ----
    # 这些包缺失时不是"崩"，而是静默降级成错误行为，最难排查，所以显式校验
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for pkg, why in (
        ("alembic", "迁移验收依赖"),
        ("psutil", "判题内存采样与进程树回收依赖"),
        ("tzdata", "Windows 无系统时区库时的兜底"),
        ("pytest-cov", "覆盖率门禁依赖"),
        ("langgraph", "多 Agent 编排依赖（缺失则链路无法启动）"),
        ("redis", "限流/缓存/分布式锁的跨进程实现依赖"),
    ):
        check(f"requirements 含 {pkg}（{why}）", pkg in req)

    # ---- 判题后端不应硬依赖 psutil ----
    sub = (ROOT / "app" / "judge" / "subprocess_backend.py").read_text(encoding="utf-8")
    check("psutil 为可选依赖（缺失时不崩）", "except ImportError" in sub)

    # ---- Redis 必须是可选增强：未配置时全部走进程内实现 ----
    # 这条断言守护项目定位（单机零外部依赖）。若有人把 Redis 改成硬依赖，
    # `python run.py` 在没起 Redis 的机器上会直接崩 —— 必须挡住。
    rc = (ROOT / "app" / "core" / "redis_client.py").read_text(encoding="utf-8")
    check("Redis 未配置时返回 None（优雅降级）", "return None" in rc)
    check("Redis 连接用 RESP2（兼容 Redis 3.x）", "protocol=2" in rc)

    # ---- 生产 compose 应提供 redis 服务与连接串 ----
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    check("生产 compose 含 redis 服务", "redis:" in prod and "image: redis:" in prod)
    check("生产 compose 配置了 REDIS_URL", "AIGOTUTOR_REDIS_URL" in prod)

    # ---- LangGraph 编排存在且被 pipeline 使用 ----
    graph = ROOT / "app" / "core" / "graph.py"
    check("存在 LangGraph 编排模块", graph.exists())
    if graph.exists():
        gs = graph.read_text(encoding="utf-8")
        check("编排使用 StateGraph", "StateGraph" in gs)
        check("编排声明了状态 reducer（防静默覆盖）", "Annotated" in gs and "operator" in gs)
    pipe = (ROOT / "app" / "core" / "pipeline.py").read_text(encoding="utf-8")
    check("pipeline 委托 LangGraph 执行", "run_practice_graph" in pipe)

    # ---- 任务状态必须跨进程可见（多 worker 下的关键契约）----
    ts = (ROOT / "app" / "core" / "task_store.py").read_text(encoding="utf-8")
    check("存在任务跨进程镜像模块", True)
    check("任务状态镜像到 Redis", "setex" in ts and "state" in ts)
    check("支持跨进程取消信号", "request_cancel" in ts and "is_cancel_requested" in ts)
    tasks_py = (ROOT / "app" / "core" / "tasks.py").read_text(encoding="utf-8")
    check("任务状态变化时发布镜像", "publish_state" in tasks_py)
    check("owner 侧轮询跨进程取消信号", "_watch_remote_cancel" in tasks_py)
    # API 层必须用跨进程入口，而不是只查本地字典的 get_task
    agent_api = (ROOT / "app" / "api" / "agent.py").read_text(encoding="utf-8")
    check("任务详情接口走跨进程入口", "task_detail" in agent_api)

    print(f"\n== 结果: {'全部通过' if not failures else '失败 ' + ', '.join(failures)} ==")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
