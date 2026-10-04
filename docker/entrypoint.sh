#!/bin/sh
set -eu

# 数据根目录：默认库、平台分库、密钥、日志全在它下面
# （布局见 core/paths.py 与 docs/DATA_DIRECTORY.md）。
RUNTIME_DIR="${DATA_DIR:-${APP_RUNTIME_DIR:-/runtime}}"
KEY_FILE="${CREDENTIAL_ENCRYPTION_KEY_FILE:-${RUNTIME_DIR}/secrets/credential_key}"

# 只建代码真正会写的目录。`mail/` 与 `plugins/{repos,logs}` 曾在这里预建，
# 但随着邮箱池改分库（data/platforms/*.db）与本地插件管理整块删除，已经
# 没有任何代码路径使用它们 —— 留着只会在挂载卷里堆空目录、让运维以为
# 那些功能还在。
mkdir -p \
  "${RUNTIME_DIR}/logs" \
  "${RUNTIME_DIR}/platforms" \
  "$(dirname "${KEY_FILE}")"

# 凭据加密密钥必须和数据库一起留在挂载卷里。放在镜像内的默认位置
# （/app/data/secrets）会在每次重建容器时重新生成，导致库里已加密的
# iCloud/ChatGPT 凭据全部解不开（decrypt 抛 InvalidTag），且无补救手段。
chmod 700 "$(dirname "${KEY_FILE}")"

# 数据目录只有一个来源：DATA_DIR 已决定默认库在哪，不必再往 /app 下塞软链接
# ——那只会在排查问题时多出"同一个库两条路径"的干扰。
echo "[entrypoint] 数据目录: ${RUNTIME_DIR}"

echo "[entrypoint] Starting backend under Xvfb so Docker can handle both headed and headless browser tasks"
exec xvfb-run -a --server-args="-screen 0 1920x1080x24" python main.py
