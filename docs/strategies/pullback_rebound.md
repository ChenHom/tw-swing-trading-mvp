# Strategy Thesis: pullback_rebound

> 本文件須在任何正式（非 diagnostic）backtest 結果產出**前**寫死。看到結果後才回頭調整本文件
> 任一節（尤其合格/否決標準）即違反治理閉環，等同沒有 gate。修改訊號/出場邏輯＝新
> `strategy_version`，須開新版本的 thesis，不得覆寫本檔。

- **strategy_id**: `pullback_rebound`
- **strategy_version**: `1.0.0`（對應 `config/strategies/pullback_rebound.yaml` / [pullback_rebound.py](../../src/strategy/pullback_rebound.py)）
- **狀態**：現役（[config/trading.yaml](../../config/trading.yaml) `entry_strategies` 之一）；目前所有 backtest 結果僅能是
  diagnostic（21 檔人工固定清單非 PIT），尚無 `RESEARCH_PASS` 資格。

## Edge 來源

**均值回歸（mean reversion）疊加趨勢延續濾網**：多頭結構成立時，價格短暫回踩月線
（20MA）支撐後若 K 線轉強（收紅且收盤價高於前一日收盤），代表短期賣壓已釋放、買盤
重新接手——本質是在確認的中期上升趨勢中，等一個短期超賣的相對低點進場，而非單純
逆勢猜底。

## 為何在台股存在

台股個股波動大、籌碼面（融資融券、主力進出）造成短期超漲超跌頻繁，但只要中期
（季線之上）趨勢未破，短期回檔常因技術性支撐（月線）與既有多頭買盤回補而出現
反彈。本策略要求「季線之上 + 月線在季線之上（`sma_short > sma_long`）+ 大盤多頭」
三重濾網，刻意排除「逆勢猜底」情境，只在中期趨勢健康的回檔中進場。

## 有效 / 失效市場

- **有效**：大盤多頭格局、個股已處於確立的中期上升趨勢（季線上揚、月線在季線之上）、
  回踩月線後出現至少一日強勢收紅 K 棒。
- **失效**：大盤空頭或盤整（訊號被大盤濾網全數阻擋）；個股中期趨勢未明朗（月線季線
  糾結，`is_uptrend` 條件本身即不穩定，訊號頻率異常高或低皆為警訊）；月線支撐失守後
  才反彈（即真正的趨勢反轉，非健康回檔——本策略的進場時間點設計上應早於此情境，
  若觀察到大量「破月線才反彈」的進場樣本，代表訊號定義本身可能需重新檢視，而非
  事後調整門檻）。

## 訊號 / 進場 / 出場 / 持有期 / 標的池 / 容量

| 項目 | 規格 | 來源 |
|---|---|---|
| 進場訊號 | close > 60MA 且 20MA > 60MA（中期多頭結構）+ 當日 low 觸及 20MA×(1+2%) 緩衝內（回踩支撐）+ 當日收紅且收盤價高於前一日收盤（K 線轉強）+ 大盤 > 60MA | [pullback_rebound.py:45-49](../../src/strategy/pullback_rebound.py#L45-L49) |
| 進場執行 | 訊號日收盤後產生 BUY，下一交易日（D+1）依本系統 raw 成交模型成交 | runner D+1 排程 |
| 出場（四層，任一觸發即出） | 固定停損 -5%（加權均價基準，較 trend_breakout 緊，因進場本身已貼近支撐）／移動停利（自最高收盤回落 8%）／均線失效（收盤低於 20MA×98%、連續 2 日確認，緩衝避免進出互咬）／時間停損（20 個交易日內報酬未達 +5%） | [risk_exit.py](../../src/strategy/risk_exit.py)、`config/strategies/pullback_rebound.yaml` `exit:` |
| 持有期 | 理論上限 20 個交易日（時間停損），實際視最先觸發的出場條件 | — |
| 標的池 | 今日：人工固定 21 檔（**diagnostic-only，非 PIT**） | `UniversePolicy.is_diagnostic_only` |
| 容量 | 每筆下單預算 20,000 TWD（`order_budget_twd`），現階段容量非主要風險來源，放大資金前須重新估算成交量參與率與零股流動性 | `config/strategies/pullback_rebound.yaml` |

## 合格 / 否決標準（看結果前寫死）

晉級 `RESEARCH_PASS` 前置條件（缺一即 `INVALID`，見 [verdict.py](../../src/application/runners/verdict.py)）：
1. universe 必須為 PIT（非 `diagnostic:` 前綴）。
2. 回測窗須完整涵蓋 2022 年（`start_date <= 2022-01-01` 且 `end_date >= 2022-12-31`）。
3. `regime_gate_thresholds` 須已為本 `strategy_version` 寫入具體數值（下方為本版本提議數值，
   **尚未寫入任何資料庫，需經明確「註冊」動作才生效，不會被任何程式自動套用**）：

| 欄位 | 提議數值 | 理由 |
|---|---|---|
| `max_regime_drawdown` | 0.25 | 進場貼近支撐、停損較緊（-5% vs trend_breakout 的 -7%），回撤上限門檻同步收緊 |
| `min_expectancy_ci_lower` | 0 | 逐筆期望值 bootstrap 5% CI 下界須 ≥0 |
| `max_bear_underperformance` | 0.15 | 暫不參與 Phase 1 評估（需 regime 標記，Phase 3+ 才建），先寫死供未來沿用 |
| `min_effective_sample_size` | 30 | 進場日聚類後的有效樣本下限 |
| `max_profit_concentration` | 0.40 | 獲利 Herfindahl 上限，避免「賺錢全靠一兩筆」 |

違反任一可評估門檻（1、2、4、5）→ `REJECTED`。`max_bear_underperformance` 暫不參與門檻
評估（regime 偵測未建），但數值仍先寫死、不可等該機制建好後再回頭調寬。

修改本策略訊號或出場邏輯 → 視為新 `strategy_version`，須另開新版 thesis 文件，
不得回頭修改本版任何標準以配合已知結果。
