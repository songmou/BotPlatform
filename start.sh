#!/bin/sh

set -e

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
MODEL_ENV_FILE="$PROJECT_DIR/data/system/model.env"
cd "$PROJECT_DIR"

if [ -f "$MODEL_ENV_FILE" ]; then
  if stat -f '%Lp' "$MODEL_ENV_FILE" >/dev/null 2>&1; then
    MODEL_ENV_MODE=$(stat -f '%Lp' "$MODEL_ENV_FILE")
  else
    MODEL_ENV_MODE=$(stat -c '%a' "$MODEL_ENV_FILE")
  fi
  if [ "$MODEL_ENV_MODE" != "600" ]; then
    echo "模型密钥文件权限必须是 0600：$MODEL_ENV_FILE" >&2
    exit 1
  fi
  MODEL_ENV_COUNT=0
  while IFS='=' read -r MODEL_ENV_NAME MODEL_ENV_VALUE; do
    [ -z "$MODEL_ENV_NAME" ] && continue
    if [ "$MODEL_ENV_NAME" != "DEEPSEEK_API_KEY" ] || [ -z "$MODEL_ENV_VALUE" ] || [ "$MODEL_ENV_COUNT" != "0" ]; then
      echo "模型密钥文件只能包含非空的 DEEPSEEK_API_KEY：$MODEL_ENV_FILE" >&2
      exit 1
    fi
    export DEEPSEEK_API_KEY="$MODEL_ENV_VALUE"
    MODEL_ENV_COUNT=1
  done < "$MODEL_ENV_FILE"
  if [ "$MODEL_ENV_COUNT" != "1" ]; then
    echo "模型密钥文件缺少 DEEPSEEK_API_KEY：$MODEL_ENV_FILE" >&2
    exit 1
  fi
fi

if [ ! -x "$VENV_PYTHON" ]; then
  echo "未找到 Python 虚拟环境：$PROJECT_DIR/.venv" >&2
  echo "请先执行：cd \"$PROJECT_DIR\" && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

# pip 等辅助脚本的 shebang 可能保留移动前的绝对路径，但本启动器并不
# 直接调用它们。以 Python 实际报告的 sys.prefix 判断环境是否仍可用，
# 并始终通过 `.venv/bin/python -m pip` 管理依赖。
if ! VENV_PREFIX=$(
  "$VENV_PYTHON" -c 'import os, sys; print(os.path.realpath(sys.prefix))' 2>/dev/null
); then
  echo "Python 虚拟环境无法运行：$PROJECT_DIR/.venv" >&2
  echo "请重新创建 .venv，并使用 .venv/bin/python -m pip 安装依赖。" >&2
  exit 1
fi

EXPECTED_PREFIX=$(CDPATH= cd -- "$PROJECT_DIR/.venv" && pwd -P)
if [ "$VENV_PREFIX" != "$EXPECTED_PREFIX" ]; then
  echo "Python 虚拟环境仍绑定其他目录：$VENV_PREFIX" >&2
  echo "请重新创建：$PROJECT_DIR/.venv" >&2
  exit 1
fi

if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "BotPlatform 要求 Python 3.10 或更高版本，请重新创建 .venv。" >&2
  exit 1
fi

if [ "$1" = "web" ]; then
  shift
  exec "$VENV_PYTHON" "$PROJECT_DIR/web.py" "$@"
fi

exec "$VENV_PYTHON" "$PROJECT_DIR/main.py" "$@"
