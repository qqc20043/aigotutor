"""轻量结果缓存：内存 LRU + TTL，按 prompt 指纹去重。

成本控制的核心一环：5 个 Agent 串一条链路一次刷题 6-10 次调用，
缓存命中直接把重复调用打到 0（演示时反复跑同一题不再烧钱）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any


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


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_cache(key: str) -> Any | None:
    return _cache.get(key)


def set_cache(key: str, value: Any, ttl: int = 3600) -> None:
    _cache.set(key, value, ttl)


def cache_stats() -> dict[str, Any]:
    return _cache.stats()


def cache_clear() -> None:
    _cache.clear()
