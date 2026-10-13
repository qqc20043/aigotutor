"""任务状态的跨进程镜像：让任意 worker 都能查询/订阅/取消任务。

问题（README 曾列为遗留）：
    任务表 `_TASKS` 是进程内字典。多 worker 部署下，提交任务的请求可能落在
    worker A，而轮询 `/api/tasks/{id}` 被负载均衡到 worker B —— B 的字典里
    没有这个任务，返回 404；`cancel` 同理打不到任务真正所在的那个进程。

本模块解决"可观测性与控制信号的跨进程共享"，但**不迁移执行**：
    asyncio 任务句柄无法跨进程传递，硬迁移会引入 broker + 序列化 + 重试语义
    （即 Celery 那一套），对"秒级到十秒级"的任务来说是过度设计。
    因此这里的分工是：
      * **执行**留在创建它的进程（owner）；
      * **状态/事件/取消信号**镜像到 Redis，任何 worker 都能读、能取消。

降级原则与 redis_client 一致：未配置 Redis 时全部走进程内，行为与改造前完全一致
（单机开发零外部依赖）。

键设计（统一前缀，TTL 兜底防泄漏）：
    {prefix}:task:{tid}:state    JSON 状态快照
    {prefix}:task:{tid}:events   事件列表（RPUSH 追加，LRANGE 按索引读）
    {prefix}:task:{tid}:cancel   取消信号（owner 轮询它并取消本地句柄）
    {prefix}:task:{tid}:owner    所属 worker 标识（便于诊断）
    {prefix}:tasks:user:{uid}    该用户的任务索引（有序集合，score=创建时间）
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from typing import Any

from .redis_client import get_redis

logger = logging.getLogger("aigotutor.tasks")

# 状态与事件的存活时间：与 tasks.cleanup 的默认 max_age 保持一致（1 小时）
TASK_TTL_SECONDS = 3600
# 单个任务保留的事件数上限。任务本身只跑几秒到几十秒、事件量级是十几条，
# 这个上限是防御性的（万一有异常循环把事件刷爆）。
MAX_EVENTS = 1000

# 本进程标识：hostname:pid。用于诊断"这个任务跑在哪个 worker 上"
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"


def _k(tid: str, suffix: str) -> str:
    from ..config import settings

    return f"{settings.REDIS_PREFIX}:task:{tid}:{suffix}"


def _user_index_key(user_id: str) -> str:
    from ..config import settings

    return f"{settings.REDIS_PREFIX}:tasks:user:{user_id}"


def publish_state(task: Any) -> None:
    """把任务状态快照写入 Redis（幂等，每次状态变化调用）。"""
    client = get_redis()
    if client is None:
        return
    tid = task.id
    state = {
        "task_id": tid,
        "user_id": task.user_id,
        "kind": task.kind,
        "status": task.status,
        "progress": round(task.progress, 4),
        "stage": task.stage,
        "error": task.error,
        "result": task.result if task.status in ("done", "failed") else {},
        "cancel_requested": bool(task.cancel_requested),
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "owner": INSTANCE_ID,
    }
    try:
        pipe = client.pipeline()
        pipe.setex(_k(tid, "state"), TASK_TTL_SECONDS, json.dumps(state, ensure_ascii=False))
        pipe.setex(_k(tid, "owner"), TASK_TTL_SECONDS, INSTANCE_ID)
        if task.user_id:
            # 用户索引：score 用创建时间，便于按时间倒序列出
            pipe.zadd(_user_index_key(task.user_id), {tid: task.created_at})
            pipe.expire(_user_index_key(task.user_id), TASK_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        # 镜像失败不能影响任务执行本身 —— 它只是可观测性增强
        logger.debug("任务状态镜像失败（%s）: %s", tid, type(e).__name__)


def publish_event(task_id: str, event: dict[str, Any]) -> None:
    """追加一条事件到 Redis 列表（与进程内 deque 同源同序）。"""
    client = get_redis()
    if client is None:
        return
    try:
        key = _k(task_id, "events")
        pipe = client.pipeline()
        pipe.rpush(key, json.dumps(event, ensure_ascii=False))
        pipe.ltrim(key, -MAX_EVENTS, -1)
        pipe.expire(key, TASK_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug("任务事件镜像失败（%s）: %s", task_id, type(e).__name__)


def read_state(task_id: str) -> dict[str, Any] | None:
    """从 Redis 读任务状态（本地没有时用）。"""
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_k(task_id, "state"))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def read_events(task_id: str, from_index: int = 0) -> list[dict[str, Any]]:
    """从 Redis 读事件（按索引增量）。返回 [] 表示没有更多。"""
    client = get_redis()
    if client is None:
        return []
    try:
        raw = client.lrange(_k(task_id, "events"), from_index, -1)
        return [json.loads(item) for item in raw]
    except Exception:
        return []


def request_cancel(task_id: str) -> bool:
    """写入取消信号。返回是否真的写入了（Redis 不可用时返回 False）。

    信号带 TTL：任务结束后它自然过期，不需要清理逻辑。
    """
    client = get_redis()
    if client is None:
        return False
    try:
        client.setex(_k(task_id, "cancel"), TASK_TTL_SECONDS, "1")
        return True
    except Exception:
        return False


def is_cancel_requested(task_id: str) -> bool:
    """owner 侧轮询：是否有别的 worker 请求取消这个任务。"""
    client = get_redis()
    if client is None:
        return False
    try:
        return client.exists(_k(task_id, "cancel")) > 0
    except Exception:
        return False


def list_user_task_ids(user_id: str, limit: int = 50) -> list[str]:
    """列出某用户的任务 id（按创建时间倒序）。"""
    client = get_redis()
    if client is None:
        return []
    try:
        # zrevrange 按 score 倒序取最新的 limit 个
        return list(client.zrevrange(_user_index_key(user_id), 0, limit - 1))
    except Exception:
        return []


def cleanup_expired_index(user_id: str) -> int:
    """清理用户索引里已过期（state 键不存在）的 task id。

    索引本身有 TTL 兜底，但活跃用户会不断续期，导致索引里累积已完成的旧任务。
    这里顺手做一次清理，避免 zrevrange 取到的都是无效 id。
    """
    client = get_redis()
    if client is None:
        return 0
    try:
        ids = client.zrange(_user_index_key(user_id), 0, -1)
        if not ids:
            return 0
        pipe = client.pipeline()
        for tid in ids:
            pipe.exists(_k(tid, "state"))
        exists_flags = pipe.execute()
        dead = [tid for tid, ok in zip(ids, exists_flags, strict=False) if not ok]
        if dead:
            client.zrem(_user_index_key(user_id), *dead)
        return len(dead)
    except Exception:
        return 0


def store_info() -> dict[str, Any]:
    """运行态信息，供 /api/stats/global 展示。"""
    client = get_redis()
    return {
        "backend": "redis" if client is not None else "memory",
        "instance_id": INSTANCE_ID,
        "ttl_seconds": TASK_TTL_SECONDS,
        "note": (
            "任务状态/事件/取消信号跨进程共享，任意 worker 都能查询与取消"
            if client is not None
            else "任务状态仅本进程可见；多 worker 部署需配置 Redis（否则轮询可能 404）"
        ),
    }


def now() -> float:
    return time.time()
