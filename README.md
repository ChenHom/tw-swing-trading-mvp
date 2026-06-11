# 台股波段量化交易系統 MVP (tw-swing-trading-mvp)

本專案為台股波段量化交易系統的最小可行性產品 (MVP)，旨在建構一個**高確定性、可重現、可安全對帳**的量化交易閉環。系統支援唯讀行情同步（Shioaji API）、歷史回測、每日模擬交易、風控授權檢查，以及精確的 FIFO 持倉對帳。

---

## 1. 環境設定與安裝

### 依賴安裝
本專案基於 Python 3.10 運行。使用以下指令安裝所需依賴套件：
```bash
python3 -m pip install -r requirements.txt
```

### 環境變數設定
請在專案根目錄下建立 `.env` 檔案（已加入 `.gitignore` 避免洩露敏感憑證）。可以使用 `.env.example` 作為範本：
```bash
cp .env.example .env
```
編輯 `.env` 檔案並填入您的永豐金證券 Shioaji API 憑證：
```ini
SHIOAJI_API_KEY=您的API金鑰
SHIOAJI_SECRET_KEY=您的密鑰
DATABASE_URL=data/app.db
```

---

## 2. 核心設定檔與範本說明

專案的所有設定檔均放置於 `config/` 目錄中，常用範本說明如下：

### A. 策略參數範本：`config/strategies/trend_pullback.yaml`
用於設定交易策略的指標與限制。例如均線長度與停損停利點（bps 代表萬分之一）：
```yaml
strategy_id: trend_pullback      # 策略唯一識別碼
strategy_version: 1.0.0          # 策略版本
parameters:
  ma_short: 20                   # 短期均線期數
  ma_long: 60                    # 長期均線期數
  stop_loss_bps: 500             # 停損點 (500 bps = 5%)
  take_profit_bps: 1200          # 停利點 (1200 bps = 12%)
  order_budget_twd: 20000        # 單筆委託預算 (元)
```

### B. 策略授權清單 (Manifest)
系統所有買入 (`BUY`) 指令必須嚴格匹配受信任發行者簽署的 `StrategyApprovalManifest`，以落實防呆與風控。
* 授權清單包含有效期、單筆委託上限、每日總買入上限、最大持倉量等限制。
* 行動指南請參閱下方指令。

---

## 3. 命令列指令 (CLI) 使用說明

本專案所有的功能都可以透過 `app.py` 入口以 `python3 -m app` 執行。您可以隨時在指令後方加上 `--help` 查詢正體中文的說明。

### 查詢說明
* 查詢主選單：`python3 -m app --help`
* 查詢特定模組：`python3 -m app simulation --help`

---

### 帳戶參數選填說明 (Dynamic Account Resolution)
本系統所有子指令的 `--account` 參數均改為**選填**：
* 如果資料庫中僅存在一個投資帳戶，執行指令時無須填寫 `--account`，系統會自動選取。
* 只有在資料庫中存在兩個或更多帳戶時，系統才會提示您使用 `--account` 指定目標帳戶。

---

## 4. 標準每日模擬與訊號查詢工作流

以下為一個完整的「每日排程運行與訊號查詢/重置」的標準操作範本：

### 第一步：初始化模擬帳戶與啟用授權 (僅需執行一次)
```bash
# 1. 初始化模擬帳戶 (設定 1,000,000 元台幣初始現金)
python3 -m app account init --initial-cash 1000000

# 2. 建立策略授權清單 JSON 檔
python3 -m app approval create \
  --strategy config/strategies/trend_pullback.yaml \
  --expires-at 2026-12-31T23:59:59+08:00 \
  --output artifacts/approvals/active-approval.json \
  --max-order-value 50000 \
  --max-daily-buy-value 200000 \
  --max-open-positions 5

# 3. 啟用該授權清單 (使其成為系統當前活動授權)
python3 -m app approval activate artifacts/approvals/active-approval.json
```

### 第二步：執行每日模擬交易工作流 (通常排入每日下午收盤後的 cron)
```bash
# 執行 2026-06-10 當日的模擬交易
python3 -m app simulation run-daily --date 2026-06-10
```

### 第三步：查詢產生的交易訊號 (取代原始 SQLite 查詢)
當日流程跑完後，您可以使用以下指令來查詢當天（或歷史）產生的交易訊號：
```bash
# 1. 查詢 2026-06-10 產生的交易訊號
python3 -m app signal list --date 2026-06-10

# 2. 查詢資料庫中所有的交易訊號
python3 -m app signal list
```
**輸出範例**：
```text
訊號日期   | 執行日期   | 代號 | 名稱   | 動作 | 參考價格 | 原因                 | 狀態   | 訊號 ID
-----------+------------+------+--------+------+----------+----------------------+---------+--------------------------------------
2026-06-10 | 2026-06-11 | 2301 | 光寶科 | BUY  | 213.00   | TREND_PULLBACK_ENTRY | 待執行 | sig-a1b2c3d4...
2026-06-10 | 2026-06-11 | 2308 | 台達電 | BUY  | 2200.00  | TREND_PULLBACK_ENTRY | 待執行 | sig-e5f6g7h8...
2026-06-10 | 2026-06-11 | 2317 | 鴻海   | BUY  | 263.00   | TREND_PULLBACK_ENTRY | 已拒絕 (今日不適合) | sig-i9j0k1l2...
2026-06-10 | 2026-06-11 | 2330 | 台積電 | SELL | 2255.00  | TAKE_PROFIT_EXIT     | 待執行 | sig-m3n4o5p6...

共計: 4 筆訊號。

提示：使用以下指令拒絕執行某筆訊號：
  python3 -m app trade reject-signal --signal-id <訊號 ID> [--reason '原因']
```

### 第四步：產生委託計畫預覽 (依據帳戶可用餘額計算規劃買入的張數/股數)
您可以使用以下指令來預估交易訊號在目前帳戶餘額限制下，實際會執行多少交易數量（支援張與股拆單分類，並包含風控限額查驗）：
```bash
# 查詢 2026-06-10 訊號包的委託計畫預覽
python3 -m app trade plan --bundle 2026-06-10
```
**輸出範例**：
```text
帳戶：simulation-main | 可用現金：500,000 元 | 當日已買入金額：0 元
委託計畫預覽 (訊號包: bundle-20260610)：

代號 | 名稱   | 動作 | 參考價格 | 規劃數量 | 單位 | 預估金額 | 規劃狀態     
-----+--------+------+----------+----------+------+----------+--------------
2301 | 光寶科 | 買入 | 213.00   | 46       | 股   | 9,798    | 成功 (待執行)
2308 | 台達電 | 買入 | 2200.00  | 4        | 股   | 8,800    | 成功 (待執行)
2317 | 鴻海   | 買入 | 263.00   | -        | -   | -        | 已拒絕 (今日不適合)
2330 | 台積電 | 賣出 | 2255.00  | 66       | 股   | 148,830  | 成功 (待執行)
```

### 第四點五步：拒絕執行特定訊號 (人工閘門)
如果某筆訊號經審察後决定不執行，可將其標記為已拒絕。標記後該訊號將在 `trade plan` 與模擬執行中被跳過，不占用當日購買額度：
```bash
# 1. 拒絕執行訊號（signal_id 從 signal list 輸出的最後一欄複製）
python3 -m app trade reject-signal --signal-id sig-i9j0k1l2 --reason "今日不適合"

# 2. 反悔恢復（拒絕前請確認已提交的成交事實是否已記錄）
python3 -m app trade un-reject-signal --signal-id sig-i9j0k1l2
```

### 第五步：手動錄入成交紀錄 (手動建立已購買/已賣出的交易資料)
若您需要手動輸入一筆交易事實，系統會自動在資料庫中寫入成交與現金流水，並重新對帳。**若輸入了新的股票代號，系統會自動將其納入動態持倉估值池 (Valuation Universe)，在後續同步行情時自動更新現價，而不會修改版控下的設定檔 `config/universe.yaml`，以確保回測的確定性與重現性**。

此外，您可使用 `--long-term` 參數將此成交標記為**長期持有部位**，長期持有部位將會被排除在策略的自動出場邏輯之外，免受自動出場訊號影響：
```bash
# 手動錄入買入 71 股的 2327 (每股 516.92 元) 並標記為長期持有
python3 -m app trade record-fill --symbol 2327 --side BUY --quantity 71 --price 516.92 --long-term
```
**輸出範例**：
```text
成功錄入成交資料：
  - 帳戶：simulation-main
  - 標的：2327
  - 動作：BUY
  - 數量：71 股
  - 成交單價：516.92 元 (資料庫整數值: 5169200)
  - 成交總額：36,701 TWD (單價 x 數量)
  - 估計手續費：52 TWD
  - 估計總付出成本：36,753 TWD
```

### 第六步：若執行出錯，重置當日狀態原地重跑 (若無成交紀錄)
如果行情尚未就緒或參數有誤導致當日流程出錯，且該日期**尚未產生成交紀錄 (Fills)**，您可以一鍵安全地重置當日狀態，並重新執行。如果該日期已有成交事實，系統會安全阻擋 (RESET_BLOCKED_EXECUTION_FACTS_EXIST) 以防清空成交事實：
```bash
# 1. 重置 2026-06-10 的模擬狀態與產生的訊號 (在無成交記錄下)
python3 -m app simulation reset --date 2026-06-10

# 2. 原地重新跑當日模擬
python3 -m app simulation run-daily --date 2026-06-10
```

### 第七步：對帳與資產損益查詢
損益報告會自動轉為正體中文，並顯示對應的股票名稱：
```bash
# 1. 查詢收盤後的帳戶資產與損益對帳單
python3 -m app report pnl

# 2. 手動對帳 (比對 cash_ledger 與持倉投影一致性)
python3 -m app portfolio reconcile
```
**損益報告範例**：
```text
--- 帳戶 simulation-main 於 2026-06-10 的損益報告 ---
可用現金：5,987 TWD
部位價值：588,933 TWD
總資產淨值：594,920 TWD

持有部位：
  00400A 主動國泰動能高息: 9000 股 @ 均價 13.67 (現價: 13.97) - 價值: 125,730 TWD
  00981A 主動統一台股增長: 2000 股 @ 均價 28.97 (現價: 29.89) - 價值: 59,780 TWD
  00994A 主動第一金台股優: 5000 股 @ 均價 17.30 (現價: 17.24) - 價值: 86,199 TWD
  2327 國巨: 71 股 @ 均價 516.92 (現價: 819.00) - 價值: 58,149 TWD
  2330 台積電: 66 股 @ 均價 1975.23 (現價: 2255.00) - 價值: 148,830 TWD
  2360 致茂: 35 股 @ 均價 1994.86 (現價: 2210.00) - 價值: 77,350 TWD
  3090 日電貿: 15 股 @ 均價 227.00 (現價: 212.00) - 價值: 3,180 TWD
  3691 碩禾: 150 股 @ 均價 151.82 (現價: 163.00) - 價值: 24,450 TWD
  6805 富世達: 3 股 @ 均價 1890.00 (現價: 1755.00) - 價值: 5,265 TWD
```

---

## 5. 自動排程設定 (Cron Job Setup)

為了實現每日自動化運行，建議設定 `cron` 定期排程在收盤後執行。

### 做法 A：使用包裝腳本（推薦，內建時區處理）
使用專案內附的包裝腳本 [run_daily_sim.sh](file:///home/hom/services/stock/tw-day-trading/scripts/run_daily_sim.sh)。此腳本會自動將時區指定為台北時間、計算今日日期、確保日誌目錄存在，並自動檢查 `.venv` 虛擬環境。

1. **賦予腳本執行權限**：
   ```bash
   chmod +x scripts/run_daily_sim.sh
   ```
2. **編輯 crontab**：
   ```bash
   crontab -e
   ```
3. **加入排程設定**（請將 `/path/to/tw-swing-trading-mvp` 替換為您的實際專案絕對路徑）：
   ```cron
   CRON_TZ=Asia/Taipei
   10 15 * * 1-5 /path/to/tw-swing-trading-mvp/scripts/run_daily_sim.sh
   ```
   *(註：每個交易日週一至週五下午 15:10 執行。即使遇到非交易日或國定假日，`TradingCalendar` 也會自動偵測並安全跳過執行。)*

---

### 做法 B：直接設定 crontab 指令
如果您不想使用包裝腳本，也可以直接將指令寫入 crontab 中。

1. **確認虛擬環境的 Python 絕對路徑**：
   ```bash
   # 於正常 terminal 中執行確認路徑
   which python
   # 通常長得像：/path/to/tw-swing-trading-mvp/.venv/bin/python
   ```
2. **編輯 crontab**：
   ```bash
   crontab -e
   ```
3. **寫入以下內容**（注意：`simulation run-daily` **不接受** `--strategy` 參數，策略設定已寫在對應的 yaml 設定檔中）：
   ```cron
   CRON_TZ=Asia/Taipei
   10 15 * * 1-5 cd /path/to/tw-swing-trading-mvp && /path/to/tw-swing-trading-mvp/.venv/bin/python -m app simulation run-daily --account simulation-main >> logs/daily_sim.log 2>&1
   ```

> [!IMPORTANT]
> * **非互動式安全防護**：在 `cron` 等非互動式環境中執行時，系統**強制要求**必須顯式指定 `--account` 參數，否則基於防呆安全機制會拒絕執行。
> * **時區問題**：`cron` 預設使用系統時間。若您的伺服器不是台北時間，務必在 `crontab` 頂端設定 `CRON_TZ=Asia/Taipei`（如上所示）或在腳本中設定 `TZ=Asia/Taipei`。

---

### 驗證排程是否有正常跑

您可以透過以下方式檢查排程狀態與執行狀況：
```bash
# 1. 檢查 cron 排程最近的執行記錄 (Linux 系統)
grep CRON /var/log/syslog | tail -20

# 2. 檢視排程輸出日誌
tail -n 50 logs/daily_sim.log
```

---

## 6. 已知設計限制與未來改善 (Known Limits & Future Improvements)

當前 MVP 為了流程閉環與架構安全進行了重構，已解決資金超額分配、長期持倉 FIFO 污染、模擬重置事實刪除、交易宇宙配置污染、排程安全（帳戶解析強制、file lock、fill source 標記）等問題。後續開發仍需注意以下設計限制與改善方向：

1. **手動成交的事實完整性**:
   - 目前 `record-fill` 已標記 `source = MANUAL_IMPORT`，但仍僅能使用估計費率，且缺乏沖銷修正的模型支援（reversal / corrected fill）。
2. **撮合模型與公司行動限制**:
   - 零股與整張股票採用相同的成交滑價模型；未追蹤除權息等公司行動；缺乏詳細的排程異常告警閉環。

---

## 7. 測試與驗證

本專案使用 `pytest` 進行完整的單元與整合測試，包含 Shioaji 行情模組測試、參數 Canonicalization、授權驗證、自動拆單、動態帳戶解析、長期持有與 FIFO 隔離、資金配置與超額分配優化、排程安全（非互動帳戶解析、file lock、MANUAL_IMPORT 來源標記）、訊號拒絕閘門等共計 **67 個測試案例**。

執行所有測試：
```bash
python3 -m pytest
```
