"""FastAPI 依赖：当前用户、模型配置、数据库会话、分页。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import User, UserModelConfig
from .security import decode_token, decrypt_secret

DbSession = Annotated[AsyncSession, Depends(get_db)]


@dataclass
class CurrentUser:
    id: str
    username: str
    nickname: str = ""
    level: int = 3
    role: str = "user"
    timezone: str = "Asia/Shanghai"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """解析 Bearer Token 并校验其仍然有效。

    三重校验：
      1. 签名与有效期（decode_token）；
      2. token 类型必须是 access —— 否则长命的 refresh token 能当通行证用，
         短命 access 的设计就白做了；
      3. token 里的 ver 必须等于用户当前 token_version —— 改密或登出后
         旧 token 立即失效，无需黑名单。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer Token")

    payload = decode_token(authorization.split(" ", 1)[1].strip(), expect="access")
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 无效、已过期或类型不正确")

    user = await db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")

    if int(payload.get("ver", 0)) != int(user.token_version):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效（密码已修改或已登出）")

    return CurrentUser(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        level=user.level,
        role=user.role,
        timezone=user.timezone,
    )


UserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def require_admin(user: UserDep) -> CurrentUser:
    """管理接口专用依赖：非管理员一律 403。"""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


async def get_model_config(
    db: DbSession,
    user: UserDep,
    purpose: Annotated[str, Query()] = "default",
) -> dict:
    """取当前用户的模型配置；缺省回落到全局默认（mock）。"""
    stmt = select(UserModelConfig).where(
        UserModelConfig.user_id == user.id,
        UserModelConfig.purpose == purpose,
        UserModelConfig.enabled == True,  # noqa: E712
    )
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if cfg is None:
        return {
            "provider": settings.DEFAULT_PROVIDER,
            "model": settings.DEFAULT_MODEL,
            "base_url": settings.DEFAULT_BASE_URL,
            "api_key": settings.DEFAULT_API_KEY,
            "temperature": 0.2,
            "max_tokens": 2048,
            "purpose": purpose,
        }
    return {
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.base_url,
        # 落库为密文（services.upsert_model_config 已加密），使用时解密；
        # 存量明文行 decrypt_secret 原样返回，兼容旧数据
        "api_key": decrypt_secret(cfg.api_key_ref),
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "purpose": purpose,
    }


ModelConfigDep = Annotated[dict, Depends(get_model_config)]


@dataclass
class Pagination:
    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Pagination:
    return Pagination(page=page, size=size)


PageDep = Annotated[Pagination, Depends(pagination)]
