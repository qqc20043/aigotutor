"""⑧ 数据导入导出与健康检查。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..core.pipeline import annotate_problem
from ..db import healthcheck
from ..deps import DbSession, ModelConfigDep, UserDep
from ..services import create_problem, export_user_data

router = APIRouter(prefix="/api/data", tags=["⑧ 数据"])


class ImportIn(BaseModel):
    problems: list[dict[str, Any]] = []
    auto_annotate: bool = True


class ProblemIn(BaseModel):
    title: str
    statement: str = ""
    input_desc: str = ""
    output_desc: str = ""
    samples: list[Any] = []
    constraints: str = ""
    tags: list[str] = []
    source: str = "import"
    difficulty: str = "入门"
    difficulty_score: float = 0.0


@router.post("/import", response_model=dict)
async def import_data(body: ImportIn, db: DbSession, user: UserDep, cfg: ModelConfigDep):
    """批量导入题目（JSON），可选自动难度标注。"""
    created: list[dict[str, Any]] = []
    for raw in body.problems[:500]:
        item = ProblemIn(**raw)
        data = item.model_dump()
        tag_names = item.tags
        if body.auto_annotate:
            annotation = await annotate_problem(
                {
                    "title": item.title,
                    "statement": item.statement,
                    "constraints": item.constraints,
                    "samples": item.samples,
                    "tags": item.tags,
                    "statement_len": len(item.statement),
                    "tag_count": max(1, len(item.tags)),
                    "algo_depth": 2,
                    "constraint_tight": any(
                        x in item.constraints for x in ("10^5", "1e5", "10^9", "1e9")
                    ),
                    "is_constructive": "构造" in item.statement,
                },
                cfg,
                user_id=user.id,
            )
            data["difficulty"] = annotation["difficulty"]
            data["difficulty_score"] = annotation["difficulty_score"]
            data["difficulty_source"] = annotation["difficulty_source"]
            tag_names = annotation["tags"]
        problem = await create_problem(db, data, tag_names=tag_names)
        created.append({"id": problem.id, "title": problem.title, "difficulty": problem.difficulty})
    return {"imported": len(created), "items": created}


@router.get("/export", response_model=dict)
async def export_data(db: DbSession, user: UserDep):
    """导出当前用户的全部学习数据（JSON，可用于迁移与离线分析）。"""
    return await export_user_data(db, user.id)


@router.get("/export/problems", response_model=dict)
async def export_problems(db: DbSession, user: UserDep, limit: int = 500):
    """导出题库。"""
    from ..services import get_problem_tags, list_problems

    rows, total = await list_problems(db, offset=0, limit=limit)
    tag_map = await get_problem_tags(db, [p.id for p in rows])
    return {
        "total": total,
        "items": [
            {
                "title": p.title,
                "statement": p.statement,
                "input_desc": p.input_desc,
                "output_desc": p.output_desc,
                "samples": json.loads(p.samples or "[]"),
                "constraints": p.constraints,
                "tags": [t["name"] for t in tag_map.get(p.id, [])],
                "difficulty": p.difficulty,
                "difficulty_score": p.difficulty_score,
                "source": p.source,
            }
            for p in rows
        ],
    }


health_router = APIRouter(tags=["⑧ 数据"])


async def _health_payload() -> dict:
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "db": await healthcheck(),
        "default_provider": settings.DEFAULT_PROVIDER,
    }


# 探针路径保持 /health 不变：Dockerfile、两个 compose 与 CI 的 HEALTHCHECK
# 都依赖它，改动会静默打断容器健康检查。
@health_router.get("/health", response_model=dict)
async def health():
    return await _health_payload()


# 前端 API 层统一走 /api 前缀（http.get('/health') → /api/health）。
# 少了这个别名，设置页的"服务健康"永远显示异常并弹出 Not Found —— 实际
# 服务是好的，纯属路径不一致（实测踩到）。同一实现注册两次，杜绝再次漂移。
@health_router.get("/api/health", response_model=dict, include_in_schema=False)
async def health_api_alias():
    return await _health_payload()


# 注意：这里刻意不再注册 "/" 路由。
# 前端构建产物由 main.py 以 StaticFiles 挂载在根路径；若此处也定义 "/"，
# 它会因为注册顺序更早而抢占根路径，导致前端页面永远打不开（实测踩到过）。
# 未构建前端时，main.py 会提供一个带指引的 "/" 兜底响应。
