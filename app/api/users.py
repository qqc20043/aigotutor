"""① 用户与配置 —— 认证、账号、模型配置。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from ..config import settings
from ..deps import DbSession, ModelConfigDep, UserDep
from ..models import User
from ..security import (
    create_token,
    decode_token,
    hash_password,
    mask_key,
    password_strength,
    verify_password,
)
from ..services import get_user_by_name, touch_login, upsert_model_config
from .schemas import (
    ChangePasswordIn,
    LoginIn,
    ModelConfigIn,
    ModelConfigOut,
    RefreshIn,
    RegisterIn,
    TokenOut,
    UpdateProfileIn,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["① 用户与配置"])


def _token_pair(user: User) -> TokenOut:
    """签发 access + refresh 双 token。"""
    access = create_token(
        user.id,
        user.username,
        kind="access",
        token_version=user.token_version,
        role=user.role,
    )
    refresh = create_token(
        user.id,
        user.username,
        kind="refresh",
        token_version=user.token_version,
        role=user.role,
    )
    return TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TTL_SECONDS,
        refresh_expires_in=settings.REFRESH_TTL_SECONDS,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )


async def _maybe_first_admin(db: DbSession) -> str:
    """首个注册用户自动成为管理员（首次部署省去手工提权）。"""
    if not settings.FIRST_USER_IS_ADMIN:
        return "user"
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    return "admin" if int(count) == 0 else "user"


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, db: DbSession):
    """注册并直接登录。首个用户自动获得管理员角色。"""
    if await get_user_by_name(db, body.username):
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")

    ok, reason = password_strength(body.password)
    if not ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)

    role = await _maybe_first_admin(db)
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        nickname=body.nickname or body.username,
        level=body.level,
        role=role,
        timezone=body.timezone,
    )
    db.add(user)
    await db.flush()
    return _token_pair(user)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: DbSession):
    """登录，返回双 token。"""
    user = await get_user_by_name(db, body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        # 不区分"用户不存在"与"密码错误"，避免用户名枚举
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")

    await touch_login(db, user)
    return _token_pair(user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: DbSession):
    """用 refresh token 换新的一对 token。

    校验点：签名、有效期、类型必须是 refresh、token 版本必须匹配。
    版本校验使"改密后旧 refresh 立即失效"，否则 refresh 的长寿命会变成后门。
    """
    payload = decode_token(body.refresh_token, expect="refresh")
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh Token 无效或已过期")

    user = await db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    if int(payload.get("ver", 0)) != int(user.token_version):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态已失效，请重新登录")

    return _token_pair(user)


@router.post("/logout", response_model=dict)
async def logout(db: DbSession, user: UserDep, all_devices: bool = False):
    """登出。

    all_devices=False：仅提示前端丢弃本地 token（token 是无状态签名，
    服务端不记录单次会话）。
    all_devices=True：token_version +1，所有设备上的旧 token 立即失效。
    """
    row = await db.get(User, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    if all_devices:
        row.token_version += 1
        await db.flush()
        return {"ok": True, "all_devices": True, "message": "所有设备已登出"}

    return {"ok": True, "all_devices": False, "message": "已登出（请前端丢弃本地 token）"}


@router.post("/change-password", response_model=dict)
async def change_password(body: ChangePasswordIn, db: DbSession, user: UserDep):
    """修改密码。成功后 token_version +1，所有旧 token 失效（含当前这个）。"""
    row = await db.get(User, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not verify_password(body.old_password, row.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")

    ok, reason = password_strength(body.new_password)
    if not ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)
    if verify_password(body.new_password, row.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与原密码相同")

    row.password_hash = hash_password(body.new_password)
    row.token_version += 1
    await db.flush()
    return {
        "ok": True,
        "message": "密码已修改，所有设备需重新登录",
        "token_version": row.token_version,
    }


@router.get("/me", response_model=UserOut)
async def me(db: DbSession, user: UserDep):
    """当前用户信息。"""
    row = await db.get(User, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return UserOut.model_validate(row, from_attributes=True)


@router.patch("/me", response_model=UserOut)
async def update_profile(body: UpdateProfileIn, db: DbSession, user: UserDep):
    """更新个人资料（昵称、自评等级、每日目标、时区）。"""
    row = await db.get(User, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    if body.nickname is not None:
        row.nickname = body.nickname
    if body.level is not None:
        row.level = body.level
    if body.daily_goal is not None:
        row.daily_goal = body.daily_goal
    if body.timezone is not None:
        if not _valid_timezone(body.timezone):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"无效时区：{body.timezone}（应为 IANA 名称，如 Asia/Shanghai）",
            )
        row.timezone = body.timezone
    await db.flush()
    return UserOut.model_validate(row, from_attributes=True)


def _valid_timezone(name: str) -> bool:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


config_router = APIRouter(prefix="/api/users", tags=["① 用户与配置"])


@config_router.get("/me/model-config", response_model=ModelConfigOut)
async def read_model_config(cfg: ModelConfigDep):
    """读取用户级模型配置（密钥脱敏）。"""
    return ModelConfigOut(
        provider=cfg["provider"],
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key_masked=mask_key(cfg.get("api_key", "")),
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        purpose=cfg["purpose"],
        enabled=True,
        is_default=cfg["provider"] == settings.DEFAULT_PROVIDER and not cfg.get("api_key"),
    )


@config_router.put("/me/model-config", response_model=ModelConfigOut)
async def write_model_config(body: ModelConfigIn, db: DbSession, user: UserDep):
    """写入用户级模型配置。"""
    cfg = await upsert_model_config(
        db,
        user.id,
        purpose=body.purpose,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url,
        api_key_ref=body.api_key,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        enabled=body.enabled,
    )
    return ModelConfigOut(
        provider=cfg.provider,
        model=cfg.model,
        base_url=cfg.base_url,
        api_key_masked=mask_key(cfg.api_key_ref),
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        purpose=cfg.purpose,
        enabled=cfg.enabled,
        is_default=False,
    )
