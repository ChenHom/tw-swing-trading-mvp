#!/bin/bash
# scripts/web_ui.sh — 啟動唯讀 Web 儀表板（掛 nginx 子路徑 /trading）
#
# 預設綁 127.0.0.1:8800，由 nginx 反向代理 /trading/ → 此服務。
# 環境變數：
#   TRADING_WEB_HOST（預設 127.0.0.1）TRADING_WEB_PORT（預設 8800）
#   TRADING_WEB_ROOT_PATH（預設 /trading）

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

HOST="${TRADING_WEB_HOST:-127.0.0.1}"
PORT="${TRADING_WEB_PORT:-8800}"
ROOT_PATH="${TRADING_WEB_ROOT_PATH:-/trading}"
export TRADING_WEB_ROOT_PATH="$ROOT_PATH"

PYTHON_EXEC=".venv/bin/python"
[ -f "$PYTHON_EXEC" ] || PYTHON_EXEC="python3"

exec "$PYTHON_EXEC" -m uvicorn src.web.server:app \
  --host "$HOST" --port "$PORT" --root-path "$ROOT_PATH"
