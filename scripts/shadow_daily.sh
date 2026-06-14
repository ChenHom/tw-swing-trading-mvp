#!/bin/bash
# scripts/shadow_daily.sh
#
# 影子先行每日流程（go-live 前用、cron 可掛）：
#   1. simulation run-daily（paper-trading，FakeBroker，永不串實盤）
#   2. 產生每日影子報告文字檔並記錄路徑
#   3. 不論 run 成功與否都產報告（FAILED 也要留下證據）
#
# 用法： scripts/shadow_daily.sh [account] [YYYY-MM-DD]
#   省略時 account=simulation-main、日期=今天（Asia/Taipei）。
#
# 退出碼：run-daily 失敗則為非 0（cron 可據此告警），但報告仍會產出。

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ACCOUNT="${1:-simulation-main}"
RUN_DATE="${2:-$(TZ=Asia/Taipei date +%Y-%m-%d)}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/shadow_daily.log"
mkdir -p "$LOG_DIR"

PYTHON_EXEC=".venv/bin/python"
[ -f "$PYTHON_EXEC" ] || PYTHON_EXEC="python3"

echo "=== shadow_daily $ACCOUNT $RUN_DATE $(date) ===" | tee -a "$LOG_FILE"

# 1) 每日模擬（影子）
$PYTHON_EXEC -m app simulation run-daily --account "$ACCOUNT" --date "$RUN_DATE" >> "$LOG_FILE" 2>&1
RUN_RC=$?
echo "run-daily 退出碼: $RUN_RC" | tee -a "$LOG_FILE"

# 2) 產生每日報告（無論成敗都產，以記錄 FAILED 狀態）
$PYTHON_EXEC scripts/daily_report.py --account "$ACCOUNT" --date "$RUN_DATE" > "$LOG_DIR/daily_report_stdout.txt" 2>> "$LOG_FILE"
REPORT_PATH=$(grep '^REPORT_PATH=' "$LOG_DIR/daily_report_stdout.txt" | tail -1 | cut -d= -f2-)
echo "報告路徑: ${REPORT_PATH:-<未產出>}" | tee -a "$LOG_FILE"

# 3) cron 告警鉤子：run-daily 非 0 即明確標示失敗
if [ "$RUN_RC" -ne 0 ]; then
  echo "⚠ ALERT: run-daily FAILED ($ACCOUNT $RUN_DATE)，請查 $LOG_FILE 與報告 $REPORT_PATH" | tee -a "$LOG_FILE"
fi

exit "$RUN_RC"
