# Strategy Thesis: trend_rider

> 本文件在 trend_rider 任何回測結果產出**前**寫死（2026-06-23）。動機來自現役 trend_breakout/
> pullback_rebound 在 R-T3 真實回測中暴露的結構缺陷：持續上升趨勢中嚴重低捕獲（2024 AI 大年
> 個股 +30~114%，現役策略卻 −2.4%/+6.4%）。**參數依標準趨勢跟蹤慣例設死，不得為補抓已看過的
> 2024 而調**＝過擬合。修改訊號/出場邏輯＝新 `strategy_version`，須另開新版 thesis。

- **strategy_id**: `trend_rider`
- **strategy_version**: `1.0.0`（對應 `config/strategies/trend_rider.yaml` / [trend_rider.py](../../src/strategy/trend_rider.py)）
- **狀態**：Challenger（與現役兩支並行比較，不取代）；目前所有 backtest 僅 diagnostic（21 檔人工固定清單非 PIT）→ 必 INVALID。

## Edge 來源

**趨勢延續 / 動能（momentum）+ 讓贏家跑（let winners run）**：價格創數月新高且站穩上升中期均線，
代表一段已確立、由真實資金推動的趨勢；動能效應使這類趨勢傾向延續數月至數年。與 trend_breakout
的差異**不在進場、在出場**：trend_breakout 用 20 日時間停損 + 8% 緊移動停利，把贏家在大趨勢
早期就砍掉；trend_rider 停用時間停損、用 25% 寬移動停利 + 60 日均線跌破，讓贏家在整段趨勢中跑完。

## 為何在台股存在

台股以電子硬體/半導體為核心，產業具明確的資本支出循環（如當前 AI 基建超級循環）。這類循環一旦
啟動，龍頭股（台積電、AI 伺服器/散熱供應鏈）的趨勢往往持續多年、回檔淺而短。緊出場策略會在每次
正常回檔被洗出、錯過後續主升段；「讓贏家跑」正是要捕獲這種**少數、巨大、持久**的趨勢，用寬停損
容忍中途波動換取整段報酬。代價是平庸/盤整年會多付一些回吐。

## 有效 / 失效市場

- **有效**：強勁、持久、廣泛的上升趨勢（資本支出循環、產業主升段）；大盤站穩 60MA。少數大贏家
  能跑很久，寬停損讓它跑完。
- **失效**：① 急殺崩盤 —— **這是本策略相對現役最大的弱點**：25% 寬移動停利在崩盤中會回吐遠多於
  現役的緊停損（崩盤防守換得 upside 捕獲，必有代價，回測須量化）。② 長期橫盤/箱型 —— 一再創高
  又拉回，寬停損下每次小賺變小賠，被趨勢假突破反覆消耗。③ 急漲急回的主題炒作（非真實基本面趨勢）。

## 訊號 / 進場 / 出場 / 持有期 / 標的池 / 容量

| 項目 | 規格 | 來源 |
|---|---|---|
| 進場訊號 | close > 60MA 且 60MA 上升 + 創 60 日新高 + 大盤 > 60MA（保留崩盤防守濾網） | [trend_rider.py:48-54](../../src/strategy/trend_rider.py#L48-L54) |
| 進場執行 | 訊號日收盤後 BUY，D+1 依 raw 成交模型成交 | runner D+1 |
| 出場（讓贏家跑，任一觸發即出） | 災難固定停損 −12%／寬移動停利（自最高收盤回落 25%）／長均線跌破（連 3 日收盤跌破 60MA）／**時間停損停用**（999 日） | [risk_exit.py](../../src/strategy/risk_exit.py)、`config/strategies/trend_rider.yaml` `exit:` |
| 持有期 | 無時間上限（核心差異）；實際視趨勢何時真破 | — |
| 標的池 | 今日：人工固定 21 檔（**diagnostic-only，非 PIT**） | `UniversePolicy.is_diagnostic_only` |
| 容量 | 每筆 20,000 TWD；放大前須估成交量參與率/零股流動性 | `config/strategies/trend_rider.yaml` |

## 合格 / 否決標準（看結果前寫死）

晉級 `RESEARCH_PASS` 前置（缺一即 `INVALID`，見 [verdict.py](../../src/application/runners/verdict.py)）：
1. universe 必須為 PIT（非 `diagnostic:` 前綴）。
2. 回測窗須完整涵蓋 2022（`start_date <= 2022-01-01` 且 `end_date >= 2022-12-31`）。
3. `regime_gate_thresholds` 須已為本版本寫入具體數值（下方為提議數值，尚未寫入 DB）：

| 欄位 | 提議數值 | 理由 |
|---|---|---|
| `max_regime_drawdown` | 0.35 | 寬停損的趨勢策略本就容忍較大回撤，門檻較現役寬，但仍須有上限 |
| `min_expectancy_ci_lower` | 0 | 逐筆期望值 bootstrap 5% CI 下界須 ≥0 |
| `max_bear_underperformance` | 0.20 | 暫不參與 Phase 1 評估（需 regime 標記，Phase 3+ 才建），先寫死 |
| `min_effective_sample_size` | 20 | 順勢交易者交易筆數較少（持有久），有效樣本下限相應放寬，但不可過少 |
| `max_profit_concentration` | 0.50 | 趨勢策略獲利天生較集中於少數大贏家，門檻較現役寬，但仍須防「全靠一筆」 |

**本策略專屬否決條件（Challenger 對照）**：若 trend_rider 的崩盤防守（2022/COVID 回吐）惡化到
回撤門檻被突破，且 2024 型趨勢捕獲未顯著優於現役 → 證明「讓贏家跑」在此標的池/成本結構下
upside 與 defense 不可兼得，**否決本版**，回去深化 pullback_rebound。

違反任一可評估門檻（1、2、4、5）→ `REJECTED`。修改訊號/出場邏輯＝新 `strategy_version`。
