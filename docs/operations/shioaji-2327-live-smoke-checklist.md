# Shioaji 2327（國巨）唯讀行情 smoke — 操作計畫

更新：2026-10-08。狀態：**OFFLINE_PREFLIGHT_ONLY；LIVE_SMOKE_NOT_AUTHORIZED / NOT_EXECUTED**。

## 固定測試範圍

- 股票：**2327**，只測單一股票，不從持倉或候選資料額外選股。
- 四個 SDK quote topics：`BOARD Tick`、`ODD Tick`、`BOARD BidAsk`、`ODD BidAsk`；整股與零股絕不相加。
- 下一正常交易日：**2026-10-12（一）**。10/09 國慶日補假，10/10–11 週末。建議 09:05–09:30 測試；正式單次 smoke 30 秒。
- **D10 雙閘門**：目前只有規格核准，真正唯讀行情登入／訂閱必須再明確核准；之後日常自動收集還需第二次獨立核准。

## 今天離線檢查（完全不需券商 API）

在隔離、未被每日 cron 引用的 branch worktree / venv：

```bash
python3 -m pytest \
  tests/unit/test_intraday_tick_collector.py \
  tests/unit/test_intraday_reliability.py \
  tests/unit/test_intraday_book.py \
  tests/unit/test_intraday_2327_preflight.py -q

python3 -m compileall -q \
  src/market_data/intraday_tick.py \
  src/market_data/intraday_book.py \
  src/market_data/intraday_storage.py \
  src/market_data/intraday_connection.py \
  src/market_data/intraday_collector.py \
  src/cli/intraday.py src/cli/intraday_book.py src/cli/main.py

# 當前 CLI 在完整既有 venv 也可用此純預覽，無需登入券商
python3 -m app market intraday-plan --symbols 2327 --max-symbols 1
```

離線斷言：只產生一檔股票、兩種 lot；假的 SDK 在 **同一 session** 掛 4 個 quote topics；停機退訂 4 topics；不會呼叫 login／交易 API；未授權的 live entry 必須在 SDK import 之前被阻擋。

## Shioaji 模擬環境測試（2026-10-12，暫時固定）

`src/cli/intraday.py` 的 **`intraday-smoke` 唯一入口**暫時直接使用
`sj.Shioaji(simulation=True)`。目前指令**沒有 `--simulation` 參數**，
不會嘗試正式環境登入；要恢復真正行情測試，必須在未來另行審查修改。
這是券商**模擬連線**，不能視為 2327 真實市場資料驗收。

保留現有雙重授權閘門與 1–60 秒時間限制。複用原專案已存在的
`SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`，不啟動 CA、交易權限或下單。

在 detached worktree 更新 PR-2 最新程式，先確認沒有未保存修改：

```bash
cd ~/services/stock/tw-day-trading-smoke
git status --short
git fetch origin feat/shioaji-bidask-observations-pr2
git switch --detach origin/feat/shioaji-bidask-observations-pr2
python3 -m app market intraday-smoke --help
```

原本 `python3 -c ...` + `dotenv_values("../tw-day-trading/.env")` 的憑證載入
方式維持不變，執行 `market intraday-smoke` **不加 `--simulation`**：

```bash
INTRADAY_LIVE_SMOKE_APPROVED=yes python3 -c '
import os
from dotenv import dotenv_values

config = dotenv_values("../tw-day-trading/.env")
for key in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"):
    if not os.environ.get(key) and config.get(key):
        os.environ[key] = config[key]

from src.cli.main import main
main()
' market intraday-smoke \
  --enable-live-smoke \
  --symbols 2327 \
  --max-symbols 1 \
  --with-bidask \
  --duration-seconds 30 \
  --cache-dir "$HOME/shioaji-2327-smoke" \
  --stop-at-disk-pct 75
```

此模式輸出 `mode: ONE_OFF_SIMULATION_QUOTE_SMOKE`、
`simulation: true`、`real_market_verified: false`。
必須另外核對 Tick/BidAsk 是否真的有資料落盤；
未觀察到零股行情時不得宣稱模擬 SDK 訂閱驗證成功。

## 已知的 SDK 相容性問題（2026-10-12）

真實主機已確認在 `simulation=True` 模擬環境登入階段成功收到
`Session up`、合約資料訂閱事件 16，但先前入口使用
`sj.QuoteType.Tick`，使用者安裝的 SDK **沒有頂層 `shioaji.QuoteType`**，
造成 `AttributeError`，**因此尚未成功訂閱 2327**。

最新入口優先查找 `shioaji.QuoteType`、再查找
`shioaji.constant.QuoteType`；若 SDK 無這些 enum，則使用官方
接受的字串 `tick` / `bid_ask`。修正已用 fake SDK
離線涵蓋舊版 enum、頂層 enum、字串 fallback，及登入失敗時 logout
清理。真實主機需 fetch 最新 PR-2 再次測試，**不可宣稱已通過 live SDK smoke**。

執行時若仍碰 SDK 相容性錯誤，請只回報遮蔽個資的例外類型／訊息，
避免貼出登入回應中的身分證號、對外 IP 或 API 金鑰。

## 2026-10-12 SDK 訂閱 API 差異（已修程式，待真實主機回測）

在 `8e46779` 版，`simulation=True` 登入成功並出現 `Session up`，
但 Collector 於 `self.api.subscribe(...)` 中止：
`AttributeError: 'Shioaji' object has no attribute 'subscribe'`。

這表示主機安裝的 SDK 仍使用舊式的 `api.quote.subscribe`/
`api.quote.unsubscribe` 介面；該 SDK **不可用** 1.7 新文件的
`api.subscribe`。新版 Collector 的唯讀 adapter：
- 若 `api.subscribe`、`api.unsubscribe` 都存在，使用新版路徑。
- 否則若 `api.quote.subscribe`、`api.quote.unsubscribe` 都存在，使用舊版路徑，
  **所有 Tick/BidAsk、BOARD/ODD 訂閱與退訂必須明確帶
  `version=sj.constant.QuoteVersion.v1`**，包括重新連線後的三次有界重訂閱。
- 若只能找到不完整的訂閱／退訂配對或找不到 `QuoteVersion.v1`，直接拒絕啟動，
  **絕不默默退回 v0 或沒有退訂能力的模式**。
- 真實模擬行情仍待主機測試；在零股 Tick / BidAsk 沒有收到資料時，
  不得將 SDK 訂閱函式不拋例外等同於資料已成功驗證。
- 保持 `simulation=True`、`subscribe_trade=False`、不載 CA、不呼叫交易 API。

舊版介面及 v1 指定方式參照官方 Shioaji 行情說明：
https://sinotrade.github.io/zh/upgrade/ 。

## 測試通過標準／失敗判斷

1. 成功訂閱 2327 的四個 quote topics，且沒有 SDK 權限錯誤或意外其他股票訂閱。
2. 確認原始 Tick / BidAsk 寫入隔離資料夾，格式有效，BOARD/ODD 分檔、單位與 SDK 對照一致。
3. 健康度必須呈現 queue_dropped / raw_write_error / book_raw_write_error / rejected 數量，不能用已登入代替行情健康。
4. 若 ODD 30 秒沒有成交／五檔推送，必須標「尚未觀察到」；不得直接宣稱零股訂閱成功且有資料，也不能捏造缺少的 Tick。
5. 停機必須退訂所有 topic、釋放 Collector lease 並 logout；不影響正式 app.db、策略權限與既有 cron。
6. 結果需要完整 raw event、健康快照、SDK 版本、訂閱清單及錯誤摘要供獨立 code review。重連實測須另外設計，不應故意中斷正在運作的正式連線。

## P0／P1 最新進度與原始資料稽核（2026-10-12）

- 已新增離線 `market intraday-audit`，每次稽核只讀取固定日期及股票的 Tick/BidAsk × BOARD/ODD 四類 raw，**完全不登入券商**。
- 使用者本次實際 `wc -l` 基準：Tick BOARD 9、ODD 6；BidAsk BOARD 60、ODD 6，總計 81。這只能證明檔案筆數；實際內容由下面指令獨立核對。
- `collection_status=RAW_CAPTURED` 只表示已寫入 raw；`transport_verification_status=UNVERIFIED` 仍表示 SDK transport 回調未給出足夠證據；`data_quality_status` 與 `market_completeness_status` 不會憑有收到 Tick 就自行升級。
- P1 假 SDK 已覆蓋：登入失敗且清理、啟動失敗退訂、斷線重訂及不同 epoch、三次重試耗盡 fail-closed、異常 worker 退訂、執行緒停止逾時持有 lock、callback 欄位錯誤、零交易 API。真實 broker 中斷／長時間壓測 **尚未實測**。
- 詳細 Code Review：`docs/reviews/shioaji-pr1-pr2-p0-p1-review-20261012.md`。

更新到最新 PR-2 後，在**同一個 detached smoke worktree** 執行下列離線稽核。無須 `.env`、不會更改 81 筆原始 JSONL：

```bash
python3 -m app market intraday-audit \
  --cache-dir "$HOME/shioaji-2327-smoke" \
  --symbol 2327 --date 2026-10-12 \
  --expected-tick-board 9 --expected-tick-odd 6 \
  --expected-bidask-board 60 --expected-bidask-odd 6 \
  --output "$HOME/shioaji-2327-20261012-quality.json"
```

只有 `status=PASS_RAW_ONLY`、`total_raw_lines=81`、`total_accepted=81` 且四組零錯誤／拒絕時，才能說**已觀測到的 raw 內容**通過基本完整性／量價規則檢查。這不證明交易所完整資料、模擬行情與正式行情相等，也不代表可以直接合併或啟用日常服務。非 PASS 時 CLI exit code 1，請先檢查 JSON 報告的逐檔原因，不要修改 raw 來強制過關。

## 明確限制

離線測試不連接 Shioaji。現有 live smoke CLI 只允許交易日 08:30–13:40、單次 1–60 秒。真實 SDK 的 QuoteType/contract/回調格式與連線品質尚未由此預演證明。即使 30 秒無錯也只是 smoke，不等於長期蒐集已核准。
