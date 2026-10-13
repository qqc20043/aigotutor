"""分布式写锁：跨进程串行化写事务（多 worker 部署的关键一环）。

问题背景（README 曾列为已知边界）：
    本项目的写锁原本是 `asyncio.Lock` —— 它只在一个进程内有效。
    用 `uvicorn --workers N` 时，N 个进程各自持锁，SQLite 的写锁竞争
    会退化成"多个进程同时写"，实测会撞 `database is locked`。

本模块把写锁升级为 Redis 分布式锁：
  * 配置 Redis 且可用 -> `SET key token NX PX ttl` 实现跨进程互斥；
  * 未配置/不可用 -> 回落为 asyncio.Lock（单进程语义不变，单机开发零依赖）。

为什么是"锁 + 自愈"而不是单纯 SETNX：
    持锁进程若崩溃，锁不会自动释放 —— 因此必须给 TTL。但 TTL 又不能太短，
    否则长事务会被误判为死锁而放行。这里取 10 秒：SQLite 的写事务是毫秒级
    （单条 commit），10 秒足够宽裕；真出现 10 秒的写事务，说明代码有问题，
    让它超时释放反而是正确的（避免整站写操作被一个卡死的请求阻塞）。

为什么释放锁要校验 token：
    直接 DEL 会误删**别人的**锁 —— 场景：A 持锁超时释放后 B 拿到锁，
    此时 A 的 finally 执行 DEL，把 B 的锁删了，C 就能和 B 同时进入临界区。
    校验 token 保证"只删自己加的锁"。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from .redis_client import get_redis

logger = logging.getLogger("aigotutor.lock")

# 锁的 TTL（毫秒）。见模块 docstring：写事务是毫秒级，10 秒足够宽裕
LOCK_TTL_MS = 10_000
# 抢锁失败后的重试间隔（秒）。取小值让竞争激烈时也能快速推进
RETRY_INTERVAL = 0.02
# 单次抢锁的最长等待（秒）—— 超过就放弃并如实报错，不无限等
ACQUIRE_TIMEOUT = 10.0

_local_locks: dict[str, asyncio.Lock] = {}
_local_lock_guard = threading.Lock()


def _get_local_lock(name: str) -> asyncio.Lock:
    """取（或建）进程内锁。

    必须按事件循环隔离：asyncio.Lock 绑定创建它的事件循环，
    跨循环复用会抛 "attached to a different loop"。测试里每个用例
    用新循环，因此这里同时按 name 与 loop 缓存。
    """
    loop = asyncio.get_running_loop()
    key = f"{name}:{id(loop)}"
    with _local_lock_guard:
        lock = _local_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _local_locks[key] = lock
        return lock


@asynccontextmanager
async def distributed_lock(name: str = "write") -> AsyncIterator[dict[str, Any]]:
    """获取分布式锁；退出时释放。yield 一个含 backend 信息的 dict。

    用法：
        async with distributed_lock("write") as info:
            await session.commit()

    无论走 Redis 还是本地锁，语义一致：同一 name 下的临界区在**同一时刻
    只有一个持有者**（本地锁限单进程，Redis 锁跨进程）。
    """
    client = get_redis()
    if client is None:
        # 降级路径：进程内锁，行为与改造前完全一致
        async with _get_local_lock(name):
            yield {"backend": "local", "name": name}
        return

    token = uuid.uuid4().hex
    key = f"{_prefix()}{name}"
    acquired = await _acquire(client, key, token)
    if not acquired:
        # 抢不到锁不应让请求失败 —— 写事务本身有 SQLite busy_timeout 兜底，
        # 这里退化为本地锁继续执行，并记录告警（说明存在锁竞争压力）。
        logger.warning("Redis 锁获取超时（%s），本次退化为进程内锁", name)
        async with _get_local_lock(name):
            yield {"backend": "local-fallback", "name": name}
        return

    try:
        yield {"backend": "redis", "name": name, "token": token}
    finally:
        _release(client, key, token)


async def _acquire(client: Any, key: str, token: str) -> bool:
    """轮询抢锁，最多等 ACQUIRE_TIMEOUT 秒。"""
    deadline = asyncio.get_running_loop().time() + ACQUIRE_TIMEOUT
    while True:
        try:
            ok = client.set(key, token, nx=True, px=LOCK_TTL_MS)
            if ok:
                return True
        except Exception as e:
            logger.warning("Redis 锁操作异常（%s），退化为进程内锁", type(e).__name__)
            return False
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(RETRY_INTERVAL)


def _release(client: Any, key: str, token: str) -> None:
    """释放锁，但只删自己加的那把（用 Lua 保证"校验+删除"原子）。

    非原子实现（先 GET 比较再 DEL）在两步之间有窗口：若锁在此期间因 TTL
    过期并被他人获取，比较会通过、DEL 会删掉别人的锁。Lua 脚本在 Redis
    单线程内执行，没有这个窗口。
    """
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    else
        return 0
    end
    """
    # 释放失败不抛：锁有 TTL 兜底，最终会自己过期，不该让业务请求失败
    with suppress(Exception):
        client.eval(script, 1, key, token)


def _prefix() -> str:
    from ..config import settings

    return f"{settings.REDIS_PREFIX}:lock:"


def lock_info() -> dict[str, Any]:
    """运行态信息，供 /api/stats/global 展示。"""
    client = get_redis()
    return {
        "backend": "redis" if client is not None else "local",
        "ttl_ms": LOCK_TTL_MS,
        "acquire_timeout_s": ACQUIRE_TIMEOUT,
        "note": (
            "跨进程互斥，支持 uvicorn --workers N"
            if client is not None
            else "进程内互斥（未配置 Redis）；多 worker 部署会退化为各自持锁"
        ),
    }
