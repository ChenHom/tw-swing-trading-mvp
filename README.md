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

### A. 策略參數範本：`config/strategies/<strategy_id>.yaml`
系統採**多策略並行**架構，每個策略一個 YAML，包含 `parameters:`（進場參數）與 `exit:`（退出參數，由 risk_exit 引擎執行）兩個區塊。目前的策略：

| 策略 | 角色 | 說明 |
|---|---|---|
| `trend_breakout` | 進場（主） | 20 日新高 + 1.5 倍量 + 個股/大盤 60MA 濾網 |
| `pullback_rebound` | 進場（輔） | 多頭結構回踩月線 + K 線轉強 + 大盤濾網 |
| `trend_pullback` | 已退役 | 不再進場；存量倉位由 risk_exit 依其 `exit:` 管理至出清 |

範例（`config/strategies/trend_breakout.yaml`，bps 代表萬分之一）：
```yaml
strategy_id: trend_breakout
strategy_version: 1.0.0
parameters:
  breakout_lookback_days: 20    # 收盤創 20 日新高
  volume_multiple_pct: 150      # 當日量 > 20 日均量 1.5 倍
  ma_trend_period: 60           # 個股多頭濾網
  index_ma_period: 60           # 大盤多頭濾網（TSE 加權指數）
  order_budget_twd: 20000       # 單筆委託預算 (元)
exit:                           # 由 risk_exit 引擎讀取執行
  fixed_stop_loss_bps: 700      # 固定停損 -7%（加權均價計）
  trailing_stop_bps: 800        # 自持有後最高收盤回落 8%
  ma_break_period: 20           # 均線失效（連續確認、可設 buffer）
  ma_break_confirm_days: 2
  time_stop_days: 20            # 時間停損（交易日）
  time_stop_min_return_bps: 500
```

> [!IMPORTANT]
> `exit:` 區塊**納入 params_hash**：變更任何進場或退出參數後，必須重新 `approval create` + `approval activate`，否則該策略的 BUY 會因 hash 不符被阻擋（SELL/停損不受授權閘門影響，照常執行）。

### B. 策略授權清單 (Manifest) — 多策略並存
系統所有買入 (`BUY`) 指令必須嚴格匹配該策略的 `StrategyApprovalManifest`；每個策略各自獨立一份有效授權（active map：`artifacts/approvals/active-approvals.json`，以 `strategy_id` 為鍵）。
* 授權清單包含有效期、單筆委託上限、每日總買入上限、最大持倉量等**策略層限額**；全帳戶層限額（總持倉上限、每日總買入、每日新建倉數）設定於 `config/trading.yaml` 的 `global_limits:`。
* **賣出（SELL，含 risk_exit 停損）永不受授權閘門阻擋**——授權過期期間持倉保護照常運作。
* 管線順序設定於 `config/trading.yaml` 的 `pipeline:`（risk_exit 一律最先，再依序執行進場策略）。

### C. 既有資料庫升級（一次性 Migration）
從單策略版本升級者，需執行冪等的回填腳本（補 `strategy_id`、初始化移動停利 watermark），並於完成後自動對帳：
```bash
python3 -m scripts.migrate_multi_strategy --db data/app.db
```

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

### 第一步：初始化模擬帳戶與啟用各策略授權 (僅需執行一次)
```bash
# 1. 初始化模擬帳戶 (設定 1,000,000 元台幣初始現金)
python3 -m app account init --initial-cash 1000000

# 2. 為每個進場策略建立並啟用授權清單（每策略各一份）
python3 -m app approval create \
  --strategy config/strategies/trend_breakout.yaml \
  --expires-at 2026-12-31T23:59:59+08:00 \
  --output artifacts/approvals/trend_breakout-approval.json \
  --max-order-value 50000 \
  --max-daily-buy-value 200000 \
  --max-open-positions 5
python3 -m app approval activate artifacts/approvals/trend_breakout-approval.json

python3 -m app approval create \
  --strategy config/strategies/pullback_rebound.yaml \
  --expires-at 2026-12-31T23:59:59+08:00 \
  --output artifacts/approvals/pullback_rebound-approval.json \
  --max-order-value 50000 \
  --max-daily-buy-value 200000 \
  --max-open-positions 5
python3 -m app approval activate artifacts/approvals/pullback_rebound-approval.json

# 3. 檢視各策略授權狀態 / 停用特定策略
python3 -m app approval list
python3 -m app approval deactivate --strategy pullback_rebound   # 停用後該策略 BUY 被擋，SELL 不受影響

# 4. 首次啟用前回補行情（大盤濾網需要 ≥60 個交易日的 TSE 指數資料）
python3 -m app market backfill --calendar-days 120
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

### 第五點五步：出場試算與長期持有重分類

**(A) 單筆部位出場試算 (dry-run)**：對某個持倉，套用某策略的 `exit:` 規則（固定停損／移動停利／均線失效／時間停損）跑一次，報告各條件目前數值與是否觸發。**純唯讀**，不寫入、不產生 SELL 訊號，僅供判斷「若交由某策略管理會不會該賣」：
```bash
python3 -m app trade exit-check --symbol 2327 --strategy trend_breakout
```
**輸出範例**：
```text
=== 出場試算：2327（國巨） × 策略 trend_breakout ===
帳戶：simulation-main　試算日：2026-06-15
持倉：71 股　歸屬策略：MANUAL　建倉日：2026-06-10
收盤價：940.00 元　加權均價：516.92 元　當前報酬：+81.85%
------------------------------------------------
固定停損（-7.0%）：跌破 480.74 元 → ✓ 未觸發
移動停利（-8.0%）：最高 940.00（max(均價,收盤) 保守初始（無 watermark））→ 跌破 864.80 元 → ✓ 未觸發
均線失效（20MA，連 2 日、buffer 0.0%）：最新 SMA 739.95 元 → ✓ 未觸發
時間停損（持有≥20日且報酬<5.0%）：已持有 3 交易日 → ✓ 未觸發
------------------------------------------------
結論：未觸發任何出場條件，**不會賣出**。
```
> 註：MANUAL／長期部位沒有累積的移動停利最高價水位（watermark，只對 risk_exit 監控中的部位累積），試算時以 `max(加權均價, 當日收盤)` 保守初始化，報告會標註。實際要賣出仍走 `trade record-fill --side SELL`。

**(B) 既有部位重分類為長期持有**：把帳戶內某 symbol 的**手動持倉**（預設 `MANUAL` bucket）改列長期持有，使其免受策略自動出場；以 `--unset` 取消。更新成交事實 `fills` 後重建投影：
```bash
# 把 00400A 的手動持倉標為長期（不影響同 symbol 的策略交易部位）
python3 -m app trade set-long-term --symbol 00400A
# 取消長期標記
python3 -m app trade set-long-term --symbol 00400A --unset
```
> 同一 symbol 可能同時有手動長期持倉與策略交易部位（FIFO 依 strategy_id 隔離）。本指令**預設只動 MANUAL bucket**，避免誤把策略部位排除於 risk_exit 監控之外；要動其他策略 bucket 須顯式 `--strategy-id`。重分類不改變數量／FIFO 淨額，對帳仍通過。

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

# 1b. 依策略分組的損益歸因報表（trend_breakout / pullback_rebound / MANUAL ...）
python3 -m app report pnl --by-strategy

# 2. 手動對帳 (比對 cash_ledger 與持倉投影一致性，含 per-strategy bucket 檢查)
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

本專案使用 `pytest` 進行完整的單元與整合測試，涵蓋 Shioaji 行情、參數 Canonicalization、授權驗證、自動拆單、動態帳戶解析、長期持有與 FIFO 隔離、資金配置、排程安全、訊號拒絕閘門、per-account no-add／exit bundle 隔離，以及研究回測層（雙價/CA 帳本、FinMind/TWSE provider、風險/穩健指標、裁決狀態機、Research Ledger、lockbox、參數高原、PIT 流動性 universe）等共計 **321 個測試案例**。

執行所有測試：
```bash
python3 -m pytest
```

---

## 8. 研究回測（Research Backtest，Phase 0/1/2 + R）

live 每日流程（上述）負責「能不能穩定執行」；研究回測層負責「**這策略到底會不會賺錢**」。研究資料與 live 完全隔離：寫 `data/research.db`，不碰 `data/app.db`。

### 工作流

```bash
# 1. 回補真實深歷史（FinMind 主，含 2022 完整空頭）→ data/research.db
python3 app.py market backfill-history --from 2018-01-01 --to 2026-06-22 \
  --symbols "TSE,0050,2330,2317,..." --db data/research.db --source finmind

# 2. 在 research.db 上回測（--db 指向研究庫；研究庫當資料母版、建議 copy-per-run 避免重跑碰撞）
cp data/research.db data/_run.db
python3 app.py backtest run --db data/_run.db --from 2018-01-01 --to 2026-06-22 \
  --strategy trend_rider --initial-cash 300000
# 報告落 artifacts/reports/backtest/（gitignored，可重現）
```

### 量尺與裁決（看結果前寫死，不可事後人工裁切）

- **指標**：CAGR/Sharpe/Sortino/Calmar、對 0050 Beta/Alpha、三種回撤、PF/Expectancy、四條 benchmark（0050 buy-hold / 同曝險 / 同波動 / 等權 universe）、報酬分層、DSR/block bootstrap/Herfindahl、有效樣本數 gate、成本占比、分年表。
- **五級裁決**：`INVALID / REJECTED / RESEARCH_PASS / SHADOW_PASS / CAPITAL_APPROVED`。**今日固定 21 檔 universe 是 diagnostic（非 PIT）→ 結果只能標 `INVALID`+`diagnostic_result`，只能淘汰爛策略、不能晉級**。要拿 `RESEARCH_PASS` 必須改用 PIT universe（policy 驅動、含下市股、流動性條件只用 ≤D 資料）。
- **治理工具**：Strategy Thesis（`docs/strategies/<id>.md`）、Research Ledger（append-only、餵 DSR 試驗次數）、家族級 lockbox（只開一次）、參數高原掃描（不取單點最佳）。

### 現役策略（live）vs Challenger（研究中）

| strategy_id | 中文名 | 角色 | 出場特性 |
|---|---|---|---|
| `trend_breakout` | 趨勢帶量突破 | live | risk_exit 四層（緊停損 -7%、時間停損 20 日） |
| `pullback_rebound` | 回檔轉強 | live | risk_exit 四層（緊停損 -5%） |
| `trend_rider` | 順勢交易者 | **研究 Challenger（未上線交易）** | 「讓贏家跑」：寬移動停利 -25%、長均線跌破、**停用時間停損** |

> ⚠️ 回測在 diagnostic universe 上的報酬數字（含 trend_rider 亮眼的 +122%）**受後見之明/survivorship bias 污染**，不可當賺錢證據。可信的是「不受標的池影響」的結構面（崩盤防守、成本占比）。正式裁決待 PIT universe（見 plan `2455-cosmic-fountain.md` R-T4b）。
