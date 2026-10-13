"""认证增强测试（批次 C / F09）：双 token、版本失效、角色、改密、时区。"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.security import (
    create_token,
    decode_token,
    hash_password,
    password_strength,
    token_remaining_seconds,
    verify_password,
)


# ============================================================ 口令强度
def test_password_strength_rules():
    assert password_strength("short")[0] is False
    assert password_strength("12345678")[0] is False  # 纯数字
    assert password_strength("abcdefgh")[0] is False  # 纯字母
    assert password_strength("abcd1234")[0] is True
    assert password_strength("x" * 200)[0] is False  # 过长


def test_password_hash_roundtrip():
    h = hash_password("my-secret-123")
    assert verify_password("my-secret-123", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("my-secret-123", "garbage") is False


def test_password_hash_is_salted():
    """同一口令两次散列结果必须不同（加盐）。"""
    assert hash_password("same-pass-1") != hash_password("same-pass-1")


# ============================================================== Token
def test_token_kinds_are_not_interchangeable():
    """refresh token 不能当 access 用 —— 否则长命 token 会绕过短命设计。"""
    access = create_token("u1", "bob", kind="access")
    refresh = create_token("u1", "bob", kind="refresh")

    assert decode_token(access, expect="access") is not None
    assert decode_token(refresh, expect="refresh") is not None
    assert decode_token(refresh, expect="access") is None
    assert decode_token(access, expect="refresh") is None


def test_token_carries_version_and_role():
    tok = create_token("u1", "bob", kind="access", token_version=7, role="admin")
    payload = decode_token(tok)
    assert payload is not None
    assert payload["ver"] == 7
    assert payload["role"] == "admin"
    assert payload["typ"] == "access"


def test_token_expiry():
    tok = create_token("u1", "bob", ttl=-1)
    assert decode_token(tok) is None
    assert token_remaining_seconds(tok) == 0


def test_token_signature_tampering_rejected():
    tok = create_token("u1", "bob")
    body, sig = tok.split(".")
    assert decode_token(f"{body}.{sig[:-2]}xx") is None


# ======================================================= 认证接口流程
async def test_register_returns_dual_tokens(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"dual{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] > 0
    assert body["refresh_expires_in"] > body["expires_in"]
    assert body["role"] in ("user", "admin")


async def test_weak_password_rejected(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"weak{uuid.uuid4().hex[:8]}", "password": "12345678"},
    )
    assert r.status_code == 422, r.text


async def test_invalid_username_pattern_rejected(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": "bad name!", "password": "secret12345"},
    )
    assert r.status_code == 422


async def test_refresh_flow(client):
    """refresh token 换新 token，且新 access 可用。"""
    username = f"ref{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret12345"}
    )
    refresh_token = r.json()["refresh_token"]

    r2 = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 200, r2.text
    new_access = r2.json()["access_token"]

    r3 = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert r3.status_code == 200
    assert r3.json()["username"] == username


async def test_refresh_rejects_access_token(client):
    """拿 access token 去 refresh 必须被拒（类型校验）。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": f"mix{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    access = r.json()["access_token"]
    r2 = await client.post("/api/auth/refresh", json={"refresh_token": access})
    assert r2.status_code == 401


async def test_access_token_cannot_be_used_where_refresh_expected(client):
    """反之亦然：refresh 不能当 access 用（直接访问受保护接口）。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": f"rev{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    refresh = r.json()["refresh_token"]
    r2 = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert r2.status_code == 401


async def test_change_password_invalidates_all_tokens(client):
    """核心主张：改密后所有旧 token 立即失效（无需黑名单）。"""
    username = f"chg{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret12345"}
    )
    old_access = r.json()["access_token"]
    old_refresh = r.json()["refresh_token"]
    h = {"Authorization": f"Bearer {old_access}"}

    # 改密前可用
    assert (await client.get("/api/auth/me", headers=h)).status_code == 200

    r2 = await client.post(
        "/api/auth/change-password",
        json={"old_password": "secret12345", "new_password": "newsecret456"},
        headers=h,
    )
    assert r2.status_code == 200, r2.text

    # 改密后：旧 access 失效
    r3 = await client.get("/api/auth/me", headers=h)
    assert r3.status_code == 401, "改密后旧 access token 仍可用"

    # 旧 refresh 也失效
    r4 = await client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r4.status_code == 401, "改密后旧 refresh token 仍可用"

    # 新密码可登录
    r5 = await client.post(
        "/api/auth/login", json={"username": username, "password": "newsecret456"}
    )
    assert r5.status_code == 200


async def test_change_password_rejects_wrong_old(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"wp{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r2 = await client.post(
        "/api/auth/change-password",
        json={"old_password": "wrongpass1", "new_password": "newsecret456"},
        headers=h,
    )
    assert r2.status_code == 400


async def test_change_password_rejects_same_password(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"same{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r2 = await client.post(
        "/api/auth/change-password",
        json={"old_password": "secret12345", "new_password": "secret12345"},
        headers=h,
    )
    assert r2.status_code == 400


async def test_logout_all_devices_invalidates_tokens(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"lo{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    tok = r.json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/auth/me", headers=h)).status_code == 200

    r2 = await client.post("/api/auth/logout", params={"all_devices": True}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["all_devices"] is True

    assert (await client.get("/api/auth/me", headers=h)).status_code == 401


async def test_logout_this_device_keeps_token_valid(client):
    """单设备登出只提示前端丢弃 token，服务端不失效（无状态设计的取舍）。"""
    r = await client.post(
        "/api/auth/register",
        json={"username": f"lo1{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    tok = r.json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    r2 = await client.post("/api/auth/logout", headers=h)
    assert r2.status_code == 200
    assert r2.json()["all_devices"] is False
    # token 仍然有效 —— 这是无状态 token 的已知取舍，已在前端配合丢弃
    assert (await client.get("/api/auth/me", headers=h)).status_code == 200


# ============================================================== 资料
async def test_update_profile(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"prof{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r2 = await client.patch(
        "/api/auth/me",
        json={"nickname": "新昵称", "level": 4, "daily_goal": 5, "timezone": "UTC"},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["nickname"] == "新昵称"
    assert body["level"] == 4
    assert body["daily_goal"] == 5
    assert body["timezone"] == "UTC"


async def test_update_profile_rejects_bad_timezone(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"tz{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r2 = await client.patch("/api/auth/me", json={"timezone": "Mars/Olympus"}, headers=h)
    assert r2.status_code == 422


async def test_me_exposes_role_and_timezone(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": f"me{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    body = (await client.get("/api/auth/me", headers=h)).json()
    assert "role" in body
    assert "timezone" in body


# ============================================================== 管理员
async def test_first_user_becomes_admin(client):
    """首个注册用户自动成为管理员（首次部署省去手工提权）。"""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import User

    async with session_scope() as db:
        count = (await db.execute(select(func.count()).select_from(User))).scalar_one()

    r = await client.post(
        "/api/auth/register",
        json={"username": f"first{uuid.uuid4().hex[:8]}", "password": "secret12345"},
    )
    role = r.json()["role"]
    if int(count) == 0:
        assert role == "admin"
        assert settings.FIRST_USER_IS_ADMIN is True
    else:
        assert role == "user"


async def test_admin_dependency_blocks_normal_user(client):
    """非管理员访问管理接口必须 403。"""
    from fastapi import HTTPException

    from app.deps import CurrentUser, require_admin

    with pytest.raises(HTTPException) as exc:
        await require_admin(CurrentUser(id="u1", username="bob", role="user"))
    assert exc.value.status_code == 403

    # 管理员通过
    ok = await require_admin(CurrentUser(id="u1", username="root", role="admin"))
    assert ok.is_admin is True
