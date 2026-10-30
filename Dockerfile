# DiffBot — Linux 容器镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DIFFBOT_DATA_DIR=/app/data

WORKDIR /app

# 依赖层独立缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 非 root 用户运行
RUN groupadd -r bot && useradd -r -g bot -d /app -s /usr/sbin/nologin bot \
    && mkdir -p /app/data /app/config \
    && chown -R bot:bot /app

COPY --chown=bot:bot . .

USER bot

# 健康检查：心跳文件 + SQLite 可读
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "diffbot_core.healthcheck"]

# exec 形式确保 SIGTERM 直达 python，优雅退出
CMD ["python", "main.py"]
