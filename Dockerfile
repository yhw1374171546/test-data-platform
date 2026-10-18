# syntax=docker/dockerfile:1
#
# 单阶段就够：依赖全是纯 Python 或带 manylinux wheel（psycopg[binary]），
# 不需要在镜像里装编译器和 libpq-dev —— 多阶段只会让缓存更复杂。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 先只拷依赖清单：改代码不会让 pip 缓存失效
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# 非 root 运行（容器里的常规做法）
RUN useradd --create-home --uid 10001 tdp \
    && chown -R tdp:tdp /srv
USER tdp

EXPOSE 8000

# 用 Python 探活，不依赖 curl（slim 镜像里没有）。
# 注意写成一行：Dockerfile 的反斜杠续行 + shell 双引号叠在一起很容易出坑。
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# 默认起 API；worker 用 compose 覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
