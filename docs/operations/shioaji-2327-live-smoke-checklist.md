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

## 實際盤中測試（**尚未核准，也未執行**）

在確認工作樹、venv、SDK API 版本、行情權限、獨立儲存容量和原本正式排程後，另行核准才使用以下示意指令：

```bash
INTRADAY_LIVE_SMOKE_APPROVED=yes \
python3 -m app market intraday-smoke \
  --enable-live-smoke \
  --duration-seconds 30 \
  --symbols 2327 \
  --max-symbols 1 \
  --with-bidask \
  --cache-dir /path/to/isolated/shioaji-2327-smoke \
  --stop-at-disk-pct 75
```

**不要在現在執行這段實盤指令。** 不能把 PR-1/2 的 65+ 離線綠燈當作一次已授權實盤煙霧測試。此步驟必須在擁有 Shioaji 合法行情憑證的主機上完成；不載入 CA、不呼叫下單或刪單，不重啟 Web、不安裝 systemd、也不建立 cron。

## 測試通過標準／失敗判斷

1. 成功訂閱 2327 的四個 quote topics，且沒有 SDK 權限錯誤或意外其他股票訂閱。
2. 確認原始 Tick / BidAsk 寫入隔離資料夾，格式有效，BOARD/ODD 分檔、單位與 SDK 對照一致。
3. 健康度必須呈現 queue_dropped / raw_write_error / book_raw_write_error / rejected 數量，不能用已登入代替行情健康。
4. 若 ODD 30 秒沒有成交／五檔推送，必須標「尚未觀察到」；不得直接宣稱零股訂閱成功且有資料，也不能捏造缺少的 Tick。
5. 停機必須退訂所有 topic、釋放 Collector lease 並 logout；不影響正式 app.db、策略權限與既有 cron。
6. 結果需要完整 raw event、健康快照、SDK 版本、訂閱清單及錯誤摘要供獨立 code review。重連實測須另外設計，不應故意中斷正在運作的正式連線。

## 明確限制

離線測試不連接 Shioaji。現有 live smoke CLI 只允許交易日 08:30–13:40、單次 1–60 秒。真實 SDK 的 QuoteType/contract/回調格式與連線品質尚未由此預演證明。即使 30 秒無錯也只是 smoke，不等於長期蒐集已核准。
