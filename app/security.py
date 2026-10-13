"""认证与安全：PBKDF2 口令散列 + 自签 HMAC Token（零外部依赖）。

为什么不用 passlib/jose：减少依赖面，PBKDF2-HMAC-SHA256 是标准库能力，
签名 token 用 HMAC-SHA256 足够覆盖本项目场景（可平滑替换为 JWT）。

批次 C 增强：
  * access / refresh 双 token —— access 短命（默认 2h），refresh 长命（默认 30d）；
  * token 版本号 —— 改密或"登出所有设备"时版本 +1，所有旧 token 立即失效，
    无需黑名单存储；
  * 角色 —— user / admin，管理接口据此鉴权。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Literal

from .config import settings

_ITERATIONS = 120_000

TokenKind = Literal["access", "refresh"]


# ------------------------------------------------------------------- 口令
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def password_strength(password: str) -> tuple[bool, str]:
    """口令强度检查：返回 (是否合格, 原因)。

    规则有意保持简单可解释（不含特殊字符强要求，避免用户写便签）：
    长度 >= 8，且不是纯数字/纯字母。
    """
    if len(password) < 8:
        return False, "密码至少 8 位"
    if len(password) > 128:
        return False, "密码过长（最多 128 位）"
    if password.isdigit():
        return False, "密码不能是纯数字"
    if password.isalpha():
        return False, "密码不能是纯字母"
    return True, ""


# -------------------------------------------------------------------- Token
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_token(
    user_id: str,
    username: str = "",
    ttl: int | None = None,
    *,
    kind: TokenKind = "access",
    token_version: int = 0,
    role: str = "user",
) -> str:
    """签发 token。

    payload 里的 ver 与用户当前 token_version 比对：不匹配即视为已失效。
    这样改密/登出后无需遍历黑名单，一次比对即可全量踢下线。
    """
    now = int(time.time())
    if ttl is None:
        ttl = settings.REFRESH_TTL_SECONDS if kind == "refresh" else settings.ACCESS_TTL_SECONDS
    payload = {
        "sub": user_id,
        "name": username,
        "role": role,
        "ver": int(token_version),
        "typ": kind,
        "iat": now,
        "exp": now + int(ttl),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def decode_token(token: str, *, expect: TokenKind | None = None) -> dict[str, Any] | None:
    """校验签名与有效期，返回 payload。

    expect 用于强制 token 类型：refresh token 不能当 access 用（反之亦然），
    否则 refresh 的长有效期会绕过 access 的短命设计。
    """
    try:
        body, sig = token.split(".")
        expected = hmac.new(settings.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if expect is not None and payload.get("typ", "access") != expect:
            return None
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


def token_remaining_seconds(token: str) -> int:
    """token 剩余有效秒数（用于响应里的 expires_in）。"""
    payload = decode_token(token)
    if payload is None:
        return 0
    return max(0, int(payload.get("exp", 0)) - int(time.time()))


def mask_key(api_key: str) -> str:
    """密钥脱敏：对外只暴露前后各 4 位。"""
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return "*" * len(api_key)
    return f"{api_key[:4]}{'*' * 8}{api_key[-4:]}"
