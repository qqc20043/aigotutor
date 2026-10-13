"""全局配置：从环境变量读取，默认值面向本地离线开发。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


class Settings:
    # --- 基础 ---
    APP_NAME: str = "AIgoTutor"
    SECRET_KEY: str = _env("AIGOTUTOR_SECRET", "dev-secret-change-me")
    # 兼容旧配置：TOKEN_TTL_SECONDS 作为 access token 的默认寿命
    TOKEN_TTL_SECONDS: int = int(_env("AIGOTUTOR_TOKEN_TTL", str(2 * 3600)))
    # access 短命（2 小时），refresh 长命（30 天）—— 降低 token 泄漏的影响面
    ACCESS_TTL_SECONDS: int = int(_env("AIGOTUTOR_ACCESS_TTL", str(2 * 3600)))
    REFRESH_TTL_SECONDS: int = int(_env("AIGOTUTOR_REFRESH_TTL", str(30 * 24 * 3600)))
    # 首个注册用户是否自动成为管理员（首次部署时省去手工提权）
    FIRST_USER_IS_ADMIN: bool = _env("AIGOTUTOR_FIRST_ADMIN", "1") == "1"

    # --- 数据库 ---
    DB_PATH: Path = Path(_env("AIGOTUTOR_DB", str(BASE_DIR / "data" / "aigotutor.db")))
    SQL_ECHO: bool = _env("AIGOTUTOR_SQL_ECHO", "0") == "1"

    # --- 模型默认配置（用户级配置缺省时使用） ---
    DEFAULT_PROVIDER: str = _env("AIGOTUTOR_PROVIDER", "mock")  # mock|openai|anthropic|gemini
    DEFAULT_MODEL: str = _env("AIGOTUTOR_MODEL", "mock-1")
    DEFAULT_BASE_URL: str = _env("AIGOTUTOR_BASE_URL", "")
    DEFAULT_API_KEY: str = _env("AIGOTUTOR_API_KEY", "")
    LLM_TIMEOUT: float = float(_env("AIGOTUTOR_LLM_TIMEOUT", "60"))
    LLM_MAX_RETRY: int = int(_env("AIGOTUTOR_LLM_RETRY", "2"))

    # --- 降级与成本控制 ---
    ROUTER_SIMPLE_TOKEN_LIMIT: int = int(_env("AIGOTUTOR_ROUTE_LIMIT", "1200"))
    CACHE_TTL_SECONDS: int = int(_env("AIGOTUTOR_CACHE_TTL", "3600"))

    # --- 任务 ---
    TASK_POLL_INTERVAL: float = float(_env("AIGOTUTOR_TASK_POLL", "0.3"))
    MAX_CONCURRENT_PIPELINES: int = int(_env("AIGOTUTOR_MAX_PIPELINES", "8"))

    # --- 判题沙箱 ---
    JUDGE_BACKEND: str = _env("AIGOTUTOR_JUDGE_BACKEND", "subprocess")  # subprocess|docker|none
    JUDGE_TIMEOUT_SECONDS: float = float(_env("AIGOTUTOR_JUDGE_TIMEOUT", "10"))
    JUDGE_MEMORY_MB: int = int(_env("AIGOTUTOR_JUDGE_MEMORY", "256"))
    JUDGE_OUTPUT_LIMIT: int = int(_env("AIGOTUTOR_JUDGE_OUTPUT_LIMIT", "65536"))
    JUDGE_DOCKER_IMAGE: str = _env("AIGOTUTOR_JUDGE_IMAGE", "python:3.13-slim")

    # --- 防护 ---
    RATE_LIMIT_ENABLED: bool = _env("AIGOTUTOR_RATE_LIMIT", "1") == "1"
    RATE_LIMIT_PER_MINUTE: int = int(_env("AIGOTUTOR_RATE_LIMIT_RPM", "120"))
    DEDUP_WINDOW_SECONDS: int = int(_env("AIGOTUTOR_DEDUP_WINDOW", "30"))
    MAX_REQUEST_BYTES: int = int(_env("AIGOTUTOR_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

    # --- 可观测性 ---
    # 本地开发用人类可读日志；生产置 1 输出 JSON 便于采集
    LOG_JSON: bool = _env("AIGOTUTOR_LOG_JSON", "0") == "1"
    LOG_LEVEL: str = _env("AIGOTUTOR_LOG_LEVEL", "INFO")

    # --- 前端 ---
    WEB_DIST: Path = Path(_env("AIGOTUTOR_WEB_DIST", str(BASE_DIR / "web" / "dist")))
    SERVE_FRONTEND: bool = _env("AIGOTUTOR_SERVE_WEB", "1") == "1"


settings = Settings()
settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
