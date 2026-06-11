#!/bin/bash
# scripts/run_daily_sim.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/daily_sim.log"
TODAY=$(TZ=Asia/Taipei date +%Y-%m-%d)

# 確保 logs 目錄存在
mkdir -p "$PROJECT_DIR/logs"

cd "$PROJECT_DIR"

echo "=== $TODAY $(date) ===" >> "$LOG_FILE"

# 偵測是否存有虛擬環境，若無則回退至系統 Python
PYTHON_EXEC=".venv/bin/python"
if [ ! -f "$PYTHON_EXEC" ]; then
  PYTHON_EXEC="python3"
fi

$PYTHON_EXEC -m app simulation run-daily \
  --account simulation-main \
  --date "$TODAY" \
  >> "$LOG_FILE" 2>&1
