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

### 第四步：若執行出錯，重置當日狀態原地重跑 (取代原始 SQLite 刪除操作)
如果行情尚未就緒或參數有誤導致當日流程出錯，您可以一鍵安全地重置當日狀態，並重新執行：
```bash
# 1. 重置 2026-06-10 的模擬狀態與產生的訊號 (此操作會安全清除關聯的 signal_items, signal_bundles 與 daily_runs)
python3 -m app simulation reset --date 2026-06-10

# 2. 原地重新跑當日模擬
python3 -m app simulation run-daily --date 2026-06-10 --account simulation-main
```

### 第五步：對帳與資產損益查詢
```bash
# 1. 查詢 2026-06-10 收盤後的帳戶資產與損益對帳單
python3 -m app report pnl --account simulation-main --date 2026-06-10

# 2. 手動對帳 (比對 cash_ledger 與持倉投影一致性)
python3 -m app portfolio reconcile --account simulation-main
```

---

## 5. 測試與驗證

本專案使用 `pytest` 進行完整的單元與整合測試，包含 Shioaji 行情模組測試、冪等重跑測試、CLI 測試等共計 **50 個測試案例**。

執行所有測試：
```bash
python3 -m pytest
```
