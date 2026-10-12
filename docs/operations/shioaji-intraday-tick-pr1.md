# PR-1 Shioaji Tick — 隔離、唯讀的收集器

狀態：IMPLEMENTED FOR REVIEW / LIVE_SMOKE_NOT_AUTHORIZED（2026-10-08）
設計規格：docs PR #1 的 docs/planning/shioaji-intraday-observation/pr-01-tick-collector.md。
本 PR **不包含** BidAsk、策略訊號、交易執行、systemd 安裝/啟用或每日 cron。

## 介面與安全邊界

- \`src/market_data/intraday_tick.py\`：純函式 normalization、JSONL/.gz raw、保守磁碟閘門、分鐘 K deterministic replay。
- \`src/market_data/intraday_collector.py\`：SDK 注入（不依賴 SDK 套件）、同一 session 限定標的訂閱、bounded callback queue、raw-first、降級健康、停機取消訂閱。
- \`src/cli/intraday.py\`：計畫與重播離線可用；短時間真實 quote smoke 入口預設雙重封鎖。現有 src/market_data/provider.py 歷史 kbars 沒有被替換。
- 複用 tw-day-trading-lab 的「SDK adapter 與 pure normalization 分離」「事件時間」「不補零量 K」「raw / replay 同規則」原則；沒有跨 repo runtime import。
- 禁止 Shioaji 交易 CA、place_order、cancel_order；不寫交易 DB、cash_ledger、fills、approval、正式 OrderIntent，國泰買進策略原本 REJECTED 不變。

## 離線先執行

\`\`\`bash
# 只檢視手動 watchlist，不需憑證、不連網（會各列 BOARD / ODD）
python3 -m app market intraday-plan --symbols 2327,2360

# 同時讀取 app.db 目前持倉（只讀）、事先固定的 JSON watchlist 與候選清單
python3 -m app market intraday-plan \
  --positions-account 國泰 --positions-account simulation-main \
  --watchlist-file config/intraday-watchlist.local.json \
  --candidates-file data/intraday-candidates.json --max-symbols 20

# 原始單檔的 Tick replay；輸出每筆 canonical Tick 與 1m OHLCV
python3 -m app market intraday-replay \
  --input data/raw/shioaji/ticks/2026-10-08/BOARD/2327.jsonl \
  --output artifacts/reports/intraday/replay.json

# 確定該收集階段已停止，才手動壓縮原始 raw 檔；不設自動清理
python3 -m app market intraday-compress \
  --input data/raw/shioaji/ticks/2026-10-08/BOARD/2327.jsonl
\`\`\`

JSON 清單可用 \`["2327", "2360"]\`、\`{"symbols": ["2327"]}\` 或 \`{"candidates": [{"symbol":"2327"}]}\`。
Repo 規劃的每日候選清單讀取 adapter 尚待整合正式 candidate bundle contract；不要為了取得候選而從最新完整資料推回過去的候選清單。

## 真實行情 smoke（**尚未授權，不得執行**）

CLI 將所有登入及 SDK import 延後到雙重閘門後，且僅允許交易日盤中、1–60 秒：

\`\`\`bash
# 以下是日後需要「獨立獲得明確授權」才能執行的示意，
# 本次 PR 實作與審查不得執行：
INTRADAY_LIVE_SMOKE_APPROVED=yes \
  python3 -m app market intraday-smoke \
    --enable-live-smoke --duration-seconds 15 --symbols 2327 \
    --cache-dir data/raw --stop-at-disk-pct 75
\`\`\`

- 需具備環境既有 \`SHIOAJI_API_KEY\` / \`SHIOAJI_SECRET_KEY\`（不在 log 回顯）。
- \`simulation=False\` 僅用來領取真實**行情**；登入設定 \`subscribe_trade=False\`，不啟動 CA、不呼叫任何交易 API。SDK/帳號行情授權與 1.7.x 版本相容性必須獨立驗證。
- 此 smoke 不能做成 systemd/cron。即使之後通過 smoke，**每日常駐收集仍需第二次獨立核准**。
- 首先至少跑一次有錯時能退回的隔離 venv SDK smoke。不得直接升級整個 repo 的浮動 Shioaji 安裝。

## 資料合約

raw 位於 \`data/raw/shioaji/ticks/YYYY-MM-DD/BOARD|ODD/<symbol>.jsonl\`。
每筆：\`schema_version, quote_type, source, exchange, trading_date, lot_type, collector_session_id, collection_seq, event_time, received_at, payload\`。
raw 保留 simtrade / suspend / malformed events，供稽核。canonical 欄位 \`price_x10000\`、\`trade_volume_shares\`、\`cumulative_volume_shares\`。
**BOARD 單筆 volume 是張 × 1000，ODD 是股**，兩者不可混算。缺 Tick 不補人工零量 K。
來源沒有可信的唯一 tick id 時，**不以相同秒數作去重**；供 PR-2/3 的資料品質評估須另外標註疑似重複或缺口，不得聲稱具備完整交易所等級的序列檢查。

收盤 raw 採 .jsonl.gz 保存至少 **180 個交易日**；未完成研究裁決的來源不得被自動刪除。
本 PR 的手動壓縮功能不安裝排程；規劃的每日收盤批次壓縮、輪替保留與容量警報服務要在日常啟用前另行驗證。
預設 \`RawTickStore\` 磁碟停收門檻為 **75%**（在可能與交易 DB 共用分割區時更保守），未實際配置專用分割區前不得直接照 90% 跑。新服務的磁碟用量須先量測一週，並確保不影響 app.db 和 15:10/15:12 排程。

## 測試與操作原則

\`\`\`bash
python3 -m pytest tests/unit/test_intraday_tick_collector.py -q
python3 -m pytest tests/unit/ -q
python3 -m compileall -q src/market_data/intraday_tick.py src/market_data/intraday_collector.py src/cli/intraday.py
\`\`\`

對接 PR-2 前，應特別做：quote 授權拒絕、斷線重訂、緩慢標的無成交 vs 連線中斷、交易日交界、長時間磁碟壓力、同秒多筆真實成交、整股零股混合訂閱、不同 Shioaji 1.7.x 行情欄位、候選清單快照時點。
**離線 CI 已驗證：**[GitHub Actions #37739061093](https://github.com/ChenHom/tw-swing-trading-mvp/actions/runs/37739061093) 的 Python 3.10 compileall 成功、19 項離線 pytest 全數通過。**沒有在使用者真實主機執行全套測試、行情連線或日常啟用**。
