# PR-2 — 五檔行情與市場觀察（唯讀）

狀態：IMPLEMENTATION_REVIEW / LIVE_BIDASK_NOT_VERIFIED（2026-10-08）
上游基底：PR-1 \`feat/shioaji-tick-collector-pr1\`；本 PR 是 stacked PR，只包含相對 PR-1 的增量。

## 已實作

- \`intraday_book.py\`：SDK BidAskSTKv1 raw envelope，BOARD/ODD 分離，0–5 個有效價格檔位正規化為五欄（缺層 = null），必須有買價嚴格遞減、賣價嚴格遞增、買一價低於賣一價，否則拒絕為品質異常。
- raw 路徑：\`data/raw/shioaji/bidask/YYYY-MM-DD/BOARD|ODD/<symbol>.jsonl\`，和 Tick 路徑分離。原始 \`diff_bid_vol/diff_ask_vol\` 僅作稽核，不推斷撤單或實際成交。
- 五檔報價量從 SDK 單位換成**股**：整股張數×1000；零股以股計。計算 spread（價格 x10000 差值）、買五/賣五可見總股數、visible-order imbalance（bps）。**不是完整委託簿或成交量。**
- \`intraday_observations.py\`：純函式 \`observe_book\` 與 \`replay_observations\`，輸出 deterministic \`ObservationEvent\`（SPREAD、BOOK_IMBALANCE、SUPPORT_TESTED/HELD/BROKEN、BREAKOUT_OBSERVED/RETESTED、PRICE_LEVEL_CHECK/INCONCLUSIVE）。
- 需要事先固定 \`ObservationPlan\` 才能分析支撐與突破；條件需要多筆實際 Tick，並且有新鮮且同市場/同 lot/同 session 的 BidAsk；資料延遲／缺口／來源異常須標 INCONCLUSIVE，而不是買入確認。
- \`ShioajiTickCollector\` 可選擇掛上 BidAsk callback、共用單一 worker queue、API 登入、subscribe/unsubscribe 與 health。預設 \`book_quote_type=None\`，不改 PR-1 的 Tick-only 行為。

## CLI：完全離線

\`\`\`bash
# 只分析 raw 五檔，無策略支撐價，不連線
python3 -m app market intraday-book-replay \
  --books data/raw/shioaji/bidask/2026-10-08/BOARD/2327.jsonl \
  --output artifacts/reports/intraday/books.json

# 事先建立並封存 plan JSON，才可計算相同期間的支撐或突破
python3 -m app market intraday-book-replay \
  --books data/raw/shioaji/bidask/2026-10-08/BOARD/2327.jsonl.gz \
  --ticks data/raw/shioaji/ticks/2026-10-08/BOARD/2327.jsonl.gz \
  --plan data/observation-plans/2327-20261008.json \
  --output artifacts/reports/intraday/observations.json
\`\`\`

事前計畫範例（**僅測試資料，非國巨真實支撐價或建議**）：

\`\`\`json
{
  "plan_id": "demo-2327-20261008",
  "symbol": "2327",
  "exchange": "TSE",
  "lot_type": "BOARD",
  "known_at": "2026-10-07T20:00:00+08:00",
  "support_low_x10000": 6350000,
  "support_high_x10000": 6360000,
  "resistance_x10000": 6400000,
  "min_confirmation_ticks": 2,
  "min_confirmation_seconds": 10,
  "max_book_age_seconds": 30,
  "max_tick_gap_seconds": 90
}
\`\`\`

不能事後看當日最低價才挑支撐，不能讓已落後的 BidAsk 當確認依據。

**研究品質預設 fail-closed：**離線 CLI 的 `--data-health` 預設 `UNKNOWN`，因此未取得資料完整性證據時，只能輸出 `INCONCLUSIVE`，不得做支撐守住／突破確認。經核對收集器 session 品質後才可顯式指定 `--data-health HEALTHY`；這是研究證據標記而不是自動判斷。缺少買方或賣方整側時，五檔 imbalance 為 `null`，不得當成 100% 買賣盤。

## Live smoke：雙閘門未核准

PR-1 的 \`market intraday-smoke\` 新增可選 \`--with-bidask\`，表示在**同一帳號、同一 Collector** 訂閱 Tick 與 BidAsk；仍須滿足 PR-1 的開關、盤中與 1–60 秒限制。這次**沒有執行**，亦未啟動 CA、任何交易功能、systemd 或 daily cron。

需另獲一次性明確核准才可執行真實 BidAsk smoke，應驗證登入 API 1.7.x、整股與零股推播實際價格量單位、callback 介面、訂閱上限、資料延遲、斷線重連、事件順序與交易所時間。通過後每日上線蒐集仍需另行授權。

## 測試與限制

\`\`\`bash
python3 -m pytest tests/unit/test_intraday_tick_collector.py tests/unit/test_intraday_book.py -q
\`\`\`

PR-2 不新增下單功能，不寫 \`order_intents\`、\`approval\`、\`fills\`、\`cash_ledger\`、真實交易 DB；不改 \`config/trading.yaml\` REJECTED 策略。僅建立供 PR-3 再研究、PR-4 畫面展示的可驗證資料契約。五檔初次 smoke 未完成前，功能狀態為 LIVE_NOT_VERIFIED。
