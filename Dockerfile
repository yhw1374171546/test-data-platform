# syntax=docker/dockerfile:1
#
# 两阶段：先用 node 构建前端，再拷进 Python 运行时镜像。
# 为什么不"本地构建好 dist 再 COPY"：那样镜像依赖构建机的手工步骤，
# 换台机器 / 上 CI 就得记得先跑一次 pnpm build —— 迟早忘，然后线上 /ui 就 404。
#
# Python 侧仍然单阶段：依赖全是纯 Python 或带 manylinux wheel（psycopg[binary]），
# 不需要在镜像里装编译器和 libpq-dev。

# ── 阶段 1：前端 ──
FROM node:22-alpine AS web
WORKDIR /web
# pnpm 版本要和锁文件生成时一致，否则 --frozen-lockfile 会因 lockfileVersion 不匹配直接失败
RUN npm install -g pnpm@11.7.0
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ── 阶段 2：运行时 ──
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 先只抱依赖清单：改代码不会让 pip 缓存失效
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
# alembic.ini 不是运行必需（init_db 在代码里构造 Config），
# 但拷进来之后容器里也能直接 `alembic upgrade head` / `alembic history`
COPY alembic.ini ./
# 前端产物：少了这一行，线上 /ui 就是 404（本地开发看不出来，因为直连 vite dev server）
COPY --from=web /web/dist ./web/dist

# 非 root 运行（容器里的常规做法）
RUN useradd --create-home --uid 10001 tdp \
    && chown -R tdp:tdp /srv
USER tdp

EXPOSE 8000

# 用 Python 探活，不依赖 curl（slim 镜像里没有）。
# 注意写成一行：Dockerfile 的反斜杠续行 + shell 双引号叠在一起很容易出坑。
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# 默认起 API；worker / scheduler 由 compose 覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
