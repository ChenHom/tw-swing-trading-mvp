# Strategy Thesis: breakout_shadow_filter

> 本文件須在任何正式（非 diagnostic）backtest 結果產出**前**寫死。看到結果後才回頭調整本文件
> 任一節（尤其合格/否決標準）即違反治理閉環，等同沒有 gate。修改訊號/出場邏輯＝新
> `strategy_version`，須開新版本的 thesis，不得覆寫本檔。

- **strategy_id**: `breakout_shadow_filter`
- **strategy_version**: `1.0.0`（對應 `config/strategies/breakout_shadow_filter.yaml` / [breakout_shadow_filter.py](../../src/strategy/breakout_shadow_filter.py)）
- **研究家族**：`trend_breakout`（本策略＝`trend_breakout` 1.0.0 進場條件 **加一條** 影線濾網，其餘逐字相同）。
  因程式隔離需要（不得動到 live 的 `trend_breakout.yaml`）而另開 strategy_id，但**試驗次數、lockbox、
  過擬合責任一律算在 `trend_breakout` 家族**，不得因換 id 而重置。
- **狀態**：研究 Challenger，**不加入任何帳號的 `entry_strategies`**、不影響 15:10/15:12 cron。
- **起源**：2026-10 社群貼文的「N 日下影線占比」XScript 指標（Σ下影線 / Σ(高−低) × 100，N=7），
  以及其反駁「只看下影線、看不到上影線的賣壓」。兩者皆為單一個股（欣興 3037）看圖敘事，**無任何回測證據**。

## Edge 來源

**假突破過濾（failed-breakout / overhead supply）**：`trend_breakout` 的訊號是「收盤創 20 日新高 + 帶量」。
若突破前後幾天的 K 棒反覆出現長上影線（盤中衝高、收盤被賣回），代表上方有持續的供給（套牢解套、
獲利了結、主力出貨），突破後的延續性應較差。反之，若近期下影線（盤中殺低被買回）多於上影線，
代表承接強於賣壓，突破較可能延續。

假設：**在 trend_breakout 的訊號中，剔除「近 7 日上影線總和 > 近 7 日下影線總和」者，
可提高逐筆期望值**（剔除的主要是會被停損/時間停損打掉的假突破）。

這是對既有 edge 的**品質篩選**，不是新的 edge 來源；若 trend_breakout 本身的動能 edge 不存在，本濾網不可能無中生有。

## 為何在台股存在

- 台股散戶比重高、融資追價常見；帶量突破日的長上影線常是「追價買盤被上方籌碼倒貨」的足跡。
- 前高附近常有套牢籌碼（解套賣壓），在 20 日新高附近尤其集中。
- 反向論點（須誠實列出）：影線是日內路徑資訊，日 K 只保留四個點，雜訊大；台股漲停鎖死、
  開盤跳空等情況會使影線失真；動能文獻中幾乎沒有「影線」作為穩健因子的證據。**先驗成功機率低**。

## 指標定義（全部只用 ≤ 訊號日 D 的日 K，無 lookahead）

對訊號日 D 及之前共 `shadow_window_days`（=7）根 K 棒：

```
upper_i = high_i − max(open_i, close_i)
lower_i = min(open_i, close_i) − low_i
U = Σ upper_i ，L = Σ lower_i ，R = Σ (high_i − low_i)
```

- 價格皆為系統整數（元×10000），全程整數運算，無浮點比較。
- 除權息調整為同日四價等比例縮放，影線差值同比例縮放，`U > L` 的比較不受影響。
- `R == 0`（7 日全為一價到底，如連續鎖漲停無量）→ `U = L = 0` → 濾網**不剔除**（無資訊時退回基準行為）。

**濾網**：`U > L` → 不產生 BUY 訊號（記為被濾掉）；否則與 `trend_breakout` 相同。

選 `U > L`（無門檻參數）而非「上影線占比 > X%」：X% 沒有任何非資料來源的先驗值可選，選了就是在調參；
`U > L` 是貼文作者與反駁者討論的直接對稱形式（承接 vs 賣壓誰大），唯一參數 N=7 照抄原貼文，**不調**。

## 訊號 / 進場 / 出場 / 持有期 / 標的池 / 容量

| 項目 | 規格 | 來源 |
|---|---|---|
| 進場訊號 | `trend_breakout` 1.0.0 全部條件（收盤創 20 日新高 + 量 > 20 日均量 1.5 倍 + close > 個股 60MA + 大盤 > 60MA）**且** 近 7 日 `U ≤ L` | [breakout_shadow_filter.py](../../src/strategy/breakout_shadow_filter.py) |
| 訊號排序 | 與 trend_breakout 相同（`ranking_score = volume_ratio`），避免資金排序差異混入比較 | 同上 |
| 進場執行 | 訊號日收盤後產生 BUY，D+1 依本系統 raw 成交模型成交 | runner D+1 排程 |
| 出場 | **與 trend_breakout 1.0.0 `exit:` 逐字相同**（-7% 停損／8% 移動停利／20MA 連 2 日跌破／20 日未達 +5% 時間停損） | `config/strategies/breakout_shadow_filter.yaml` |
| 持有期 | 同 trend_breakout | — |
| 標的池 | PIT `liquidity-top150-v1`（與 trend_breakout 基準同一份） | `market build-universe` |
| 容量 | 每筆 20,000 TWD，同 trend_breakout | — |

## 合格 / 否決標準（看結果前寫死）

### A. 絕對門檻（verdict 狀態機）

與 trend_breakout 1.0.0 **完全相同**（同家族、同出場，不得因是濾網版而放寬）：

| 欄位 | 數值 |
|---|---|
| `max_regime_drawdown` | 0.30 |
| `min_expectancy_ci_lower` | 0 |
| `max_bear_underperformance` | 0.15（暫不參與評估，同既有策略） |
| `min_effective_sample_size` | 30 |
| `max_profit_concentration` | 0.40 |

PIT universe、窗 2018-01-01 ~ ≥2022-12-31（實際用與基準相同的 2018-01-01 ~ 2026-06-22）、初始 300,000 TWD。

### B. 增量門檻（是否值得取代 trend_breakout，僅在 A=RESEARCH_PASS 時才看）

基準＝trend_breakout 1.0.0 在**同一 research.db、同 universe、同窗、同初始資金**的 PIT 重跑結果
（不可引用 2026-06-24 舊報告數字，需同庫重跑以消除資料差異）。**以下全部成立**才判「濾網有效」：

1. 逐筆期望值（平均每筆淨損益，TWD）> 基準。
2. 期望值 bootstrap 5% CI 下界 > 基準。
3. 成本占毛利比 < 基準。
4. 總淨損益 ≥ 基準 × 0.8（濾網不得靠大量砍交易換取漂亮的平均數；容許因交易變少損失至多 20%）。
5. 濾網剔除率（被濾掉的訊號 / 基準訊號）介於 5% ~ 60%。< 5%＝濾網幾乎沒作用、差異屬雜訊；
   > 60%＝已變成另一支策略，與「品質篩選」假設不符，判無效。

任一不成立 → 結論「影線濾網對 trend_breakout 無增量價值」，**不得**以改 N、改成門檻式、改只看訊號日
等方式重試同一想法（那些是同家族的新試驗，需另開版本 thesis 並計入試驗次數，且本次結果已被看過）。

### C. 治理註記

- **lockbox**：trend_breakout 家族的 2018~2026 全窗已在 2026-06-24 被基準看過，**此家族無未開封資料**。
  本次結果只能是研究證據；即使 A、B 全過，唯一可信的下一步是 simulation-main 影子 forward 驗證，
  不得直接上國泰。
- **DSR 試驗次數**：Research Ledger 以 strategy_id 計數，本策略 id 新，`num_trials` 會被低估。
  DSR 不是 verdict gate，但報告須註明「家族累計試驗數 = trend_breakout 已登錄數 + 1」。
- **本 thesis 只登錄一個參數組合**（N=7、`U > L`）。不做參數高原掃描：沒有可掃的連續門檻，
  掃 N 即是 fishing。

## 已知風險 / 失效情境

- 漲停鎖死突破日：上下影線皆 0，濾網對此日無判斷力，由前 6 日決定。
- 跳空開高走低收紅（收 > 開但收 < 前日收）不影響影線定義，但開高走低本身是賣壓訊號，本濾網看不到。
- 濾網和「帶量」條件可能高度共線（爆量日常伴長上影），剔除率可能偏高——由 B5 把關。
- 樣本量：基準有效樣本 366，濾掉一部分後仍需 ≥ 30（A 門檻），預期可滿足。
