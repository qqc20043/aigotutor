# AIgoTutor 生产镜像（多阶段构建）
#
# 阶段 1 构建前端，阶段 2 只带运行所需内容 —— 最终镜像不含 node_modules
# 与前端源码，体积与攻击面都更小。

# ---------------------------------------------------------------- 前端构建
FROM node:20-alpine AS web-builder

WORKDIR /build/web

# 先只拷贝依赖清单，利用 Docker 层缓存：改业务代码不会重装依赖
COPY web/package.json web/pnpm-lock.yaml* web/.npmrc web/pnpm-workspace.yaml* ./
RUN corepack enable && corepack prepare pnpm@9 --activate \
    && pnpm install --frozen-lockfile || pnpm install

COPY web/ ./
RUN pnpm build

# ---------------------------------------------------------------- 运行镜像
FROM python:3.12-slim AS runtime

# 判题需要 g++ 与 JRE；不需要 C++/Java 判题时可删掉这两行以缩小镜像
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ \
        default-jre-headless \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行：容器逃逸的影响面更小
RUN useradd --create-home --shell /bin/bash aigotutor

WORKDIR /app

# 依赖先装，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY alembic.ini pyproject.toml run.py ./

# 前端产物从构建阶段取
COPY --from=web-builder /build/web/dist ./web/dist

# 数据与日志目录（compose 里挂卷持久化）
RUN mkdir -p /app/data /app/logs && chown -R aigotutor:aigotutor /app

USER aigotutor

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    RELOAD=0 \
    AIGOTUTOR_DB=/app/data/aigotutor.db \
    AIGOTUTOR_LOG_JSON=1 \
    AIGOTUTOR_SERVE_WEB=1 \
    AIGOTUTOR_JUDGE_BACKEND=subprocess \
    AIGOTUTOR_RATE_LIMIT=1

EXPOSE 8000

# 健康检查：容器编排据此判断实例是否可用
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "run.py"]
