# 注意：这里刻意**不写** `# syntax=docker/dockerfile:1.7` 指令。该指令会强制
# BuildKit 从 Docker Hub 拉取前端镜像（docker/dockerfile:1.7），在受限网络
# （Docker Hub 被墙）下构建会在第一步直接失败（实测 no route to host）。
# 本文件没有用到需要显式前端镜像的语法特性（heredoc / --mount / --link 等），
# 去掉后走内置前端，受限网络里也能构建。

# 基础镜像可覆盖：受限网络（Docker Hub 被墙等）时用 --build-arg 换成可达镜像源，
# 例：docker build --build-arg NODE_IMAGE=docker.m.daocloud.io/library/node:20-bookworm-slim ...
# 两个 ARG 都必须在**首个 FROM 之前**声明 —— ARG 是 stage 作用域，声明在某个
# FROM 之后的 ARG 进不了下一个 FROM 的解析（实测报 "base name should not be blank"）。
ARG NODE_IMAGE=node:20-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM ${PYTHON_IMAGE} AS runtime

# 浏览器版本说明：脚本**优先读 camoufox 包自带的 browser-pin.json**（库升级时
# 浏览器自动跟着升，永不错配）。这两个 ARG 只是兜底 —— 仅当包不带 pin
# （开发版）时使用；两者都没有时构建会明确报错退出。
ARG CAMOUFOX_VERSION=135.0.1
ARG CAMOUFOX_RELEASE=beta.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_CONDA_ENV=docker \
    APP_RELOAD=0 \
    APP_RUNTIME_DIR=/runtime \
    DATA_DIR=/runtime \
    CREDENTIAL_ENCRYPTION_KEY_FILE=/runtime/secrets/credential_key \
    APP_ENABLE_SOLVER=1 \
    SOLVER_PORT=8889 \
    SOLVER_BIND_HOST=0.0.0.0 \
    LOCAL_SOLVER_URL=http://127.0.0.1:8889 \
    SOLVER_BROWSER_TYPE=camoufox

WORKDIR /app

COPY requirements.txt ./
COPY scripts/install_camoufox.py /tmp/install_camoufox.py

# nodejs 不是构建期依赖：ChatGPT 的 Sentinel PoW 求解器要在运行时起 node 子进程
# 跑 OpenAI 的 sdk.js，缺它注册链路会静默收不到验证码。
# tini 是 PID 1 的 init：xvfb-run 作为 PID 1 会卡死（实测 Xvfb 就绪后不向
# PID 1 发 USR1，xvfb-run 的 wait 无限阻塞；同一脚本加 --init 立刻正常），
# 镜像内自带 tini 后裸 docker run 与 compose 两种启动方式都正常。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates nodejs tini \
        libgtk-3-0 libx11-xcb1 libasound2 xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && installed=0 \
    && for attempt in 1 2 3; do \
         if python -m playwright install --with-deps chromium firefox; then \
           installed=1; \
           break; \
         fi; \
         if [ "$attempt" -eq 3 ]; then break; fi; \
         echo "playwright browser install failed, retrying ($attempt/3)..." >&2; \
         sleep 5; \
       done \
    && [ "$installed" -eq 1 ] \
    && CAMOUFOX_VERSION="$CAMOUFOX_VERSION" CAMOUFOX_RELEASE="$CAMOUFOX_RELEASE" python /tmp/install_camoufox.py

COPY . .
COPY --from=frontend-builder /app/static /app/static

RUN apt-get update && apt-get install -y --no-install-recommends dos2unix iproute2 procps \
    && dos2unix /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /runtime /runtime/logs /runtime/secrets /runtime/platforms \
    && rm -rf /var/lib/apt/lists/*
# 注：iproute2 / procps 供运维 exec 进容器排障（ss / ps）；git 已移除 ——
# 它唯一的消费者是 main.py 的版本戳，而 .git 被 .dockerignore 排除，
# 容器里永远读到「未知」，装了只是白付 25-35MB 镜像体积。

EXPOSE 8000 8889

VOLUME ["/runtime"]

# ENTRYPOINT 用 tini 包裹：容器 PID 1 的职责是转发信号 + 收尸，xvfb-run
# 自己干不了这活（作 PID 1 时卡死，见上面 tini 注释）。
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
