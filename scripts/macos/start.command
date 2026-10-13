#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

if ! command -v python3 >/dev/null 2>&1; then
  printf '未找到 Python 3。请先安装 Python 3.11 或更高版本。\n'
  read -r _
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  printf '首次启动：正在创建本地 Python 环境…\n'
  python3 -m venv .venv
fi

printf '正在检查依赖…\n'
.venv/bin/python -m pip install --disable-pip-version-check --quiet -r requirements.txt

(
  for _ in {1..50}; do
    if curl --silent --fail http://127.0.0.1:5050/api/health >/dev/null 2>&1; then
      open http://127.0.0.1:5050
      exit 0
    fi
    sleep 0.2
  done
) &

printf 'FitMix 已启动：http://127.0.0.1:5050\n'
printf '关闭此窗口或按 Control-C 即可停止。\n\n'
exec .venv/bin/python -m fitmix.app
