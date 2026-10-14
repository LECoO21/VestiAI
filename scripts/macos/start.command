#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

START_PYTHON="${VESTIAI_PYTHON:-}"
if [[ -z "$START_PYTHON" ]]; then
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      START_PYTHON="$candidate"
      break
    fi
  done
fi
if [[ -z "$START_PYTHON" ]] || ! command -v "$START_PYTHON" >/dev/null 2>&1 || ! "$START_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf '未找到 Python 3.11 或更高版本。安装后重新启动，或设置 VESTIAI_PYTHON 指定路径。\n'
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  printf '首次启动：正在创建本地 Python 环境…\n'
  "$START_PYTHON" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf '现有 .venv 使用了旧版 Python。请将 .venv 重命名保留，再重新启动。衣橱数据不受影响。\n'
  exit 1
fi

printf '正在检查依赖…\n'
.venv/bin/python -m pip install --disable-pip-version-check --quiet -r requirements.txt

.venv/bin/python "$SCRIPT_DIR/server.py" start
printf '服务已在后台运行，可以关闭此终端窗口。停止服务请双击 stop.command。\n'
open "http://127.0.0.1:${VESTIAI_HTTP_PORT:-5050}"
