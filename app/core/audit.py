"""管理操作审计：统一写入入口 + 查询。

设计要点：
  * **写入不能拖垮业务**：审计失败只记 warning、绝不让管理动作失败
    （用独立 session，不参与调用方事务）；
  * **actor_name 冗余存**：用户改名/删除后日志仍可读；
  * detail 存 JSON（参数 + 结果摘要），列表页只展示摘要、详情可展开。

动作命名（action）固定为动词_对象，便于筛选与统计：
    problem.create / problem.update / problem.delete / problem.testcases
    user.patch / user.reset_password / submission.rejudge / announce.send
    data.cleanup
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select

from ..models import AdminAuditLog

ACTION_LABELS: dict[str, str] = {
    "problem.create": "新建题目",
    "problem.update": "修改题目",
    "problem.delete": "删除题目",
    "problem.testcases": "维护测试用例",
    "user.patch": "修改用户",
    "user.reset_password": "重置密码",
    "submission.rejudge": "重判提交",
    "announce.send": "发送公告",
    "data.cleanup": "数据清理",
    "users.batch": "批量操作用户",
}


async def log_admin_action(
    db,
    *,
    actor_id: str,
    actor_name: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """写一条审计日志——**复用调用方的 db session，与业务同事务**。

    为什么不能用独立 session（实测踩坑）：管理动作在调用方事务里已持有
    SQLite 写锁，独立 session 再写会 database is locked；而审计又被设计成
    "失败不影响业务"（吞异常）→ 日志静默全丢（测试抓到：库里一行没有）。
    正确语义：审计与业务同生共死——日志写不进去，高危动作就不该生效。
    """
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            actor_name=actor_name[:64],
            action=action[:48],
            target_type=target_type[:32],
            target_id=str(target_id)[:64],
            detail=json.dumps(detail or {}, ensure_ascii=False)[:4000],
        )
    )
    await db.flush()


async def list_audit_logs(
    db,
    *,
    action: str = "",
    actor: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """审计日志查询（按动作/操作人筛选，时间倒序）。"""
    stmt = select(AdminAuditLog)
    count_stmt = select(func.count()).select_from(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
        count_stmt = count_stmt.where(AdminAuditLog.action == action)
    if actor:
        stmt = stmt.where(AdminAuditLog.actor_name.contains(actor))
        count_stmt = count_stmt.where(AdminAuditLog.actor_name.contains(actor))
    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await db.execute(
                stmt.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "actor_name": r.actor_name,
                "action": r.action,
                "action_label": ACTION_LABELS.get(r.action, r.action),
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": _safe_json(r.detail),
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}
