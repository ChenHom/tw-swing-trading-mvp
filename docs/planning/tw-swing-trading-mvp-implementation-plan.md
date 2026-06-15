# 台股波段量化交易系統 MVP 實作規劃

> 文件版本：v2  
> 更新日期：2026-06-10  
> 文件目的：把系統收斂成可直接開工、可在少量資料與模擬環境驗證的第一版。  
> 核心原則：先完成可重現、可對帳、可安全重跑的交易閉環；MVP 不負責證明策略具有長期獲利能力。

---

## 1. MVP 目標

第一版只完成以下閉環：

```text
Shioaji 分鐘 K
→ 聚合並驗證 market_bars
→ 確定性策略產生 Daily Signal Bundle
→ Strategy Approval Manifest 授權檢查
→ Order Plan
→ Fake Broker 模擬成交
→ fills / cash_ledger 交易事實
→ 持倉與損益投影
→ 執行報告與對帳
```

MVP 成功代表：

```text
相同輸入能得到相同結果
策略無法讀到未來資料
未授權 BUY 會被阻擋
Manifest 失效時 SELL 仍可執行
重複執行不會重複成交或扣款
成交、現金、持倉、費用與損益能互相對帳
歷史回測與每日模擬使用同一套交易執行內核
每日流程失敗後可以從未完成階段安全重跑
```

MVP 不以以下結果作為完成條件：

```text
長期正報酬
跨多空週期有效
100 筆以上交易
五年以上回測
參數最佳化
正式券商下單
```

---

## 2. MVP 範圍

### 2.1 保留

```text
單一確定性波段策略
單一策略版本與參數版本
5～10 檔固定股票池
20～60 個交易日作流程與短回測驗證
一個 Daily Signal Bundle 格式
一個 Strategy Approval Manifest 格式
一套共用 TradeExecutionEngine
BacktestRunner 與 DailySimulationRunner
一個 Fake Broker
SQLite
整張與零股
基本手續費、證交稅與固定滑價
FIFO 持倉與損益
不可變 fills 與 cash_ledger
可重建持倉與損益投影
單一收盤後每日排程
```

### 2.2 暫不處理

```text
正式券商下單
多策略同時運行
全市場掃描
五年以上資料
百萬筆歷史資料
Walk-forward
參數最佳化
多帳戶正式交易
跨策略 Portfolio Risk
產業曝險
市場狀態模型
財報、新聞、法人資料
除權息、減資、股票分割
融資融券與放空
數位簽章與金鑰輪替
跨主機撤銷同步
Web Dashboard
微服務
Message Queue
完整 Event Sourcing 框架
讓 LLM 直接控制正式交易
```

暫不處理不代表永久刪除；這些項目不得阻礙第一版閉環完成。

---

## 3. 已確定的架構決策

| 問題 | MVP 定案 |
|---|---|
| 市場資料來源 | Shioaji 分鐘 K，聚合後只從 `market_bars` 讀取 |
| 回測與每日模擬 | 共用 `TradeExecutionEngine`，使用不同 Runner |
| 日期控制 | Runner 注入 `ExecutionContext.as_of_date`，核心不呼叫系統時鐘 |
| 交易日曆 | 獨立 `TradingCalendar` port，實作使用 `exchange_calendars` 的 `XTAI` |
| 日曆例外 | `calendar_overrides.yaml` 可強制開市或休市 |
| Manifest 更新 | 人工產生、驗證、啟用；不自動續期 |
| Manifest 預警 | 剩餘有效交易日小於等於 3 日時標記 `EXPIRING_SOON` |
| 每日執行 | 一個收盤後排程，依序執行 sync → execute → generate → report |
| 帳務真相 | `fills` 與 `cash_ledger` 為不可變事實 |
| 持倉與損益 | `position_lots`、FIFO 配對、`realized_pnl`、餘額快照為可重建投影 |
| 回測訊號 | `HistoricalSignalGenerator` 動態產生並保存歷史 Bundle |
| 防未來資料 | 策略只能使用綁定日期的 `PointInTimeMarketData` |
| `params_hash` | Pydantic 正規化後，以 `strategy-params-v1` canonical JSON 計算 SHA-256 |
| 百分比參數 | 使用整數 bps，不使用 float |
| 回測起始現金 | run-level 設定，預設由 `backtest.yaml` 提供，可被 CLI 覆寫 |
| Shioaji 憑證 | 環境變數或未提交 Git 的 `.env`，MVP 不導入 Vault |
| 每日排程工具 | 開發環境手動；Milestone 4 使用單一 cron + 程序鎖 |

---

## 4. 系統邊界與執行架構

```text
                        ┌────────────────────────┐
                        │ TradingCalendar        │
                        │ XTAI + overrides       │
                        └───────────┬────────────┘
                                    │
┌──────────────────────┐            │
│ BacktestRunner       │────────────┤
│ 歷史日期迴圈          │            │
└──────────┬───────────┘            │
           │                        ▼
           │              ┌──────────────────────┐
           ├─────────────>│ TradeExecutionEngine │
           │              │ 共用授權、風控、執行 │
           │              └──────────┬───────────┘
           │                         │
┌──────────┴───────────┐             ▼
│ DailySimulationRunner│    ┌──────────────────────┐
│ 收盤後每日閉環        │───>│ FakeBroker           │
└──────────────────────┘    └──────────┬───────────┘
                                       ▼
                             fills + cash_ledger
                                       │
                                       ▼
                       positions / FIFO / PnL projections
```

### 4.1 共用內核

以下只能有一套實作：

```text
Manifest 驗證
Bundle 驗證
BUY / SELL 動作判斷
單筆、每日與持倉數限額
Order Plan 產生
Fake Broker 成交模型
交易成本
持倉更新
FIFO 配對
已實現損益
現金異動
冪等檢查
Decision Codes
```

### 4.2 分離的執行情境

| 項目 | BacktestRunner | DailySimulationRunner |
|---|---|---|
| 日期來源 | `TradingCalendar.sessions_between()` | `SystemClock.today()` + `is_trading_day()` |
| Signal 來源 | 歷史日期即時計算 | 當日收盤後新產生 |
| 狀態空間 | 每個 run 獨立 account_id | 長期持久化 simulation account |
| 資料缺漏 | 回測失敗：`DATASET_INCOMPLETE` | 保留等待：`WAITING_MARKET_DATA` |
| 重跑方式 | 建立新 run_id | 同一 run_date 原地冪等重跑 |
| 報告 | 整段回測彙總 | 每日執行報告 |

核心不應充滿：

```python
if mode == "backtest":
    ...
else:
    ...
```

差異由 Runner、Clock、Signal Source 與資料庫命名空間處理。

---

## 5. 設定與秘密資料

### 5.1 策略設定

`strategy.yaml` 只放會直接影響策略決策的內容：

```yaml
strategy_id: trend_pullback
strategy_version: 1.0.0

parameters:
  ma_short: 20
  ma_long: 60
  stop_loss_bps: 500
  take_profit_bps: 1200
  order_budget_twd: 20000
```

禁止放入：

```text
回測起始現金
資料庫路徑
Shioaji API Key
排程時間
報告輸出位置
```

### 5.2 回測設定與起始現金

起始現金是回測 run 的輸入，不是策略參數。

```yaml
# config/backtest.yaml
initial_cash_twd: 300000
slippage_bps: 10
fee_model_version: tw-stock-v1
```

CLI 可覆寫：

```bash
python -m app backtest run \
  --from 2026-03-01 \
  --to 2026-05-31 \
  --initial-cash 300000
```

優先序：

```text
CLI --initial-cash
> config/backtest.yaml
> 無預設則拒絕啟動
```

每次回測建立：

```text
run_id = bt:<timestamp-or-uuid>
account_id = backtest:<run_id>
```

起始資金寫入 `cash_ledger`：

```text
INITIAL_DEPOSIT +300000 TWD
```

`initial_cash_twd` 必須記錄於 run metadata 與 run fingerprint。

每日模擬帳戶則在建立時只初始化一次：

```bash
python -m app account init \
  --account simulation-main \
  --initial-cash 300000
```

### 5.3 Shioaji 認證

MVP 使用環境變數：

```text
SHIOAJI_API_KEY
SHIOAJI_SECRET_KEY
```

本機可使用未提交 Git 的 `.env`：

```dotenv
SHIOAJI_API_KEY=replace-me
SHIOAJI_SECRET_KEY=replace-me
```

必要規則：

```text
.env 必須加入 .gitignore
repository 只提供 .env.example，不放真實值
啟動時缺少金鑰直接失敗，不使用空字串
log 不得輸出 API Key、Secret 或完整登入物件
檔案權限建議限制為僅目前使用者可讀
MVP 只開行情所需權限，不載入正式下單 CA
```

MVP 不導入 Vault、AWS Secrets Manager 或 Kubernetes Secret；正式下單前再升級秘密管理。

---

## 6. 市場資料管道

### 6.1 唯一外部行情來源

```text
正式 Provider：ShioajiMarketDataProvider
測試 Provider：FixtureMarketDataProvider
策略與交易唯一讀取來源：market_bars
```

Shioaji 用途：

```text
api.Contracts.Stocks：驗證商品代碼、交易所與商品資訊
api.kbars：取得指定股票與日期區間的分鐘 OHLCV
```

MVP 不混用 FinMind、TWSE、TPEX、Yahoo Finance 或其他來源，避免同一根 K 棒出現多個版本。

### 6.2 固定股票池

```yaml
# config/universe.yaml
symbols:
  - code: "2330"
    exchange: "TSE"
    instrument_type: "STOCK"
  - code: "2317"
    exchange: "TSE"
    instrument_type: "STOCK"
```

啟動時以商品檔驗證；無法取得契約的標的不進入策略。

### 6.3 資料流

```text
config/universe.yaml
→ ShioajiMarketDataProvider.fetch_kbars()
→ raw minute bars JSON.gz
→ DailyBarAggregator
→ MarketBarValidator
→ UPSERT market_bars
→ Strategy / Backtest / FakeBroker
```

各模組禁止自行呼叫 Shioaji。

```python
class MarketDataProvider(Protocol):
    def fetch_kbars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[MinuteBar]: ...
```

### 6.4 初始回補

MVP 初始回補：

```text
5～10 檔股票
最近 100 個日曆日
預期取得約 60 個交易日
```

```bash
python -m app market backfill --calendar-days 100
python -m app market validate --last-sessions 60
```

### 6.5 每日同步

```bash
python -m app market sync --date 2026-06-10
```

規則：

```text
只抓最後成功日期之後或指定日期的資料
原始回應先落盤，再進行聚合
驗證通過後才寫入可用的 market_bars
完整日 K 不被不完整資料覆寫
相同資料重跑必須冪等
策略執行期間不得臨時向 Shioaji 補資料
```

### 6.6 分鐘 K 聚合成日 K

MVP 只取臺灣一般交易時段：

```text
timezone = Asia/Taipei
regular_session = 09:00:00 ～ 13:30:00
```

聚合規則：

```text
open   = 第一根分鐘 K 的 open
high   = 所有分鐘 K 的 high 最大值
low    = 所有分鐘 K 的 low 最小值
close  = 最後一根分鐘 K 的 close
volume = volume 加總
amount = amount 加總
```

盤後與盤後零股資料不混入日 K。

### 6.7 `market_bars` 最小欄位

```text
symbol
exchange
instrument_type
trade_date
open
high
low
close
volume
amount
source
source_timezone
is_complete
source_fetched_at
raw_payload_checksum
created_at
updated_at
```

唯一鍵：

```sql
UNIQUE(symbol, exchange, trade_date, source)
```

MVP 只有一個正式 source；保留 source 欄位是為了稽核，不代表啟用多來源切換。

### 6.8 資料驗證

至少檢查：

```text
OHLC 與數量不得為負
high >= max(open, close, low)
low <= min(open, close, high)
同商品同日期不得重複
時間戳可轉成 Asia/Taipei
日 K 日期符合 TradingCalendar session
交易日缺資料必須被標記，不得假裝休市
```

---

## 7. TradingCalendar

交易日曆是獨立 port，Runner 與 Fake Broker 不得各自從 `market_bars` 猜交易日。

```python
class TradingCalendar(Protocol):
    def is_trading_day(self, value: date) -> bool: ...

    def sessions_between(
        self,
        start: date,
        end: date,
    ) -> Sequence[date]: ...

    def next_trading_day(self, value: date) -> date: ...

    def previous_trading_day(self, value: date) -> date: ...
```

MVP 實作：

```text
ExchangeCalendarsTradingCalendar(calendar_name="XTAI")
```

例外覆寫：

```yaml
# config/calendar_overrides.yaml
open_dates: []
closed_dates: []
```

判斷優先序：

```text
open_dates
→ closed_dates
→ exchange_calendars XTAI
```

### 7.1 責任切分

| 元件 | 回答的問題 |
|---|---|
| `TradingCalendar` | 這一天理論上是否應開市？ |
| `market_bars` | 這一天的行情是否已取得且完整？ |
| Shioaji 商品檔 | 商品是否存在及基本 metadata |
| MarketDataReadinessChecker | 今日資料是否已可供執行？ |

核心 invariant：

> 交易日由 `TradingCalendar` 決定；能否成交由該交易日的完整 `market_bar` 是否存在決定。

### 7.2 Fake Broker 的預定成交日

```python
expected_fill_date = calendar.next_trading_day(signal_date)
bar = market_bar_repository.find(symbol, expected_fill_date)
```

若預定交易日缺資料：

```text
WAITING_MARKET_DATA
```

不得跳到再下一筆有資料的日期成交，否則資料缺漏會被偷偷轉成延後成交。

---

## 8. 策略與 Point-in-Time 市場資料

### 8.1 MVP 策略

先實作一個完全確定性的策略，例如趨勢回檔：

```text
市場資料：日 K
訊號頻率：每日收盤後
持有期間：數日到數週
動作：BUY / SELL / HOLD
禁止：LLM 直接決定主交易訊號
```

### 8.2 防止 Lookahead Bias

策略不能取得 raw repository 或自行指定查詢截止日。

```python
class MarketDataRepository(Protocol):
    def as_of(self, value: date) -> "PointInTimeMarketData": ...


class PointInTimeMarketData(Protocol):
    @property
    def as_of_date(self) -> date: ...

    def history(self, symbol: str, limit: int) -> list[MarketBar]: ...

    def latest(self, symbol: str) -> MarketBar | None: ...
```

SQL 永遠套用固定上限：

```sql
SELECT *
FROM market_bars
WHERE symbol = :symbol
  AND trade_date <= :bound_as_of_date
  AND is_complete = 1
ORDER BY trade_date DESC
LIMIT :limit;
```

策略只能呼叫：

```python
market_data.history("2330", limit=60)
```

不能要求 D+1 的行情。

### 8.3 共用 SignalGenerator

回測與每日模擬使用同一個 SignalGenerator：

```python
class SignalGenerator(Protocol):
    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot,
    ) -> DailySignalBundle: ...
```

回測：

```python
market_view = repository.as_of(historical_date)
```

每日模擬：

```python
market_view = repository.as_of(run_date)
```

### 8.4 資料不足

策略要求 60 根 K 棒但只有 25 根時：

```text
INSUFFICIENT_HISTORY
```

不得補未來資料、使用全期間統計值或偷偷改成 25 日版本。

### 8.5 MVP 接受的偏差限制

第一版固定 5～10 檔股票池，整段回測不變。這仍可能有選股與存活者偏差，因此報告必須標記：

```text
research_scope = fixed_universe_observation
not_full_market_evidence = true
```

MVP 不把此結果解讀為全市場策略績效。

---

## 9. `params_hash` 與 canonicalization

`params_hash` 表示：

> 經策略專屬 schema 驗證、型別正規化並補齊預設值後，實際參與決策的完整參數。

### 9.1 計算流程

```text
YAML
→ Pydantic schema 驗證
→ 禁止未知欄位
→ 補齊預設值
→ 正規化 Unicode
→ canonical JSON
→ UTF-8
→ SHA-256
```

Canonicalization 版本：

```text
strategy-params-v1
```

### 9.2 參數型別

百分比與滑價使用整數 bps：

```yaml
stop_loss_bps: 500
slippage_bps: 10
```

不使用：

```yaml
stop_loss_pct: 0.05
stop_loss_pct: "0.05"
```

必要規則：

```text
整數保持整數
字串與數字不可混用
禁止 NaN 與 Infinity
陣列維持順序
巢狀物件 key 遞迴排序
字串使用 Unicode NFC
未知欄位直接拒絕
```

### 9.3 Pydantic 模型

```python
class TrendPullbackParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ma_short: int = Field(default=20, ge=2)
    ma_long: int = Field(default=60, ge=5)
    stop_loss_bps: int = Field(default=500, ge=1, le=5000)
    take_profit_bps: int = Field(default=1200, ge=1, le=10000)
    order_budget_twd: int = Field(default=20000, ge=1000)
```

省略預設值與明確寫出預設值，必須產生相同 hash。

### 9.4 Canonical JSON

```python
json.dumps(
    normalized_params,
    sort_keys=True,
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
)
```

### 9.5 單一計算元件

```text
StrategyParameterCanonicalizer
```

使用者：

```text
strategy inspect
approval create
signal generate
approval validate
```

禁止各模組自行實作 hash 邏輯。

### 9.6 CLI

```bash
python -m app strategy inspect config/strategies/trend_pullback.yaml
```

輸出：

```text
strategy_id: trend_pullback
strategy_version: 1.0.0
canonicalization: strategy-params-v1
params_hash: sha256:...
```

### 9.7 Manifest digest

Manifest 使用獨立版本：

```text
manifest-v1
```

計算時只移除：

```text
integrity.digest
```

保留：

```text
integrity.algorithm
integrity.canonicalization
```

因此有效期限、限額、策略身分與 canonicalization 規則都受 digest 保護。

---

## 10. Strategy Approval Manifest

### 10.1 最小結構

```json
{
  "schema_version": "1.0",
  "approval_id": "approval-trend-pullback-v1",
  "issuer_id": "manual-research-review",
  "strategy": {
    "strategy_id": "trend_pullback",
    "strategy_version": "1.0.0",
    "params_canonicalization": "strategy-params-v1",
    "params_hash": "sha256:..."
  },
  "permissions": {
    "execution_modes": ["simulation"],
    "risk_increasing_actions": ["open_long", "increase_long"]
  },
  "limits": {
    "currency": "TWD",
    "max_order_value": 20000,
    "max_daily_buy_value": 40000,
    "max_open_positions": 3
  },
  "validity": {
    "valid_from": "2026-06-10T00:00:00+08:00",
    "expires_at": "2026-07-10T00:00:00+08:00"
  },
  "integrity": {
    "algorithm": "sha256",
    "canonicalization": "manifest-v1",
    "digest": "sha256:..."
  }
}
```

有效區間：

```text
valid_from <= now < expires_at
```

### 10.2 驗證順序

```text
1. JSON Schema
2. Manifest digest
3. issuer allowlist
4. denylist / revoked approvals
5. valid_from / expires_at
6. execution mode
7. strategy_id
8. strategy_version
9. params_canonicalization
10. params_hash
11. approval_id binding
12. action permission
13. limits
```

### 10.3 人工生命週期

Manifest：

```text
人工建立
→ CLI 驗證
→ 人工啟用
→ 到期前人工 review
→ 產生新 Manifest
→ 新 Bundle 引用新 approval_id
```

禁止：

```text
自動延長 expires_at
複製舊 Manifest 只改日期
過期後忽略授權
自動選目錄中最新檔案
讓新 Manifest 接管舊 BUY Bundle
```

### 10.4 Active Manifest

```text
artifacts/approvals/
├── approval-v1.json
├── approval-v2.json
└── active-approval.json
```

`active-approval.json`：

```json
{
  "approval_id": "approval-trend-pullback-v2",
  "activated_at": "2026-07-05T20:00:00+08:00"
}
```

切換使用暫存檔 + atomic rename。Manifest 本體不可變。

### 10.5 CLI

```bash
python -m app approval create \
  --strategy config/strategies/trend_pullback.yaml \
  --expires-at 2026-07-10T00:00:00+08:00 \
  --output artifacts/approvals/approval-v2.json

python -m app approval validate artifacts/approvals/approval-v2.json
python -m app approval activate artifacts/approvals/approval-v2.json
python -m app approval status
```

### 10.6 到期預警與降級執行

Runner preflight 狀態：

```text
ACTIVE
EXPIRING_SOON
NOT_YET_VALID
EXPIRED
REVOKED
MISSING
INVALID
```

```yaml
approval:
  expiry_warning_sessions: 3
```

行為：

| 狀態 | BUY | SELL | Runner |
|---|---:|---:|---|
| ACTIVE | 繼續逐筆驗證 | 可執行 | 正常 |
| EXPIRING_SOON | 繼續逐筆驗證 | 可執行 | 警告 |
| NOT_YET_VALID | 阻擋 | 可執行 | 降級 |
| EXPIRED | 阻擋 | 可執行 | 降級 |
| REVOKED | 阻擋 | 可執行 | 降級 |
| MISSING | 阻擋 | 可執行 | 降級 |
| INVALID | 阻擋 | 可執行 | 降級 |

Preflight 只負責預警與報告；每筆 BUY 仍須由 `TradeExecutionEngine` 正式驗證。

---

## 11. Daily Signal Bundle

### 11.1 最小結構

```json
{
  "schema_version": "1.0",
  "bundle_id": "bundle-20260610",
  "run_id": "daily:2026-06-10",
  "approval_id": "approval-trend-pullback-v1",
  "strategy": {
    "strategy_id": "trend_pullback",
    "strategy_version": "1.0.0",
    "params_canonicalization": "strategy-params-v1",
    "params_hash": "sha256:..."
  },
  "signal_date": "2026-06-10",
  "target_execution_date": "2026-06-11",
  "market_data_cutoff": "2026-06-10",
  "signals": [
    {
      "signal_id": "bundle-20260610:2330:buy",
      "symbol": "2330",
      "action": "BUY",
      "reference_price": 1020,
      "reason_code": "TREND_PULLBACK_ENTRY"
    }
  ]
}
```

`target_execution_date` 在產生 Bundle 時由 `TradingCalendar.next_trading_day(signal_date)` 固定，執行日不得重新推算。

### 11.2 動作轉換

| Signal | 現有持倉 | 交易動作 |
|---|---:|---|
| BUY | 0 | `open_long` |
| BUY | > 0 | `increase_long` |
| SELL | > 0 | `close_long` |
| SELL | 0 | 忽略並記錄 `SELL_WITHOUT_POSITION` |
| HOLD | 任意 | 不建立訂單 |

MVP 不支援：

```text
reduce_long
target_weight
partial_exit
rebalance
short_sell
```

### 11.3 Hash 來源

Bundle 的 `params_hash` 必須從策略實際載入的參數重新計算，不得從 Manifest 複製。

```text
策略實際參數 → Bundle.params_hash
審核時參數   → Manifest.params_hash
執行時比較兩者
```

### 11.4 歷史 Bundle

Milestone 2 使用 `HistoricalSignalGenerator` 逐日產生並保存 Bundle。即使訊號為空，也要保存空 Bundle，供重現與除錯。

`RecordedSignalBundleSource` 延後到 Milestone 4 累積歷史 Bundle 後再實作，用於重播曾經產生的決策，不作為 MVP 首次回測的來源。

---

## 12. ExecutionContext 與共用執行內核

```python
@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    run_type: Literal["BACKTEST", "DAILY_SIMULATION"]
    as_of_date: date
    execution_date: date
    account_id: str
```

規則：

```text
TradeExecutionEngine 不呼叫 datetime.now()
Runner 負責取得日期與建立 ExecutionContext
核心只依賴 context 與明確輸入
```

主要介面：

```python
class TradeExecutionEngine:
    def execute_bundle(
        self,
        context: ExecutionContext,
        bundle: DailySignalBundle,
    ) -> ExecutionResult: ...
```

共用內容：

```text
契約驗證
Manifest 驗證
Bundle / approval 綁定
Action 轉換
風控與限額
Order Plan
Fake Broker
事實寫入與投影更新
執行結果
```

---

## 13. 每日模擬排程與執行時序

### 13.1 單一收盤後排程

採用一個每日排程：

```bash
python -m app simulation run-daily --date 2026-06-10
```

交易日 D 收盤後執行：

```text
1. TradingCalendar 判斷 D 是否為交易日
2. Manifest preflight
3. 同步 D 日分鐘資料
4. 聚合並驗證 D 日 market_bar
5. 執行 target_execution_date = D 的 PENDING 訊號
6. 使用 D 日 open 模擬成交
7. 寫入成交、現金與持倉投影
8. 以 D 日 close 與更新後持倉產生新 Bundle
9. 新 Bundle 指向 next_trading_day(D)
10. 產生 D 日報告
```

不要使用「執行昨日 Bundle」作為查詢語意。正確條件：

```sql
WHERE target_execution_date = :run_date
  AND execution_status = 'PENDING';
```

### 13.2 Runner 內部 stages

```text
MARKET_SYNC
EXECUTE_PENDING
GENERATE_SIGNAL
GENERATE_REPORT
```

各 stage：

```text
可單獨呼叫
可獨立重跑
有自己的狀態與錯誤碼
不得因後續 stage 失敗而回滾已完成的交易事實
```

可用子指令：

```bash
python -m app market sync --date 2026-06-10
python -m app simulation execute-pending --execution-date 2026-06-10
python -m app signal generate --as-of-date 2026-06-10
python -m app simulation report --date 2026-06-10
```

平常只排程 `run-daily`；子指令用於修復與除錯。

### 13.3 排程工具定案

開發與 Milestone 1～3：

```text
手動 CLI 執行
```

Milestone 4：

```text
單一 OS cron
執行時間初始設定為 Asia/Taipei 14:10
使用程序鎖避免重疊
輸出結構化 log
失敗時由相同日期再次執行補跑
```

範例：

```cron
CRON_TZ=Asia/Taipei
10 14 * * 1-5 flock -n /tmp/tw-swing-daily.lock \
  /path/to/python -m app simulation run-daily >> /path/to/logs/daily.log 2>&1
```

即使 cron 只排週一至週五，Runner 仍必須呼叫 `TradingCalendar.is_trading_day()`；cron 不能取代交易日曆。

不在 MVP 引入 Celery、Airflow、Kubernetes CronJob 或應用內排程框架。

### 13.4 資料尚未就緒

14:10 只是開始嘗試，不代表資料一定完整。

```text
非交易日
→ NON_TRADING_DAY，正常跳過

交易日但完整 market_bar 尚未建立
→ WAITING_MARKET_DATA
→ execution / signal stages 保持 PENDING
→ 同日期補跑
```

### 13.5 Daily run 狀態

```text
daily_runs
- run_id
- run_date
- account_id
- strategy_id
- status
- market_sync_status
- execution_status
- signal_generation_status
- report_status
- started_at
- completed_at
- last_error_code
```

唯一鍵：

```sql
UNIQUE(run_date, account_id, strategy_id)
```

狀態範例：

```text
COMPLETED
PARTIALLY_COMPLETED
WAITING
FAILED
SKIPPED
```

---

## 14. BacktestRunner

### 14.1 訊號來源

MVP 使用：

```text
HistoricalSignalGenerator
```

不依賴既有歷史 Bundle。

### 14.2 執行順序

對每個交易日 D：

```text
1. 驗證 D 日市場資料存在
2. 執行 target_execution_date = D 的待成交訊號
3. 使用 D 日 open 成交
4. 更新現金與持倉
5. 建立 PointInTimeMarketData(as_of_date=D)
6. 使用截至 D 日 close 的資料產生訊號
7. 保存 signal_date = D 的 Bundle
8. Bundle 指向 next_trading_day(D)
```

這與每日模擬的順序一致。

### 14.3 日期來源

```python
trading_dates = calendar.sessions_between(start_date, end_date)
```

不得從任一檔股票的 `market_bars` 反推整體市場交易日。

### 14.4 回測隔離

每次執行建立新的：

```text
run_id
backtest account_id
INITIAL_DEPOSIT
fills
cash_ledger
projections
```

舊 run 不修改、不覆寫。修正程式或資料後建立新 run 比較。

### 14.5 回測重現性輸入

以下全部必須記錄：

```text
strategy_id
strategy_version
params_hash
canonicalization version
universe_hash
market_data_checksum 或 dataset version
calendar implementation/version
calendar overrides checksum
initial_cash_twd
fee_model_version
slippage_bps
Manifest approval_id / digest
程式 commit SHA（可取得時）
```

同一組輸入應產生相同：

```text
Bundles
Order Plans
fills
cash_ledger
PnL
```

### 14.6 回測資料缺漏

歷史資料理論上應完整：

```text
WAITING_MARKET_DATA
→ 轉成 DATASET_INCOMPLETE
→ 中止或標記該次 run 失敗
```

回測不得無限等待，也不得跳過缺漏交易日後繼續計算績效。

---

## 15. Trading Bot 與 Order Plan

Trading Bot 責任：

```text
載入 Bundle 與 active Manifest
驗證契約、身分、期限與綁定
取得現金與持倉投影
將 Signal 轉換為交易動作
檢查單筆、每日買入與持倉數限制
計算整張或零股數量
建立具冪等性的 order_intent
呼叫 Fake Broker
保存執行結果
```

### 15.1 股數計算

```text
order_budget = min(
    strategy_order_budget,
    manifest.max_order_value,
    remaining_daily_buy_limit,
    available_cash
)
```

```text
quantity = floor(order_budget / estimated_price)
```

拆單：

```text
quantity >= 1000
→ board_lot_quantity = floor(quantity / 1000) * 1000
→ odd_lot_quantity = quantity % 1000

quantity < 1000
→ odd_lot_quantity = quantity
```

MVP 可先在 Fake Broker 內視為同一成交模型；正式券商整合前再拆成不同 session 與訂單型態。

### 15.2 冪等鍵

交易意圖：

```text
execution_key =
account_id
+ bundle_id
+ signal_id
+ target_execution_date
```

資料庫唯一限制：

```sql
UNIQUE(execution_key)
```

Bundle：

```text
bundle_key =
strategy_id
+ strategy_version
+ params_hash
+ signal_date
+ approval_id
+ run_id
```

同一 run 重跑回傳既有結果；新的 backtest run_id 可完整重算。

---

## 16. Fake Broker

MVP 只有一個 `FakeBroker`，回測與每日模擬共用。

輸入：

```text
Order Plan
execution_date
完整 market_bar
slippage_bps
fee model
```

輸出：

```text
FILLED
PARTIALLY_FILLED
REJECTED
CANCELLED
WAITING_MARKET_DATA
```

### 16.1 成交價格

```text
BUY fill_price  = open × (1 + slippage_bps / 10000)
SELL fill_price = open × (1 - slippage_bps / 10000)
```

成交前先使用整數或 Decimal 運算並依臺股價格精度規則統一處理；不得以 binary float 寫入金額欄位。

### 16.2 預定成交日

```text
Bundle.target_execution_date
```

Fake Broker 不自行找「第一個有 market_bar 的日期」。缺少預定日期資料時回傳 `WAITING_MARKET_DATA`。

### 16.3 MVP 模擬範圍

保留：

```text
全部成交
固定條件部分成交 fixture
拒單 fixture
取消 fixture
資料等待
```

不模擬完整委託簿、排隊順位與盤中逐筆成交。

---

## 17. 持倉、現金與損益的真相來源

### 17.1 權威事實

| 領域 | 權威來源 |
|---|---|
| 成交 | `fills` |
| 現金異動 | `cash_ledger` |
| 未平倉持股 | 從 `fills` 重建 |
| FIFO 批次 | 從 `fills` 配對重建 |
| 已實現損益 | 從 FIFO 配對與交易成本重建 |
| 現金餘額 | `SUM(cash_ledger.amount)` |
| 快照 | 衍生資料，不具最高權威 |

矛盾時：

```text
fills / cash_ledger
> position_lots / realized_pnl / cash_balances
```

### 17.2 `fills`

```text
fill_id
account_id
run_id
order_id
execution_key
symbol
side
quantity
price
filled_at
reverses_fill_id nullable
created_at
```

成交建立後不直接 UPDATE 價格與數量。

### 17.3 `cash_ledger`

```text
ledger_id
account_id
run_id
event_type
amount
currency
source_type
source_id
occurred_at
idempotency_key
created_at
```

事件範例：

```text
INITIAL_DEPOSIT
BUY_NOTIONAL
SELL_PROCEEDS
BROKER_FEE
TRANSACTION_TAX
CASH_ADJUSTMENT
REVERSAL
```

### 17.4 衍生投影

```text
position_lots
fifo_matches
realized_pnl
cash_balances
portfolio_snapshots
```

日常查詢讀投影；對帳與修復才重播事實。

### 17.5 同一筆成交的 transaction

一筆成交必須在同一 SQLite transaction 內：

```text
1. insert fill
2. append cash_ledger entries
3. apply position_lots projection
4. apply FIFO matches
5. apply realized_pnl projection
6. refresh cash balance snapshot
7. commit
```

不可先提交 fill，再分開提交扣款與持倉。

### 17.6 Ledger 冪等

```sql
UNIQUE(idempotency_key)
```

並限制同一成交不得重複產生相同事件：

```sql
UNIQUE(account_id, source_type, source_id, event_type)
```

### 17.7 對帳與重建

只驗證：

```bash
python -m app portfolio reconcile --account simulation-main
```

重建投影：

```bash
python -m app portfolio rebuild-projections --account simulation-main
```

`rebuild-projections` 可清除並重建：

```text
position_lots
fifo_matches
realized_pnl
cash_balances
portfolio_snapshots
```

不得清除或重建：

```text
fills
cash_ledger
```

### 17.8 修正錯誤

回測：

```text
放棄舊 run，建立新 run
```

每日模擬：

```text
補償 fill / reversal ledger
→ 新修正 fill
```

MVP 可先不提供完整修正 CLI，但資料模型不得依賴直接 UPDATE 交易事實。

---

## 18. SQLite 最小資料表

```text
market_bars
strategy_runs
backtest_runs
daily_runs
signal_bundles
signal_items
order_intents
broker_orders
fills
cash_ledger
position_lots
fifo_matches
realized_pnl
cash_balances
execution_results
```

### 18.1 重要唯一鍵

```text
market_bars(symbol, exchange, trade_date, source)
signal_bundles(bundle_id)
order_intents(execution_key)
broker_orders(client_order_id)
fills(fill_id)
cash_ledger(idempotency_key)
daily_runs(run_date, account_id, strategy_id)
```

唯一鍵是冪等性的最後防線，不得只靠應用程式 `if exists`。

### 18.2 金額型別

SQLite 不以 `REAL` 儲存現金、成交價與損益。

MVP 可採其中一種固定規則：

```text
整數最小貨幣單位
或 Decimal 字串
```

全專案只能選一種。建議：

```text
TWD 金額以整數元儲存
成交價以固定縮放整數儲存，例如 price_x10000
```

避免 binary float 累積誤差。

---

## 19. 建議目錄

```text
project/
├── config/
│   ├── strategies/
│   │   └── trend_pullback.yaml
│   ├── backtest.yaml
│   ├── trading.yaml
│   ├── universe.yaml
│   ├── calendar_overrides.yaml
│   ├── issuer-allowlist.json
│   └── revoked-approvals.json
├── artifacts/
│   ├── approvals/
│   ├── signals/
│   ├── order-plans/
│   └── reports/
├── data/
│   ├── raw-market/
│   ├── datasets/
│   └── app.db
├── src/
│   ├── application/
│   │   ├── execution/
│   │   └── runners/
│   ├── contracts/
│   ├── strategy/
│   ├── approval/
│   ├── market_data/
│   ├── calendar/
│   ├── trading/
│   ├── broker/
│   ├── portfolio/
│   ├── reporting/
│   └── cli.py
├── fixtures/
│   ├── market/
│   ├── approvals/
│   ├── signals/
│   └── broker/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── .env.example
└── .gitignore
```

重要 ports：

```text
MarketDataProvider
MarketDataRepository
PointInTimeMarketData
TradingCalendar
ManifestRepository
SignalBundleRepository
PortfolioRepository
ExecutionRepository
Broker
Clock
```

---

## 20. CLI MVP

```bash
# 市場資料
python -m app market backfill --calendar-days 100
python -m app market sync --date 2026-06-10
python -m app market validate --last-sessions 60

# 策略與 hash
python -m app strategy inspect config/strategies/trend_pullback.yaml

# Manifest
python -m app approval create --strategy config/strategies/trend_pullback.yaml ...
python -m app approval validate artifacts/approvals/approval-v1.json
python -m app approval activate artifacts/approvals/approval-v1.json
python -m app approval status

# 帳戶
python -m app account init --account simulation-main --initial-cash 300000

# 回測
python -m app backtest run \
  --from 2026-03-01 \
  --to 2026-05-31 \
  --initial-cash 300000

# 每日模擬
python -m app simulation run-daily --date 2026-06-10
python -m app simulation execute-pending --execution-date 2026-06-10

# 訊號與預覽
python -m app signal generate --as-of-date 2026-06-10
python -m app trade plan --bundle artifacts/signals/bundle-20260610.json

# 對帳與報告
python -m app portfolio reconcile --account simulation-main
python -m app portfolio rebuild-projections --account simulation-main
python -m app report pnl --account simulation-main --date 2026-06-10

# 人工緊急退出
python -m app trade close-all --broker fake --reason manual-emergency
```

不先建立 Web 操作介面。

---

## 21. Decision Codes

### 21.1 資料與日曆

```text
NON_TRADING_DAY
WAITING_MARKET_DATA
DATASET_INCOMPLETE
MARKET_BAR_INVALID
INSUFFICIENT_HISTORY
```

### 21.2 Manifest

```text
APPROVED
SCHEMA_INVALID
INTEGRITY_INVALID
ISSUER_NOT_TRUSTED
MANIFEST_NOT_YET_VALID
MANIFEST_EXPIRING_SOON
MANIFEST_EXPIRED
MANIFEST_REVOKED
MANIFEST_MISSING
STRATEGY_MISMATCH
STRATEGY_VERSION_MISMATCH
PARAMS_CANONICALIZATION_MISMATCH
PARAMS_HASH_MISMATCH
APPROVAL_ID_MISMATCH
EXECUTION_MODE_NOT_ALLOWED
RISK_ACTION_NOT_ALLOWED
```

### 21.3 訂單與風控

```text
ORDER_LIMIT_EXCEEDED
DAILY_BUY_LIMIT_EXCEEDED
MAX_OPEN_POSITIONS_EXCEEDED
INSUFFICIENT_CASH
SELL_WITHOUT_POSITION
DUPLICATE_INTENT
BROKER_REJECTED
PARTIALLY_FILLED
FILLED
CANCELLED
```

### 21.4 帳務與對帳

```text
RECONCILE_OK
CASH_BALANCE_MISMATCH
POSITION_QUANTITY_MISMATCH
MISSING_LEDGER_ENTRY
DUPLICATE_LEDGER_ENTRY
FIFO_ALLOCATION_MISMATCH
REALIZED_PNL_MISMATCH
```

Decision Code 是程式、測試、CLI 與報告的共同語言，不依賴自由文字判斷。

---

## 22. 測試策略

### 22.1 單元測試

```text
strategy-params-v1 canonicalization
省略預設值與明確預設值 hash 相同
字串數字、未知欄位、NaN 被拒絕
manifest-v1 digest
Manifest 有效區間與剩餘交易日
TradingCalendar overrides
PointInTimeMarketData 無法讀到未來資料
BUY / SELL 動作轉換
單筆、每日與持倉數限額
整張與零股數量
固定滑價
手續費與證交稅
FIFO 配對
已實現與未實現損益
```

### 22.2 整合測試

```text
Shioaji fixture → minute bars → daily market_bars
Manifest + Bundle → Order Plan
Order Plan → Fake Broker → Fill
Fill → cash_ledger + position projection + PnL
相同 execution_key 重跑不重複成交
相同 fill 不重複產生 ledger
缺預定成交日 market_bar → WAITING_MARKET_DATA
Manifest 過期 → BUY blocked、SELL allowed
```

### 22.3 端到端測試

固定輸入：

```text
一份有效 Manifest
一份含 BUY / SELL 的 Bundle
一個有起始現金與既有持倉的模擬帳戶
固定 market_bars fixture
```

固定輸出：

```text
Approval Decision
Order Plan
Fake Broker Result
fills
cash_ledger
Position Snapshot
Cash Snapshot
PnL Report
Daily Run Report
```

### 22.4 重現性測試

同一組固定輸入執行兩次，檢查：

```text
Bundle 內容相同
Order Plan 相同
成交結果相同
最終投影相同
第二次不新增事實紀錄
```

---

## 23. 實作里程碑

### Milestone 0：Foundation

完成：

```text
專案骨架與設定讀取
.env / 環境變數驗證
TradingCalendar XTAI + overrides
Shioaji fixture provider
分鐘 K 聚合
market_bars schema 與驗證
strategy-params-v1
Manifest / Bundle / Order Plan JSON Schema
Decision Codes
```

驗收：

```text
可從 fixture 建立 20～60 日 market_bars
日曆可回答區間、今日與下一交易日
策略參數可穩定產生 params_hash
錯誤格式被拒絕
```

### Milestone 1：Simulation Closed Loop

使用 fixture，不先依賴正式 Shioaji 網路連線。

完成：

```text
Manifest Validator
Signal Bundle Parser
TradeExecutionEngine
Order Planner
Fake Broker
fills + cash_ledger
持倉與 FIFO 投影
對帳
端到端報告
```

驗收：

```text
有效 Manifest 可建立 BUY
過期或 revoked Manifest 阻擋 BUY
Manifest 失效時 SELL 仍可執行
params_hash 不一致被阻擋
限額正確
相同 Bundle 重跑不重複交易
部分成交正確更新
交易成本與 FIFO 可對帳
```

### Milestone 2：20～60 日確定性回測

完成：

```text
BacktestRunner
PointInTimeMarketData
HistoricalSignalGenerator
每日歷史 Bundle 保存
回測獨立 account_id
INITIAL_DEPOSIT
權益曲線與基本績效統計
```

資料：

```text
5～10 檔股票
20～60 個交易日
約 100～600 筆日 K
```

輸出：

```text
總損益
最大回撤
勝率
平均獲利
平均虧損
Profit Factor
交易次數
```

驗收：

```text
同一資料與設定重跑結果相同
策略無法讀到 D+1 資料
每筆成交可追溯到 Bundle 與 signal
資料缺漏會讓 run 明確失敗
```

### Milestone 3：3～6 個月初步觀察

完成：

```text
擴至 10～20 檔固定股票池
最近 3～6 個月
檢查交易成本、訊號密度與回撤
```

只回答：

```text
策略值得繼續研究
策略需要修改
策略應停止
```

不要求 100 筆交易，不自動產生正式交易授權。

### Milestone 4：每日模擬運行

完成：

```text
正式 Shioaji 行情同步
simulation-main 帳戶
單一 cron 排程
DailySimulationRunner stages
Manifest expiry preflight
同日期安全補跑
每日報告
```

持續時間：

```text
2～4 週
```

觀察：

```text
排程是否穩定
行情是否缺漏
Manifest 預警是否明確
執行順序是否正確
重跑是否安全
帳務是否持續一致
```

完成後才討論正式券商 API。

---

## 24. 第一批實作順序

```text
1. 建立設定模型與環境變數驗證
2. 實作 TradingCalendar + overrides
3. 建立 market_bars schema
4. 實作 FixtureMarketDataProvider
5. 實作 DailyBarAggregator 與 Validator
6. 實作策略 Pydantic Params Model
7. 實作 StrategyParameterCanonicalizer
8. 定義 Manifest / Bundle / Order Plan Schema
9. 建立有效與失敗 fixtures
10. 實作 Manifest Validator 與 active pointer
11. 實作 PointInTimeMarketData
12. 實作 SignalGenerator
13. 實作 Order Planner
14. 建立交易事實與投影資料表
15. 實作 Fake Broker
16. 實作單筆成交 transaction
17. 實作 reconcile / rebuild-projections
18. 實作 TradeExecutionEngine
19. 完成 Milestone 1 端到端測試
20. 實作 BacktestRunner
21. 跑 20～60 日回測
22. 接入 ShioajiMarketDataProvider
23. 實作 DailySimulationRunner
24. 建立 cron 與程序鎖
25. 運行 2～4 週每日模擬
```

不得先做前端，也不得先串正式下單。

---

## 25. MVP 完成定義

以下全部成立，才算 MVP 完成：

```text
Shioaji 或 fixture 分鐘 K 能穩定聚合為完整日 K
TradingCalendar 統一決定交易日與下一交易日
策略只能讀取截至 as_of_date 的資料
params_hash 在 Manifest 與 Bundle 間具有明確且可重現的語意
Manifest 可提前預警並在失效後阻擋 BUY、保留 SELL
BacktestRunner 與 DailySimulationRunner 共用同一執行內核
每日 run 可依 stage 安全重跑
同一 Bundle 不會重複成交或重複扣款
fills 與 cash_ledger 可重建持倉與損益投影
reconcile 能發現現金、持倉與 FIFO 不一致
20～60 日回測結果可重現
每日模擬可由單一 cron 連續運行 2～4 週
所有關鍵拒絕與等待狀態都有固定 Decision Code
```

完成後才評估：

```text
擴大資料區間
加入第二個策略
增加 RecordedSignalBundleSource replay
強化市場資料版本管理
串接券商模擬或正式下單
升級 Manifest 數位簽章
小額正式交易 Gate
```

---

## 26. 防止範圍再次膨脹

任何新增需求先回答：

```text
不做這件事，Milestone 1 或 Milestone 2 是否無法正確驗收？
```

若答案是否定，移到後續清單。

不得因以下理由提前擴充：

```text
未來可能多帳戶
未來可能多策略
未來可能分散式部署
未來可能正式下單
未來可能需要 Dashboard
```

MVP 要解決的是：

> 用少量資料與單一策略，建立一條相同邏輯可同時服務短回測與每日模擬、且交易事實可追溯與重建的完整閉環。
