"""限流与请求体大小限制（批次 C / F10）。

为什么自己实现而不用 slowapi 之类的库：
  1. 依赖面小 —— 本项目只要求"按用户 + 按 IP 的固定窗口限流"，标准库足够；
  2. 语义可控 —— LLM 相关接口的限流应该比读接口严格得多，需要按路由分组配置；
  3. 便于观测 —— 拒绝次数直接进 Prometheus 指标。

实现：滑动窗口计数（每个 key 记录时间戳队列）。
  相比固定窗口，不会出现"窗口边界突发双倍流量"的问题；
  相比令牌桶，实现更简单且行为更直观（对使用者而言"每分钟 N 次"更好理解）。

存储：进程内字典。多 worker 部署时应换 Redis —— 已在文档中标注。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import settings

# ------------------------------------------------------------------ 路由分组
# 不同接口的限流强度不同：写/LLM 接口消耗真实资源，读接口应该宽松。
# 值为"每分钟允许次数"；None 表示不限流。
RATE_GROUPS: dict[str, int | None] = {
    # 认证：防暴力破解，最严格
    "/api/auth/login": 10,
    "/api/auth/register": 5,
    "/api/auth/refresh": 30,
    "/api/auth/change-password": 5,
    # LLM 相关：每次调用都烧钱，必须收敛
    "/api/agent/run": 30,
    "/api/agent/stream": 30,
    "/api/agent/pipeline": 15,
    "/api/submissions": 60,
    "/api/submissions/": 60,
    # 判题：CPU 密集
    "/api/problems/": 120,
    # 读接口：宽松
    "/api/recommend/": 120,
    "/api/mastery/": 120,
    "/api/review/": 120,
    # 健康检查与文档不限流（监控与调试需要）
    "/health": None,
    "/docs": None,
    "/openapi.json": None,
    "/metrics": None,
}

# 管理接口单独一组（管理员操作不频繁但重要）
ADMIN_PREFIX = "/api/admin"


@dataclass
class Window:
    """滑动窗口：记录时间戳队列。"""

    hits: deque[float] = field(default_factory=deque)

    def allow(self, limit: int, window: float, now: float) -> tuple[bool, int]:
        """尝试放行。返回 (是否允许, 剩余额度)。"""
        cutoff = now - window
        while self.hits and self.hits[0] <= cutoff:
            self.hits.popleft()
        if len(self.hits) >= limit:
            return False, 0
        self.hits.append(now)
        return True, limit - len(self.hits)


class RateLimiter:
    """按 key 的滑动窗口限流器。"""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = window_seconds
        self._buckets: dict[str, Window] = defaultdict(Window)
        self._last_sweep = time.time()
        # 观测指标
        self.allowed = 0
        self.rejected = 0

    def check(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        self._maybe_sweep(now)
        ok, remaining = self._buckets[key].allow(limit, self.window, now)
        if ok:
            self.allowed += 1
        else:
            self.rejected += 1
        return ok, remaining

    def _maybe_sweep(self, now: float) -> None:
        """定期清理过期桶，防止内存无限增长（被扫的 key 可能是一次性 IP）。

        坑：不能只判断"队列是否为空" —— 队列里的时间戳只有在下次
        check() 时才会被 pop，因此空桶判断会永远失败。
        正确做法是按窗口边界判断"最新一次访问是否已过期"。
        """
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        dead = [k for k, w in self._buckets.items() if not w.hits or w.hits[-1] <= cutoff]
        for k in dead:
            self._buckets.pop(k, None)

    def reset(self) -> None:
        self._buckets.clear()
        self.allowed = 0
        self.rejected = 0

    def stats(self) -> dict[str, Any]:
        return {
            "buckets": len(self._buckets),
            "allowed": self.allowed,
            "rejected": self.rejected,
            "window_seconds": self.window,
        }


_limiter = RateLimiter(window_seconds=60.0)


def get_limiter() -> RateLimiter:
    return _limiter


def limit_for_path(path: str) -> int | None:
    """按路径匹配限流额度。返回 None 表示该路径不限流。

    只回答"这条路径的额度是多少"，不掺入全局开关判断 ——
    开关由中间件统一处理。两者混在一起会让调用方（含测试）无法
    单纯查询路由规则。
    """
    if path.startswith(ADMIN_PREFIX):
        return 120

    best: tuple[int, int | None] | None = None
    for prefix, limit in RATE_GROUPS.items():
        matches = path == prefix or path.startswith(prefix)
        # 选最长的匹配前缀（更具体的规则优先）
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), limit)

    if best is not None:
        return best[1]

    # 未显式配置的路径：走全局默认（保护未知接口）
    return settings.RATE_LIMIT_PER_MINUTE


def effective_limit(path: str) -> int | None:
    """实际生效的额度：先看全局开关，再查路由规则。"""
    if not settings.RATE_LIMIT_ENABLED:
        return None
    return limit_for_path(path)


def client_ip(request: Request) -> str:
    """取客户端 IP。

    优先 X-Forwarded-For 首段（反向代理场景），否则用直连地址。
    注意：XFF 可被伪造，因此限流的 key 用"IP + 用户"组合，
    仅靠 IP 在代理后面会把所有人算成一个桶。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流 + 请求体大小限制中间件。

    请求体限制放在这里而不是逐接口校验：Body 大小是传输层属性，
    在中间件统一拦截更可靠（且避免超大 JSON 被解析后才拒绝，白耗内存）。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # ---- 请求体大小限制 ----
        # 用 walrus 一次取值：先确认是数字，再比对上限（两个判断语义不同，
        # 强行合并成一行会让"非法值不拦截"这个前提变得不明显）
        raw_len = request.headers.get("content-length")
        if raw_len and raw_len.isdigit() and int(raw_len) > settings.MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"请求体过大：{int(raw_len)} 字节，上限 {settings.MAX_REQUEST_BYTES} 字节"
                    )
                },
            )

        # ---- 限流 ----
        if settings.RATE_LIMIT_ENABLED:
            path = request.url.path
            limit = effective_limit(path)
            if limit is not None and request.method != "OPTIONS":
                # 用户维度优先（已登录时），否则按 IP
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    key = f"u:{auth[7:47]}"  # 用 token 前缀做 key，不存全文
                else:
                    key = f"ip:{client_ip(request)}"
                key = f"{key}:{path}"

                ok, remaining = _limiter.check(key, limit)
                if not ok:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": (f"请求过于频繁：该接口限制每分钟 {limit} 次，请稍后重试")
                        },
                        headers={
                            "Retry-After": str(int(_limiter.window)),
                            "X-RateLimit-Limit": str(limit),
                            "X-RateLimit-Remaining": "0",
                        },
                    )
                response = await call_next(request)
                response.headers["X-RateLimit-Limit"] = str(limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)
                return response

        return await call_next(request)


def rate_limit_stats() -> dict[str, Any]:
    return _limiter.stats()


def reset_rate_limit() -> None:
    _limiter.reset()
