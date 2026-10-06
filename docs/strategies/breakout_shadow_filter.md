# Strategy Thesis: breakout_shadow_filter

> 本文件須在任何正式（非 diagnostic）backtest 結果產出**前**寫死。看到結果後才回頭調整本文件
> 任一節（尤其合格/否決標準）即違反治理閉環，等同沒有 gate。修改訊號/出場邏輯＝新
> `strategy_version`，須開新版本的 thesis，不得覆寫本檔。

- **strategy_id**: `breakout_shadow_filter`
- **strategy_version**: `1.0.0`（`config/strategies/breakout_shadow_filter.yaml` / [breakout_shadow_filter.py](../../src/strategy/breakout_shadow_filter.py)）
- **研究家族**：`trend_breakout`。本策略＝`trend_breakout` 1.0.0 進場條件加一條影線濾網，其餘參數與出場逐字相同。
  另開 strategy_id 只為程式隔離（不動 live 的 `trend_breakout.yaml`）；試驗次數、lockbox 與過擬合責任**一律算在
  `trend_breakout` 家族**（程式面：`research_ledger.STRATEGY_FAMILIES`，見 §D）。
- **狀態**：研究 Challenger。**不加入任何帳號的 `entry_strategies`**；影子上線另需使用者決定（§E）。
- **起源**：2026-10 社群貼文的「N 日下影線占比」XScript 指標（Σ下影線 / Σ(高−低) × 100，N=7），以及反駁
  「只看下影線、看不到上影線的賣壓」。兩者都是單一個股（欣興 3037）的看圖敘事，**沒有任何回測證據**。
- **審查**：本版已經過一輪對抗式審查（4 BLOCKER / 8 MAJOR / 7 MINOR），修訂對照見文末附錄。

## 預先登錄聲明（凍結前狀態）

- 撰寫本文件的環境**沒有** `data/research.db`。凍結前**沒有**對任何真實 K 棒或 trend_breakout 的交易計算過影線，
  也**沒有**跑過任何 diagnostic 或正式回測。唯一跑過的是用隨機合成資料做的程式煙霧測試（結果無意義）。
- 凍結點＝含本文件的 git commit。**第一次正式 run 的結果即為結論**；之後若因 bug 重跑，必須記錄原因與 diff，
  兩次結果都要報告，不得只留第二次。

## Edge 假設

**假突破過濾（overhead supply）**：`trend_breakout` 的訊號是「收盤創 20 日新高 + 帶量」。如果突破前後幾天
反覆出現長上影線（盤中衝高、收盤被賣回），代表上方有持續的供給（解套、獲利了結、出貨），突破後的延續性應該較差。

假設：在 trend_breakout 的候選訊號中，「近 7 日上影線總和 > 下影線總和」的那一組，事後淨報酬**顯著低於**其餘候選。

這是對既有動能 edge 的品質篩選。樣本內的改善也可能純屬選擇偏誤，所以以置換檢定（§B）與影子 forward 驗證（§E）為準。

**先驗成功機率低**：
- 影線是日內路徑資訊，日 K 只保留四個點，雜訊大。
- 鎖漲停、跳空等情況會讓影線失真。
- 動能文獻幾乎沒有以影線作為穩健因子的證據。

## 指標定義（只用 ≤ 訊號日 D 的日 K，無 lookahead）

取「截至 D 的最近 7 根完整 K 棒」（`history(limit=7)`，`is_complete=1`，`price_basis=raw`）：

```
upper_i = high_i − max(open_i, close_i)
lower_i = min(open_i, close_i) − low_i
U = Σ upper_i ，L = Σ lower_i
濾網：U > L → 不產生 BUY；否則與 trend_breakout 相同
```

- **單位**：價格皆為系統整數（元×10000），全程整數運算。
- **等價式**：U − L = Σ(H + L − O − C)，所以 U > L ⟺ Σ(H+L)/2 > Σ(O+C)/2，也就是「7 根 K 棒的實體中點平均落在全距中點下方」。
  這是 close-location 類指標的變形，不是全新資訊。總和用絕對價差、不做標準化，因此全距最大的幾天權重最大；
  帶量突破日通常全距最大，濾網實際上很接近「D 當天 (H−C) 是否大於 (O−L)」，和量能、突破條件可能共線。
- **價格基準**：寫死 `raw`（CLI 預設），基準與 challenger 必須相同。在 raw 下，每根 K 棒的影線都以當日價位計；
  7 日窗跨除權息日時，前後 K 棒的權重會略有差異。接受這個差異，不處理。
- **退化情況**：7 根全為一價到底（如連續鎖漲停）時 U = L = 0，濾網不剔除，退回基準行為。歷史不足 7 根同樣不剔除
  （實際上基準需要 ≥ 60 根，不會發生）。
- **stale bar**：若 D 當天停牌，「最近 7 根」會跨更多日曆天、最後一根也不是 D。這和基準的 stale bar 行為完全相同，**刻意不修**，
  以免混入第二個差異。

**這不是「無參數」**。以下都是選擇，本版各只登錄一個值，任何更動都是新試驗，須另開 thesis 版本：

| 選擇 | 本版值 |
|---|---|
| 窗長 | N = 7（照抄原貼文；原貼文是為另一個指標、看 3037 選的） |
| 比較形式 | U > L（比例門檻 1.0，等權） |
| 窗口位置 | 含 D |
| 加權 | 不標準化（絕對價差） |
| 其他替代規格 | 只看 D 單日、上影線占比門檻、量加權，**全部未登錄** |

## 訊號 / 進場 / 出場 / 標的池 / 容量

| 項目 | 規格 |
|---|---|
| 進場訊號 | trend_breakout 1.0.0 全部條件（收盤創 20 日新高、量 > 20 日均量 1.5 倍、close > 個股 60MA、大盤 > 60MA）**且** U ≤ L。實作上以組合方式呼叫原封不動的 `TrendBreakoutStrategy` 再過濾，live 程式碼零改動 |
| 進場執行 | D 收盤後產生 BUY，D+1 依 FakeBroker 成交模型成交（開盤價 ± 滑價、零股 ×3、鎖漲停／零量不成交） |
| 出場 | 與 trend_breakout 1.0.0 `exit:` 逐字相同：-7% 固定停損、8% 移動停利、20MA 連 2 日跌破、20 日未達 +5% 時間停損 |
| 標的池 | PIT `liquidity-top150-v1`（與 2026-06-24 基準同一份 policy） |
| 容量 | 每筆 20,000 TWD；manifest 限額須與 trend_breakout 相同 |

**已知排序 bug（照實揭露、刻意不修）**：回測的 `signal_items` 表沒有存 `ranking_score`，讀回 bundle 後該值為 None，
engine 只好退回**代號升冪**排序（`backtest.py` `_find_bundles_by_execution_date`、`engine.py` buys.sort）。
在每日最多 2 檔、同時最多 5 檔的限制下，實際搶到名額的是代號小的股票，而不是量比大的。2026-06-24 的基準
RESEARCH_PASS 也是在這個排序下得到的。本研究的組合回測**兩邊都不修**；若要修，視為基準的新版本，須重新裁決基準。
這也是組合回測只當次要檢查的原因之一。

## 合格 / 否決標準（看結果前寫死）

### §B 主檢定：訊號層級配對分析（決定濾網有無增量）

組合回測受容量、替補與持倉路徑影響：濾掉一筆會讓出名額給原本被擠掉的訊號，量到的是「濾網＋替補＋路徑」的合成效果。
主檢定因此改在訊號層級，與容量無關。

**程式**：`scripts/shadow_filter_study.py` → `src/application/research/shadow_filter_study.py`，純讀、固定 seed 1337。

1. **候選集合**：PIT universe 每個交易日 D，以原封不動的 `TrendBreakoutStrategy`（空持倉）列出全部基準候選 (symbol, D)。
   不管是否已持有，也不管容量。
2. **逐筆模擬**：每筆獨立模擬，規則與回測同源：
   - 股數 = 20,000 // D 收盤；整張＋零股拆單；滑價 10 bps、零股 ×3；手續費 0.1425%（最低 20 元）、賣出稅 0.3%。
   - 出場判斷與 `RiskExitEngine.explain_exit` 同定義、同優先序（有隨機路徑一致性測試保證）；出場訊號隔日開盤賣，遇鎖跌停／零量順延。
   - 窗末仍持有者，以最後收盤扣賣出費稅設算。
   - 報酬單位＝淨報酬率（淨損益 / 買進成本）。
   - 狀態為 `UNFILLED_ENTRY`（D+1 鎖漲停）、`NO_NEXT_BAR`、`BUDGET_TOO_SMALL`（股價 > 2 萬）的候選不計入兩組，只報告筆數。
3. **分組**：剔除組＝U > L；保留組＝其餘。Δ = 保留組平均淨報酬 − 剔除組平均淨報酬。

**濾網有效（`FILTER_EFFECTIVE`）須 P1–P4 全部成立**：

| # | 條件 | 定義 |
|---|---|---|
| P1 | Δ 的 cluster bootstrap 單尾 5% 下界 > 0 | 以訊號日 D 為群重抽，2,000 次 |
| P2 | 安慰劑置換檢定 p < 0.05 | 隨機剔除與實際**同筆數**的候選 2,000 次；p = (1 + #{安慰劑保留組平均 ≥ 實際保留組平均}) / 2,001 |
| P3 | 剔除率介於 5%～60% | 剔除組 / (保留組 + 剔除組)，即 §B-1 候選集合中的比例，與持倉路徑無關。< 5%＝差異屬雜訊；> 60%＝已是另一支策略 |
| P4 | 保留組平均淨報酬 > 0 | 濾網不能只是「少虧一點」 |

任一不成立 → `NO_INCREMENT`，結論「影線濾網對 trend_breakout 無增量價值」。

**只報告、不作門檻**：
- U == L 的比例（台股有跳動單位，低價股的影線常只有 1 tick，平手可能很多）。
- 兩組的出場原因分布（檢查「剔除的主要是停損／時間停損」這個機制假設）。
- 最小可偵測差異 MDE ≈ 2.49 × σ合併 × √(1/n保留 + 1/n剔除)（單尾 5%、檢定力 80%；只用合併標準差，不看分組結果）。

**檢定力的事前預估（誠實列出）**：假設逐筆淨報酬標準差約 8%、保留 1,500 筆、剔除 500 筆，MDE 約 1 個百分點。
一個影線濾網的真實效果很可能小於這個值，所以**本研究大概率落在 NO_INCREMENT**。若結果為 NO_INCREMENT 且輸出的
MDE > 1.0 個百分點，結論寫「檢定力不足、無法區分」，不寫「已證明無效」；兩種寫法都不得當成改參數重試的理由。

### §C 次要檢查：組合回測（可實作性，非增量判定）

只在 §B = `FILTER_EFFECTIVE` 時才有決策意義；但無論 §B 結果如何都要跑並報告，避免只在好結果時才看。

**凍結條件**：
- 基準（`trend_breakout`）與 challenger 從**同一份** research.db 快照各複製一份來跑（copy-per-run），記錄快照 sha256。
- 兩份 fingerprint 除 strategy_id／params_hash 外必須完全相同：slippage、`universe_policy_version`、price_basis=raw、
  manifest 限額、窗 2018-01-01～2026-06-22、初始 300,000。
- 基準重跑的裁決若 ≠ RESEARCH_PASS，或主要數字（總報酬、有效樣本、期望值 CI 下界）與 2026-06-24 紀錄不同，
  **整個研究停止**，先查原因，不得照常比較。

**A 門檻（verdict 狀態機）**：與 trend_breakout 1.0.0 完全相同，已寫入 `scripts/register_regime_gates.py`，**須在回測前 commit 並執行**：

| 欄位 | 數值 |
|---|---|
| `max_regime_drawdown` | 0.30 |
| `min_expectancy_ci_lower` | 0 |
| `max_bear_underperformance` | 0.15（暫不參與評估，同既有策略） |
| `min_effective_sample_size` | 30 |
| `max_profit_concentration` | 0.40 |

**對照欄位（只描述，不作門檻）**。每一項都指定報告中的確切欄位：

| 項目 | 報告欄位 |
|---|---|
| 逐筆淨損益（TWD） | `Σ fifo_matches 淨損益 / robustness.trade_count_raw`；一列 fifo_match＝一次 FIFO 撮合，不一定等於一筆完整進出 |
| 期望值 CI 下界 | `robustness.expectancy_bootstrap_ci_lower`（單一 run 的 iid 估計，不是配對比較） |
| 成本占毛利 | `cost_ratio.cost_to_gross_pnl_ratio`；分母 ≤ 0 時為 None，照實報告 |
| 總損益 | `statistics.total_pnl`（含期末未平倉市值）與 `cost_ratio.gross_realized_pnl` 兩者都列 |
| 未成交 | `unfilled_summary.by_reason` |

### §D 最終判定與紀錄

| §B | §C 的 A 門檻 | 結論 | 下一步 |
|---|---|---|---|
| FILTER_EFFECTIVE | RESEARCH_PASS | 濾網有效且組合可行 | 可提請使用者決定是否影子上線（§E） |
| FILTER_EFFECTIVE | 非 PASS | 訊號層有效、組合層不可行 | 不上線；記錄原因 |
| NO_INCREMENT | 任何 | 無增量 | 不上線。即使 A 門檻顯示 RESEARCH_PASS，也只是繼承基準的 edge，**不代表濾網有用** |

- 注意 `verdict.py` 只看 A 門檻：§B = NO_INCREMENT 時，DB 仍可能留下 `RESEARCH_PASS`。這不得解讀為可上線。
  結論必須寫進 engineering-log；若之後要升級，以 engineering-log 的 §D 判定為準。
- **試驗次數**：`research_ledger.count_research_trials` 依 `STRATEGY_FAMILIES` 合併計數，本策略的回測會計入 trend_breakout 家族，
  ledger notes 寫 `family=trend_breakout`。trend_rider 的 700/800/900 高原屬 trend_rider 家族，**不計入**。
- **lockbox**：trend_breakout 家族 2018～2026 全窗已在 2026-06-24 被基準看過，**這個家族沒有未開封資料**。
  禁止對 `breakout_shadow_filter` 呼叫 `set_data_partition_policy` 或 `record_lockbox_opening`（新 id 會被當成新家族，等於繞過）。
  本研究全部是樣本內證據。
- **不做參數掃描**：沒有可掃的連續門檻；掃 N 就是 fishing。

### §E 影子 forward 驗證（預先登錄條件；本次不執行）

本家族沒有未開封資料，影子 forward 才是真正的檢驗。只有 §D 第一列成立、且使用者明確同意時，才把本策略加入
simulation-main 的 `entry_strategies`。這會改到 cron 實際執行的設定，所以必須由使用者決定，並先在 live 環境驗證 YAML 載入正常。

| 項目 | 規格 |
|---|---|
| 對照 | 同帳號（simulation-main）同期的 trend_breakout |
| 帳號限制差異 | simulation-main 每日 6 檔、不限持倉、動態部位，和回測（2/5/2 萬）不同。影子只比**逐筆淨報酬率**，不比帳戶報酬 |
| 期間 | 雙方各累積 ≥ 60 筆已平倉，且 ≥ 6 個月，取較晚者 |
| 通過 | 本策略逐筆平均淨報酬率 > trend_breakout，且本策略逐筆平均 > 0 |
| 中止 | 本策略累計 30 筆後逐筆平均 < −1%，立即撤下 |

## 已知風險 / 限制

- **鎖漲停放行偏誤**：漲停收在最高時 D 的上影線為 0，強勢漲停突破天生傾向通過濾網。隔日鎖漲停無法成交，這點和基準相同。
- **跳空開高走低**：跳空後開高走低收紅不會產生上影線，但「開高走低」本身就是賣壓，本濾網看不到。
- **訊號層模擬的近似**（相對完整回測）：
  - 每筆獨立、不受現金與容量限制。
  - 停牌日不評估出場，完整回測會用 stale close 評估。
  - 窗末未平倉以收盤設算。
- **live 側連帶影響**：本策略已登錄於 `PARAMS_MODELS`，live run-daily 的 `load_exit_managed_definitions` 會載入它的 YAML。
  因為沒有這個 id 的持倉，不會出場任何東西；但 YAML 一旦寫壞，會讓 15:10 cron 載入失敗。已有測試會載入此 YAML。

## 前置條件（凍結前必須成立，已以測試保證）

1. 等價性：新策略訊號＝trend_breakout 訊號減去 {U > L}，逐日逐檔相等（`test_breakout_shadow_filter_strategy.py`）。
2. 進場參數（除 `shadow_window_days`）與 `exit:` 區塊和 trend_breakout dict 相等（同上）。
3. 訊號層出場判斷與 `RiskExitEngine.explain_exit` 在 200 條隨機路徑上逐日相同（`test_shadow_filter_study.py`）。
4. 回測前：`register_regime_gates.py` 已寫入本策略 gate；approval manifest 限額與 trend_breakout 相同。

## 附錄：對抗式審查修訂對照

| 審查項 | 處理 |
|---|---|
| B1 排序其實是代號序 | 照實揭露；兩邊都不修；另開任務 |
| B2 濾網與替補效應混淆 | 主檢定改為訊號層級（§B） |
| B3 剔除率量不出來 | 改用候選集合定義，由 study 腳本計算 |
| B4 點估計比大小、無效濾網易通過 | P1 改為差異的 cluster CI；P2 加安慰劑置換；刪除成本占比門檻 |
| M1 欄位模糊 | §C 對照欄位逐一指定 |
| M2 除權息「不受影響」錯誤 | 寫死 raw，改寫說明 |
| M3 「無參數」是包裝 | 寫出等價式與選擇清單 |
| M4 新 id 繞過治理 | 依家族計數試驗次數（程式）、gate 已登錄、禁止 lockbox 呼叫 |
| M5 A 過 B 不過時結論矛盾 | §D 判定表 |
| M6 影子驗證無條件 | §E |
| M7 基準重跑未凍結 | §C 凍結條件與停止規則 |
| M8 「逐字相同」只靠文字 | 前置條件測試 |
| m1–m7 | 預先登錄聲明、stale bar、出場原因分布、鎖漲停偏誤、樣本內偏誤措辭、live 載入、MDE |
