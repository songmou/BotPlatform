# syntax=docker/dockerfile:1
# BotPlatform Docker 镜像
# 基于 Python 3.12 slim，预装 Playwright 浏览器及项目依赖

FROM python:3.12-slim

# ============================================================
# 构建参数（国内用户无需修改，默认已使用清华镜像加速）
# 如需切换 pip 源：docker compose build --build-arg PIP_INDEX_URL=...
# 如需切换 apt 源：docker compose build --build-arg APT_MIRROR=...
# ============================================================
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn

# 环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BOTPLATFORM_DATA_DIR=/data \
    TZ=Asia/Shanghai

# === 配置 apt 清华镜像源 ===
RUN sed -i \
    "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g" \
    /etc/apt/sources.list.d/debian.sources

# === 安装系统运行时依赖（Playwright Chromium + 常用工具）===
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 创建工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存加速构建
COPY requirements.txt requirements-db.txt ./

# === 安装 Python 依赖（使用 pip 镜像源）===
RUN pip install --upgrade pip \
    && pip install --index-url "$PIP_INDEX_URL" -r requirements.txt \
    && pip install --index-url "$PIP_INDEX_URL" -r requirements-db.txt

# === 安装 Playwright Chromium 浏览器 ===
# playwright install --with-deps 会自动调用 apt-get，保持使用已配置的镜像源
RUN playwright install --with-deps chromium

# 复制项目源码
COPY . .

# 创建数据目录挂载点
VOLUME ["/data"]

# 暴露 Web 管理面板端口
EXPOSE 8080

# 默认命令：启动 Web 管理面板 + 微信机器人
# 可通过 docker run 覆盖为其他模式：
#   python main.py          → 仅 CLI 模式（不含 web）
#   python web.py --panel-only → 仅 Web 面板
ENTRYPOINT ["python", "web.py"]
CMD ["--host", "0.0.0.0", "--port", "8080"]