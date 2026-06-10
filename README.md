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

## 4. 標準每日模擬與訊號查詢工作流

以下為一個完整的「每日排程運行與訊號查詢/重置」的標準操作範本：

### 第一步：初始化模擬帳戶與啟用授權 (僅需執行一次)
```bash
# 1. 初始化模擬帳戶 (設定 1,000,000 元台幣初始現金)
python3 -m app account init --account simulation-main --initial-cash 1000000

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
# 執行 2026-06-10 當日的模擬交易 (會自動同步行情 -> 產生訊號 -> 計畫委託 -> 模擬成交 -> 更新帳務與持倉)
python3 -m app simulation run-daily --date 2026-06-10 --account simulation-main
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
訊號日期   | 執行日期   | 代號 | 名稱   | 動作 | 參考價格 | 原因                 | 策略          
-----------+------------+------+--------+------+----------+----------------------+---------------
2026-06-10 | 2026-06-11 | 2301 | 光寶科 | BUY  | 213.00   | TREND_PULLBACK_ENTRY | trend_pullback
2026-06-10 | 2026-06-11 | 2308 | 台達電 | BUY  | 2200.00  | TREND_PULLBACK_ENTRY | trend_pullback
2026-06-10 | 2026-06-11 | 2317 | 鴻海   | BUY  | 263.00   | TREND_PULLBACK_ENTRY | trend_pullback
2026-06-10 | 2026-06-11 | 2330 | 台積電 | BUY  | 2255.00  | TREND_PULLBACK_ENTRY | trend_pullback

共計: 4 筆訊號。
```

### 第四步：產生委託計畫預覽 (依據帳戶可用餘額計算規劃買入的張數/股數)
您可以使用以下指令來預估交易訊號在目前帳戶餘額限制下，實際會執行多少交易數量（支援張與股拆單分類，並包含風控限額查驗）：
```bash
# 查詢 2026-06-10 訊號包的委託計畫預覽 (可直接輸入日期或訊號包 ID)
python3 -m app trade plan --bundle 2026-06-10 --account simulation-main
```
**輸出範例**：
```text
帳戶：simulation-main | 可用現金：10,000 元 | 當日已買入金額：0 元
委託計畫預覽 (訊號包: bundle-20260610)：

代號 | 名稱   | 動作 | 參考價格 | 規劃數量 | 單位 | 預估金額 | 規劃狀態     
-----+--------+------+----------+----------+------+----------+--------------
2301 | 光寶科 | 買入 | 213.00   | 46       | 股   | 9,798    | 成功 (待執行)
2308 | 台達電 | 買入 | 2200.00  | 4        | 股   | 8,800    | 成功 (待執行)
2317 | 鴻海   | 買入 | 263.00   | 38       | 股   | 9,994    | 成功 (待執行)
2330 | 台積電 | 買入 | 2255.00  | 4        | 股   | 9,020    | 成功 (待執行)
```

### 第五步：手動錄入成交紀錄 (手動建立已購買/已賣出的交易資料)
若您需要在模擬環境中手動輸入一筆成交事實（例如手動買入 10 股的 2330），可執行此指令。系統會自動將成交事實與現金流水寫入，並重新投影您的帳務：
```bash
# 手動錄入買入 10 股的 2330 (每股 2255.0 元) 到指定帳戶
python3 -m app trade record-fill --symbol 2330 --side BUY --quantity 10 --price 2255.0 --account simulation-main
```

### 第六步：若執行出錯，重置當日狀態原地重跑 (取代原始 SQLite 刪除操作)
如果行情尚未就緒或參數有誤導致當日流程出錯，您可以一鍵安全地重置當日狀態，並重新執行：
```bash
# 1. 重置 2026-06-10 的模擬狀態與產生的訊號 (此操作會安全清除關聯的 signal_items, signal_bundles 與 daily_runs)
python3 -m app simulation reset --date 2026-06-10

# 2. 原地重新跑當日模擬
python3 -m app simulation run-daily --date 2026-06-10 --account simulation-main
```

### 第七步：對帳與資產損益查詢
```bash
# 1. 查詢 2026-06-10 收盤後的帳戶資產與損益對帳單
python3 -m app report pnl --account simulation-main --date 2026-06-10

# 2. 手動對帳 (比對 cash_ledger 與持倉投影一致性)
python3 -m app portfolio reconcile --account simulation-main
```

---

## 5. 測試與驗證

本專案使用 `pytest` 進行完整的單元與整合測試，包含 Shioaji 行情模組測試、冪等重跑測試、新 CLI 指令（對帳、訊號清單、規劃、手動錄入）測試等共計 **52 個測試案例**。

執行所有測試：
```bash
python3 -m pytest
```
