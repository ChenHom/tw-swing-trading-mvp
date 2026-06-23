# Strategy Thesis: trend_breakout

> 本文件須在任何正式（非 diagnostic）backtest 結果產出**前**寫死。看到結果後才回頭調整本文件
> 任一節（尤其合格/否決標準）即違反治理閉環，等同沒有 gate。修改訊號/出場邏輯＝新
> `strategy_version`，須開新版本的 thesis，不得覆寫本檔。

- **strategy_id**: `trend_breakout`
- **strategy_version**: `1.0.0`（對應 `config/strategies/trend_breakout.yaml` / [trend_breakout.py](../../src/strategy/trend_breakout.py)）
- **狀態**：現役（[config/trading.yaml](../../config/trading.yaml) `entry_strategies` 之一）；目前所有 backtest 結果僅能是
  diagnostic（21 檔人工固定清單非 PIT），尚無 `RESEARCH_PASS` 資格。

## Edge 來源

**趨勢延續（trend continuation / momentum）**：價格創新高 + 量能放大代表買盤動能轉強，
動能效應（momentum effect）在多數市場有實證支持——新資訊或籌碼轉強往往分批消化，
而非單日反映完畢，因此突破後續仍有機率延續一段時間。

## 為何在台股存在

台股以散戶成交占比高、機構資金進場常分批執行、消息面與籌碼面消化有時間滯後，
帶量突破後常見後續資金跟進（FOMO chasing）。但台股 long-only 個股報酬高度受大盤
Beta 主導，純粹個股動能訊號在大盤逆風時容易失效——這正是本策略疊加大盤 60MA 濾網
（`index_above_ma`）的理由：只在大盤多頭格局下尋找個股動能訊號，降低逆勢假突破比例。

## 有效 / 失效市場

- **有效**：大盤多頭格局（`index_close > index_sma_60`）、流動性足夠以至於量比訊號
  不受單一大戶單一交易日操弱、無重大除權息/減資干擾當期量能基準。
- **失效**：大盤空頭或盤整格局（指數跌破 60MA，訊號被濾網全數阻擋，理論上零進場）；
  個股流動性過低導致 20 日均量基準失真；極端事件造成單日量能異常（如除權息當沖膨脹）。

## 訊號 / 進場 / 出場 / 持有期 / 標的池 / 容量

| 項目 | 規格 | 來源 |
|---|---|---|
| 進場訊號 | 收盤創 20 日新高 + 當日量 > 20 日均量 1.5 倍 + close > 個股 60MA + 大盤 > 60MA | [trend_breakout.py:48-53](../../src/strategy/trend_breakout.py#L48-L53) |
| 進場執行 | 訊號日收盤後產生 BUY，下一交易日（D+1）依本系統 raw 成交模型成交（無法保證開盤價） | runner D+1 排程 |
| 出場（四層，任一觸發即出） | 固定停損 -7%（加權均價基準）／移動停利（自最高收盤回落 8%）／均線失效（連續 2 日收盤跌破 20MA）／時間停損（20 個交易日內報酬未達 +5%） | [risk_exit.py](../../src/strategy/risk_exit.py)、`config/strategies/trend_breakout.yaml` `exit:` |
| 持有期 | 理論上限 20 個交易日（時間停損），實際視最先觸發的出場條件 | — |
| 標的池 | 今日：人工固定 21 檔（**diagnostic-only，非 PIT**） | `UniversePolicy.is_diagnostic_only` |
| 容量 | 每筆下單預算 20,000 TWD（`order_budget_twd`），對中大型股市場衝擊可忽略；放大資金規模前須重新估算成交量參與率與零股流動性，現階段容量非主要風險來源 | `config/strategies/trend_breakout.yaml` |

## 合格 / 否決標準（看結果前寫死）

晉級 `RESEARCH_PASS` 前置條件（缺一即 `INVALID`，見 [verdict.py](../../src/application/runners/verdict.py)）：
1. universe 必須為 PIT（非 `diagnostic:` 前綴）。
2. 回測窗須完整涵蓋 2022 年（`start_date <= 2022-01-01` 且 `end_date >= 2022-12-31`）。
3. `regime_gate_thresholds` 須已為本 `strategy_version` 寫入具體數值（下方為本版本提議數值，
   **尚未寫入任何資料庫，需經明確「註冊」動作才生效，不會被任何程式自動套用**）：

| 欄位 | 提議數值 | 理由 |
|---|---|---|
| `max_regime_drawdown` | 0.30 | 高於一般 long-only 波段策略可接受回撤上限的保守值 |
| `min_expectancy_ci_lower` | 0 | 逐筆期望值 bootstrap 5% CI 下界須 ≥0（不能僅平均為正、下界仍為負） |
| `max_bear_underperformance` | 0.15 | 暫不參與 Phase 1 評估（需 regime 標記，Phase 3+ 才建），先寫死供未來沿用 |
| `min_effective_sample_size` | 30 | 進場日聚類後的有效樣本下限，避免少數幾個進場日撐起全部結論 |
| `max_profit_concentration` | 0.40 | 獲利 Herfindahl 上限，避免「賺錢全靠一兩筆」 |

違反任一可評估門檻（1、2、4、5）→ `REJECTED`。`max_bear_underperformance` 暫不參與門檻
評估（regime 偵測未建），但數值仍先寫死、不可等該機制建好後再回頭調寬。

修改本策略訊號或出場邏輯 → 視為新 `strategy_version`，須另開新版 thesis 文件，
不得回頭修改本版任何標準以配合已知結果。
