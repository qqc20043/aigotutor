"""Redis 连接层：可选依赖 + 优雅降级 + 老版本兼容。

设计原则（这是本模块存在的核心）：
  **Redis 是可选增强，不是必需依赖。**

  * 未配置 `AIGOTUTOR_REDIS_URL` → 返回 None，各调用方走进程内实现；
  * 配置了但连不上 → 记录一次警告，之后静默降级（不每次重试刷日志）；
  * 连接可用 → 提供跨进程共享的限流计数、结果缓存与会话检查点。

这样单机开发（`python run.py`，零外部服务）与多 worker 生产部署可以跑**同一份代码**，
不需要为环境差异维护两套实现。

Redis 3.0 兼容（实测踩到）：
    微软移植版 Redis 3.0.504 不支持 RESP3 的 HELLO 握手，redis-py 5+ 默认会发 HELLO
    并直接抛 `unknown command 'HELLO'`。因此连接时显式指定 protocol=2（RESP2），
    这是 3.0 就能理解的协议。生产用的 Redis 6+ 也完全兼容 RESP2，故统一用 2。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import settings

logger = logging.getLogger("aigotutor.redis")

_client: Any = None
_init_lock = threading.Lock()
_unavailable_logged = False


def get_redis() -> Any | None:
    """返回 Redis 客户端；不可用时返回 None（调用方须有降级路径）。

    首次调用做一次真实 PING 探测：只建连接对象不算可用 —— 服务器没起来时
    连接对象照样能构造，直到第一次命令才暴露。这里提前暴露，避免"以为有
    Redis，实际每次操作都超时"。
    """
    global _client, _unavailable_logged

    if not settings.REDIS_URL:
        return None
    if _client is not None:
        return _client

    with _init_lock:
        if _client is not None:  # 双检：并发首次调用只初始化一次
            return _client
        try:
            import redis  # 延迟导入：未装 redis-py 时不影响其它功能

            client = redis.Redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=settings.REDIS_TIMEOUT,
                socket_timeout=settings.REDIS_TIMEOUT,
                decode_responses=True,
                protocol=2,  # 见模块 docstring：兼容 Redis 3.0
            )
            client.ping()
            _client = client
            logger.info("Redis 已连接：%s", _redact(settings.REDIS_URL))
            return _client
        except Exception as e:
            if not _unavailable_logged:
                logger.warning(
                    "Redis 不可用（%s: %s），已降级为进程内实现；"
                    "多 worker 部署下限流与缓存不跨进程共享",
                    type(e).__name__,
                    str(e)[:120],
                )
                _unavailable_logged = True
            return None


def redis_available() -> bool:
    return get_redis() is not None


def reset_redis_cache() -> None:
    """测试用：清掉缓存的客户端，让下次调用重新探测。"""
    global _client, _unavailable_logged
    with _init_lock:
        _client = None
        _unavailable_logged = False


def _redact(url: str) -> str:
    """日志里隐去密码（形如 redis://:pass@host:port/0）。"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def redis_info() -> dict[str, Any]:
    """运行态信息，供 /api/stats/global 与管理后台展示。"""
    client = get_redis()
    if client is None:
        return {
            "configured": bool(settings.REDIS_URL),
            "available": False,
            "url": _redact(settings.REDIS_URL) if settings.REDIS_URL else "",
            "note": "未配置或不可用，限流与缓存使用进程内实现（多 worker 不共享）",
        }
    try:
        info = client.info("server")
        return {
            "configured": True,
            "available": True,
            "url": _redact(settings.REDIS_URL),
            "version": str(info.get("redis_version", "")),
            "note": "限流与缓存跨进程共享，支持多 worker 部署",
        }
    except Exception as e:
        return {
            "configured": True,
            "available": False,
            "url": _redact(settings.REDIS_URL),
            "note": f"连接异常: {type(e).__name__}",
        }
