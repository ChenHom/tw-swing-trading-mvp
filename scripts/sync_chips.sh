#!/bin/bash
# scripts/sync_chips.sh — 盤後籌碼同步（三大法人 / 融資券，FinMind）
#
# FinMind 三大法人/融資券於交易日（週一~五）晚間 20:30 官方更新，故排 21:00 全抓回。
# cron（週一~五 21:00）：
#   0 21 * * 1-5 /home/hom/services/stock/tw-day-trading/scripts/sync_chips.sh >> \
#       /home/hom/services/stock/tw-day-trading/logs/sync_chips_cron.log 2>&1
#
# 抓回後寫入 finmind_cache（原始回應）+ chip_*（聚合）；LLM 顧問之後只讀 DB、不打 API。
#
# 用法：scripts/sync_chips.sh [days]（預設 7，涵蓋當日 + 補漏/修正）。

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

DAYS="${1:-7}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/sync_chips.log"
mkdir -p "$LOG_DIR"

PYTHON_EXEC=".venv/bin/python"
[ -f "$PYTHON_EXEC" ] || PYTHON_EXEC="python3"

echo "=== sync_chips $(TZ=Asia/Taipei date) days=$DAYS ===" | tee -a "$LOG_FILE"

$PYTHON_EXEC -m app market sync-chips --days "$DAYS" >> "$LOG_FILE" 2>&1
RC=$?
echo "sync-chips 退出碼: $RC" | tee -a "$LOG_FILE"

# cron 告警鉤子：失敗發 Discord（失敗不阻擋）
if [ "$RC" -ne 0 ]; then
  $PYTHON_EXEC -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from src.notification.discord_alert import DiscordNotifier
DiscordNotifier().send_alert('⚠ sync_chips FAILED（籌碼同步）\n請查 $LOG_FILE', 'sync_chips 失敗告警')
" 2>/dev/null || true
fi

exit "$RC"
