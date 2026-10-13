"""可观测性：结构化日志 + trace id 贯穿 + Prometheus 指标（批次 C / F21、F22）。

设计取舍：
  * trace id 用 contextvars 传递 —— 异步环境下 threading.local 不适用，
    contextvars 能随协程自动传递，无需手动透传参数；
  * 日志用 JSON 格式 —— 便于采集（ELK/Loki），字段固定便于查询；
    同时保留人类可读模式供本地开发（AIGOTUTOR_LOG_JSON=0）；
  * 指标自己实现而不用 prometheus_client —— 需要的指标类型很少
    （计数器 + 直方图），自己实现零依赖且输出格式完全可控。
    如后续需要更多指标类型，替换成官方库只需改本文件。

Prometheus 文本格式要点（容易踩坑）：
  * HELP/TYPE 行必须在样本之前，且每个指标只出现一次；
  * label 值必须转义反斜杠、双引号、换行；
  * 直方图的 bucket 必须累加（le 是"小于等于"）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .config import settings

# ------------------------------------------------------------------ trace id
# contextvars 随协程自动传递，无需在每层函数签名里加 trace_id 参数
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="")


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def set_user_id(value: str) -> None:
    _user_id.set(value)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


# -------------------------------------------------------------------- 日志
class JsonFormatter(logging.Formatter):
    """JSON 日志格式化器：固定字段，便于日志系统索引。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        tid = get_trace_id()
        if tid:
            payload["trace_id"] = tid
        uid = _user_id.get()
        if uid:
            payload["user_id"] = uid
        # 业务方通过 extra={"extra_fields": {...}} 传入结构化字段
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """人类可读格式（本地开发默认）：把 trace_id 前置，方便 grep。"""

    def format(self, record: logging.LogRecord) -> str:
        tid = get_trace_id()
        prefix = f"[{tid}] " if tid else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {prefix}{record.name}: {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging() -> None:
    """配置根 logger。重复调用安全（幂等）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.LOG_JSON else HumanFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    # 压掉第三方库的噪音，只保留告警
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """带结构化字段的日志（自动携带 trace_id / user_id）。"""
    logger.log(level, msg, extra={"extra_fields": fields})


# -------------------------------------------------------------------- 指标
def _escape_label(value: str) -> str:
    """Prometheus label 值转义：反斜杠、双引号、换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Metrics:
    """极简指标收集器：计数器 + 直方图。

    刻意不做多进程聚合：单进程 uvicorn 是当前部署形态；
    多 worker 时应换成 prometheus_client 的 multiprocess 模式。
    """

    # HTTP
    http_requests: dict[tuple[str, str, int], int] = field(default_factory=lambda: defaultdict(int))
    http_duration_sum: dict[tuple[str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    http_duration_count: dict[tuple[str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    http_in_flight: int = 0

    # 业务
    agent_calls: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    agent_degraded: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    llm_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    judge_runs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rate_limited: int = 0

    started_at: float = field(default_factory=time.time)

    # ---- 记录方法 ----
    def observe_http(self, method: str, path: str, status: int, seconds: float) -> None:
        # 路径归一：把 id 段替换成占位符，避免基数爆炸（每个 id 一条时序）
        self.http_requests[(method, _normalize_path(path), status)] += 1
        key = (method, _normalize_path(path))
        self.http_duration_sum[key] += seconds
        self.http_duration_count[key] += 1

    def record_agent(self, agent: str, status: str, tokens: int = 0) -> None:
        self.agent_calls[(agent, status)] += 1
        if status == "degraded":
            self.agent_degraded[agent] += 1
        if tokens:
            self.llm_tokens[agent] += tokens

    def record_judge(self, verdict: str) -> None:
        self.judge_runs[verdict] += 1

    def record_rate_limited(self) -> None:
        self.rate_limited += 1

    def reset(self) -> None:
        self.http_requests.clear()
        self.http_duration_sum.clear()
        self.http_duration_count.clear()
        self.agent_calls.clear()
        self.agent_degraded.clear()
        self.llm_tokens.clear()
        self.judge_runs.clear()
        self.rate_limited = 0
        self.http_in_flight = 0

    # ---- 导出 ----
    def render(self) -> str:
        lines: list[str] = []

        def emit(
            name: str,
            mtype: str,
            help_text: str,
            samples: list[tuple[str, str]],
            zero_sample: str | None = None,
        ) -> None:
            """输出一个指标。

            关键：即使还没有任何样本，也必须输出 HELP/TYPE —— 否则
            指标在"首次出现数据之前"对监控系统是不可见的，告警规则会
            报"未知指标"。无样本时用 zero_sample 兜底（值 0），
            这也是 Prometheus 客户端的惯例。
            """
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            if samples:
                lines.extend(samples)
            elif zero_sample is not None:
                lines.append(zero_sample)

        # HTTP 请求总数
        samples = []
        for (method, path, status), count in sorted(self.http_requests.items()):
            labels = (
                f'method="{_escape_label(method)}",path="{_escape_label(path)}",status="{status}"'
            )
            samples.append(f"aigotutor_http_requests_total{{{labels}}} {count}")
        emit(
            "aigotutor_http_requests_total",
            "counter",
            "HTTP 请求总数",
            samples,
            "aigotutor_http_requests_total 0",
        )

        # 请求耗时（用 sum + count 表达，不逐 bucket 输出以保持简洁；
        # 需要分位时由 Prometheus 端用 histogram_quantile 计算）
        samples = []
        for (method, path), total in sorted(self.http_duration_sum.items()):
            count = self.http_duration_count.get((method, path), 0)
            if not count:
                continue
            labels = f'method="{_escape_label(method)}",path="{_escape_label(path)}"'
            samples.append(f"aigotutor_http_duration_seconds_sum{{{labels}}} {total:.6f}")
            samples.append(f"aigotutor_http_duration_seconds_count{{{labels}}} {count}")
        emit(
            "aigotutor_http_duration_seconds",
            "summary",
            "HTTP 请求耗时",
            samples,
            "aigotutor_http_duration_seconds_sum 0\naigotutor_http_duration_seconds_count 0",
        )

        # 在途请求
        lines.append("# HELP aigotutor_http_in_flight 当前处理中的请求数")
        lines.append("# TYPE aigotutor_http_in_flight gauge")
        lines.append(f"aigotutor_http_in_flight {self.http_in_flight}")

        # Agent 调用
        samples = []
        for (agent, status), count in sorted(self.agent_calls.items()):
            labels = f'agent="{_escape_label(agent)}",status="{_escape_label(status)}"'
            samples.append(f"aigotutor_agent_calls_total{{{labels}}} {count}")
        emit(
            "aigotutor_agent_calls_total",
            "counter",
            "Agent 调用次数（按状态）",
            samples,
            "aigotutor_agent_calls_total 0",
        )

        # 降级次数（核心健康指标）
        samples = [
            f'aigotutor_agent_degraded_total{{agent="{_escape_label(a)}"}} {c}'
            for a, c in sorted(self.agent_degraded.items())
        ]
        emit(
            "aigotutor_agent_degraded_total",
            "counter",
            "Agent 降级到规则引擎的次数",
            samples,
            "aigotutor_agent_degraded_total 0",
        )

        # token 消耗（成本指标）
        samples = [
            f'aigotutor_llm_tokens_total{{agent="{_escape_label(a)}"}} {c}'
            for a, c in sorted(self.llm_tokens.items())
        ]
        emit(
            "aigotutor_llm_tokens_total",
            "counter",
            "LLM token 累计消耗",
            samples,
            "aigotutor_llm_tokens_total 0",
        )

        # 判题结果分布
        samples = [
            f'aigotutor_judge_total{{verdict="{_escape_label(v)}"}} {c}'
            for v, c in sorted(self.judge_runs.items())
        ]
        emit(
            "aigotutor_judge_total",
            "counter",
            "判题结果分布",
            samples,
            "aigotutor_judge_total 0",
        )

        # 限流拒绝
        lines.append("# HELP aigotutor_rate_limited_total 被限流拒绝的请求数")
        lines.append("# TYPE aigotutor_rate_limited_total counter")
        lines.append(f"aigotutor_rate_limited_total {self.rate_limited}")

        # 进程运行时长
        lines.append("# HELP aigotutor_uptime_seconds 进程运行秒数")
        lines.append("# TYPE aigotutor_uptime_seconds gauge")
        lines.append(f"aigotutor_uptime_seconds {time.time() - self.started_at:.3f}")

        return "\n".join(lines) + "\n"


# 路径归一：把资源 id 段替换成 {id}，避免每个 id 生成一条时序（基数爆炸）。
#
# 为什么不用"长度启发式"：试过用长度 >= N 判断 id，结果
# /api/submissions/12345 里的 submissions（11 字符）被误判成 id，
# 变成 /api/{id}/{id}，不同接口混成一条时序 —— 比不归一还糟。
#
# 采用白名单策略：明确列出所有"固定路径段"，不在白名单里的才视为 id。
# 这样行为完全可预测，新增接口时补一行白名单即可。
_KNOWN_SEGMENTS = {
    # 顶层与认证
    "api",
    "auth",
    "users",
    "health",
    "metrics",
    "docs",
    "redoc",
    "openapi.json",
    "stats",
    "global",
    "data",
    # 资源集合
    "problems",
    "submissions",
    "recommend",
    "review",
    "mastery",
    "wrong-book",
    "agent",
    "tasks",
    "judge",
    "admin",
    # 固定动作 / 子资源
    "register",
    "login",
    "refresh",
    "logout",
    "change-password",
    "me",
    "model-config",
    "random",
    "search",
    "tags",
    "route",
    "testcases",
    "next",
    "explain",
    "roadmap",
    "queue",
    "due",
    "mark",
    "overview",
    "profile",
    "timeline",
    "recalc",
    "run",
    "stream",
    "pipeline",
    "cache",
    "clear",
    "cancel",
    "wait",
    "cluster",
    "mastered",
    "import",
    "export",
    "leaderboard",
    "checkin",
    "report",
    "notifications",
    "read",
}

# 明显是 id 的形态（本项目 id 为 uuid4().hex，32 位十六进制）
_HEX_CHARS = set("0123456789abcdefABCDEF")


def _looks_like_id(seg: str) -> bool:
    """判断某段是否为资源 id。

    规则（按优先级）：
      1. 白名单里的固定段 -> 不是 id；
      2. 纯数字 -> id（如 /submissions/12345）；
      3. 长十六进制串（>= 16）-> id（本项目 uuid hex）；
      4. 其余 -> 不是 id（宁可漏归一，也不要把固定段误伤成 id）。
    """
    if not seg or seg in _KNOWN_SEGMENTS:
        return False
    if seg.isdigit():
        return True
    # 长十六进制串（>= 16）视为 id：本项目 id 是 uuid4().hex
    return len(seg) >= 16 and all(c in _HEX_CHARS for c in seg)


def _normalize_path(path: str) -> str:
    parts = path.split("/")
    out: list[str] = []
    for seg in parts:
        out.append("{id}" if _looks_like_id(seg) else seg)
    return "/".join(out)


_metrics = Metrics()


def get_metrics() -> Metrics:
    return _metrics


# ------------------------------------------------------------------ 中间件
class ObservabilityMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 trace id、记录耗时与状态码、写入访问日志。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 复用上游传入的 trace id（网关/前端注入），否则新建
        incoming = request.headers.get("x-trace-id") or request.headers.get("x-request-id")
        tid = incoming or new_trace_id()
        set_trace_id(tid)

        _metrics.http_in_flight += 1
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-Id"] = tid
            return response
        finally:
            elapsed = time.perf_counter() - started
            _metrics.http_in_flight = max(0, _metrics.http_in_flight - 1)
            path = request.url.path
            _metrics.observe_http(request.method, path, status_code, elapsed)
            if status_code == 429:
                _metrics.record_rate_limited()

            # 访问日志：健康检查/指标抓取太吵，降为 debug
            quiet = path in ("/health", "/metrics")
            logging.getLogger("aigotutor.access").log(
                logging.DEBUG if quiet else logging.INFO,
                "%s %s -> %s",
                request.method,
                path,
                status_code,
                extra={
                    "extra_fields": {
                        "method": request.method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                },
            )


async def metrics_endpoint(_request: Request) -> Response:
    """Prometheus 抓取端点。"""
    return PlainTextResponse(
        _metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def record_agent_metric(agent: str, status: str, tokens: int = 0) -> None:
    """供 Agent 执行处调用（避免各调用点直接依赖 Metrics 内部结构）。"""
    _metrics.record_agent(agent, status, tokens)


def record_judge_metric(verdict: str) -> None:
    _metrics.record_judge(verdict)


def metrics_snapshot() -> dict[str, Any]:
    """JSON 快照，供 /api/stats/global 使用（人读比 Prometheus 格式友好）。"""
    total_http = sum(_metrics.http_requests.values())
    total_dur = sum(_metrics.http_duration_sum.values())
    total_cnt = sum(_metrics.http_duration_count.values())
    return {
        "http_requests": total_http,
        "http_avg_ms": round(total_dur / total_cnt * 1000, 2) if total_cnt else 0.0,
        "http_in_flight": _metrics.http_in_flight,
        "agent_calls": {f"{a}:{s}": c for (a, s), c in sorted(_metrics.agent_calls.items())},
        "agent_degraded": dict(sorted(_metrics.agent_degraded.items())),
        "llm_tokens": dict(sorted(_metrics.llm_tokens.items())),
        "judge": dict(sorted(_metrics.judge_runs.items())),
        "rate_limited": _metrics.rate_limited,
        "uptime_seconds": round(time.time() - _metrics.started_at, 1),
    }
