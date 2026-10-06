#!/usr/bin/env bash
# breakout_shadow_filter 正式研究一鍵執行（thesis：docs/strategies/breakout_shadow_filter.md）。
# 只碰 data/research.db 與其複本，不碰 live app.db。順序即 thesis 的凍結條件，不要調換。
#
#   scripts/run_breakout_shadow_filter_research.sh [research.db] [universe_policy]
#
# 前置：research.db 已有 2018~2026 行情與 PIT universe（README §8、engineering-log 2026-06-24）。
set -euo pipefail

cd "$(dirname "$0")/.."
DB="${1:-data/research.db}"
POLICY="${2:-liquidity-top150-v1}"
FROM=2018-01-01
TO=2026-06-22
CASH=300000
OUT=artifacts/reports/research
STAMP=$(date +%Y%m%d%H%M%S)

[ -f "$DB" ] || { echo "找不到 $DB"; exit 1; }
if [ -n "$(git status --porcelain -- src config scripts docs/strategies)" ]; then
  echo "工作區有未提交的程式／設定變更；thesis 要求以 commit 凍結後才跑正式研究。"; exit 1
fi
mkdir -p "$OUT"
echo "commit=$(git rev-parse HEAD)" | tee "$OUT/breakout_shadow_filter-$STAMP.meta"

# 1. 回測前寫入 regime gate（write-once；thesis §C）
python3 -m scripts.register_regime_gates --db "$DB"

# 2. 授權：限額與 trend_breakout 的有效授權相同（容量一致；thesis 前置條件 4）
read -r MAX_ORDER MAX_DAILY MAX_POS <<<"$(python3 - <<'EOF'
import json
amap = json.load(open("artifacts/approvals/active-approvals.json"))
m = json.load(open(f"artifacts/approvals/{amap['trend_breakout']['approval_id']}.json"))
l = m["limits"]
print(l["max_order_value"], l["max_daily_buy_value"], l["max_open_positions"])
EOF
)"
echo "manifest limits = $MAX_ORDER / $MAX_DAILY / $MAX_POS" | tee -a "$OUT/breakout_shadow_filter-$STAMP.meta"
APPROVAL="artifacts/approvals/approval-breakout_shadow_filter-$STAMP.json"
python3 -m app approval create --strategy config/strategies/breakout_shadow_filter.yaml \
  --expires-at 2026-12-31T23:59:59+08:00 --output "$APPROVAL" \
  --max-order-value "$MAX_ORDER" --max-daily-buy-value "$MAX_DAILY" --max-open-positions "$MAX_POS"
python3 -m app approval activate "$APPROVAL"

# 3. 凍結快照：兩支組合回測都從同一份快照複製（copy-per-run；thesis §C）
SHA=$(sha256sum "$DB" | cut -d' ' -f1)
echo "research_db_sha256=$SHA" | tee -a "$OUT/breakout_shadow_filter-$STAMP.meta"
cp "$DB" "data/_bsf_base_$STAMP.db"
cp "$DB" "data/_bsf_chal_$STAMP.db"

# 4. 主檢定：訊號層級配對分析（thesis §B）
python3 -m scripts.shadow_filter_study --db "$DB" --universe-policy "$POLICY" \
  --from "$FROM" --to "$TO" --price-basis raw \
  --output "$OUT/breakout_shadow_filter-signal-study-$STAMP.json"

# 5. 次要檢查：基準重跑 + challenger 組合回測（thesis §C）
python3 -m app backtest run --db "data/_bsf_base_$STAMP.db" --strategy trend_breakout \
  --from "$FROM" --to "$TO" --initial-cash "$CASH" --price-basis raw --universe-policy "$POLICY" \
  | tee "$OUT/breakout_shadow_filter-baseline-$STAMP.log"
python3 -m app backtest run --db "data/_bsf_chal_$STAMP.db" --strategy breakout_shadow_filter \
  --from "$FROM" --to "$TO" --initial-cash "$CASH" --price-basis raw --universe-policy "$POLICY" \
  | tee "$OUT/breakout_shadow_filter-challenger-$STAMP.log"

echo
echo "完成。依 thesis §C：先確認基準重跑仍為 RESEARCH_PASS、數字與 2026-06-24 一致，否則整個研究停止。"
echo "主檢定：$OUT/breakout_shadow_filter-signal-study-$STAMP.json（summary.verdict）"
