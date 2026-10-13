"""结果缓存：内存 LRU + TTL（默认），配置 Redis 后升级为跨进程共享。

成本控制的核心一环：一条刷题链路会打 6-10 次 LLM 调用，
缓存命中直接把重复调用打到 0（演示时反复跑同一题不再烧钱）。

两级存储策略（自动选择）：
  * 配置了 AIGOTUTOR_REDIS_URL 且可用 -> Redis（跨进程/跨 worker 共享，
    多实例部署时命中率显著更高，且重启不丢）；
  * 否则 -> 进程内 LRU（单机开发零依赖）。

无论走哪一级，对外的 get/set/stats/clear 接口完全一致 —— 调用方
（BaseAgent）不需要知道自己用的是哪种存储。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from .redis_client import get_redis


class TTLCache:
    def __init__(self, maxsize: int = 1024) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            expire_at, value = item
            if expire_at < time.time():
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


_cache = TTLCache(maxsize=2048)
# Redis 路径的命中统计（进程内计数，仅用于观测；跨进程总量由 Redis 侧 DBSIZE 反映）
_redis_hits = 0
_redis_misses = 0


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_cache(key: str) -> Any | None:
    """读缓存：优先 Redis，不可用时走进程内。

    Redis 里的值以 JSON 字符串存放（不存 pickle）—— 跨进程共享的数据用
    显式序列化格式，避免不同 Python 版本间的 pickle 兼容问题。
    """
    global _redis_hits, _redis_misses
    client = get_redis()
    if client is not None:
        try:
            raw = client.get(f"{_prefix()}{key}")
            if raw is None:
                _redis_misses += 1
            else:
                _redis_hits += 1
                return json.loads(raw)
            # Redis 未命中时**不再查进程内**：两级同源，Redis 可用时它就是
            # 唯一真相；混查会让"刚被 clear 掉的键"从本地复活。
            return None
        except Exception:
            pass  # Redis 抖动 -> 回落进程内
    return _cache.get(key)


def set_cache(key: str, value: Any, ttl: int = 3600) -> None:
    client = get_redis()
    if client is not None:
        try:
            client.setex(
                f"{_prefix()}{key}", max(1, int(ttl)), json.dumps(value, ensure_ascii=False)
            )
            return
        except Exception:
            pass
    _cache.set(key, value, ttl)


def _prefix() -> str:
    from ..config import settings

    return f"{settings.REDIS_PREFIX}:cache:"


def cache_stats() -> dict[str, Any]:
    """缓存统计。Redis 可用时以 Redis 为准（size 用 DBSIZE 估算）。"""
    client = get_redis()
    if client is not None:
        try:
            total = _redis_hits + _redis_misses
            return {
                "backend": "redis",
                "size": int(client.dbsize()),
                "hits": _redis_hits,
                "misses": _redis_misses,
                "hit_rate": round(_redis_hits / total, 4) if total else 0.0,
            }
        except Exception:
            pass
    stats = _cache.stats()
    stats["backend"] = "memory"
    return stats


def cache_clear() -> None:
    """清空缓存。Redis 可用时只清本应用前缀下的键，不动同实例的其它数据。"""
    global _redis_hits, _redis_misses
    client = get_redis()
    if client is not None:
        try:
            # SCAN 而不是 KEYS：KEYS 会阻塞整个 Redis 实例（生产禁用）
            pattern = f"{_prefix()}*"
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    client.delete(*keys)
                if cursor == 0:
                    break
            _redis_hits = _redis_misses = 0
        except Exception:
            pass
    _cache.clear()
