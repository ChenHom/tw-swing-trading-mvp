#!/bin/bash
# scripts/sync_sector_flow.sh — 族群資金：抓公開資料（TWSE/TPEx/TDCC）並重算網頁用的 dashboard.json
#
# cron（週一~五 22:00）：
#   0 22 * * 1-5 /usr/bin/flock -n /tmp/sector_flow_sync.lock /home/hom/services/stock/tw-day-trading/scripts/sync_sector_flow.sh >> /home/hom/services/stock/tw-day-trading/logs/sync_sector_flow_cron.log 2>&1
#
# 步驟 1 連網抓「今天往前 DAYS 天」到今天（Asia/Taipei）；步驟 2 無論 1 成功與否都重算 dashboard
# （離線，輸出 data/sector_flow/dashboard.json，網頁讀 GET /api/sector-flow）。任一步失敗 → exit 非 0 + Discord 告警。
#
# 用法：scripts/sync_sector_flow.sh [days]（預設 7）。

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

DAYS="${1:-7}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/sync_sector_flow.log"
mkdir -p "$LOG_DIR"

PYTHON_EXEC=".venv/bin/python"
[ -f "$PYTHON_EXEC" ] || PYTHON_EXEC="python3"

TODAY="$(TZ=Asia/Taipei date +%F)"
START="$(TZ=Asia/Taipei date -d "$TODAY - $DAYS days" +%F)"

echo "=== sync_sector_flow $(TZ=Asia/Taipei date) $START..$TODAY ===" | tee -a "$LOG_FILE"
echo "抓取中：每次請求間隔 ≥3 秒，約需 1～3 分鐘；明細寫在 $LOG_FILE"

$PYTHON_EXEC -m app market sync-sector-flow --start-date "$START" --end-date "$TODAY" >> "$LOG_FILE" 2>&1
RC1=$?
echo "sync-sector-flow 退出碼: $RC1" | tee -a "$LOG_FILE"

$PYTHON_EXEC -m app report sector-flow-dashboard --end-date "$TODAY" >> "$LOG_FILE" 2>&1
RC2=$?
echo "sector-flow-dashboard 退出碼: $RC2" | tee -a "$LOG_FILE"

RC=0
{ [ "$RC1" -ne 0 ] || [ "$RC2" -ne 0 ]; } && RC=1

# cron 告警鉤子：失敗發 Discord（失敗不阻擋）
if [ "$RC" -ne 0 ]; then
  $PYTHON_EXEC -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from src.notification.discord_alert import DiscordNotifier
DiscordNotifier().send_alert('⚠ sync_sector_flow FAILED（族群資金）sync=$RC1 dashboard=$RC2\n請查 $LOG_FILE', 'sync_sector_flow 失敗告警')
" 2>/dev/null || true
fi

exit "$RC"
