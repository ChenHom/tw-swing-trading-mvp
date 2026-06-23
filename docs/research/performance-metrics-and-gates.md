# 績效指標與裁決門檻：用什麼衡量、為什麼這樣衡量

> 本文整理專案「研究量尺」的設計理路：哪些指標只是**資訊**、哪些才是**裁決門檻(gate)**、
> 以及**為什麼比率(Sharpe/Sortino/Calmar)不被當 gate**。程式碼對照：
> 指標計算在 [`src/application/runners/backtest.py`](../../src/application/runners/backtest.py)
> `_calculate_statistics` / `_calculate_robustness_stats`；裁決在
> [`src/application/runners/verdict.py`](../../src/application/runners/verdict.py) `evaluate_verdict`。

---

## 0. 先釐清兩個容易混淆的「三」

| | 是什麼 | 內容 | 程式位置 |
|---|---|---|---|
| **三個比率** | 報酬 ÷ 風險 | Sharpe / Sortino / Calmar | backtest.py:626-628 |
| **三種回撤** | 同一件事(跌多深)的三種量法 | `close_to_close_maxdd` / `worst_case_intraday_drawdown_bound` / `timestamped_intraday_maxdd` | backtest.py:621-623 |

兩者只在一點交會：**Calmar 的分母**用了三種回撤裡的 `close_to_close_maxdd`
（Calmar = CAGR ÷ close_to_close_maxdd, backtest.py:604-605）。而**裁決 gate 直接用這個
raw 回撤值卡關，不是用 Calmar 比率**。

### 三種回撤為何要分開（不可互換）
- `close_to_close_maxdd`：收盤對收盤峰谷，**可精確重現**，是 gate 與 Calmar 用的版本。
- `worst_case_intraday_drawdown_bound`：各持倉當日 low 相加的**日線推估上界**，
  **不得宣稱為實際盤中**（這些低點未必同時發生）。
- `timestamped_intraday_maxdd`：**有分鐘資料才能算**的真實盤中回撤（目前無分鐘資料）。

---

## 1. 三個比率：定義、專案怎麼用

| 比率 | 公式（專案實作，rf=0） | 量什麼 |
|---|---|---|
| **Sharpe** | 年化報酬 ÷ 年化**總**波動 | 每單位總波動的報酬 |
| **Sortino** | 年化報酬 ÷ **下檔**波動（只罰 `min(r,0)²`） | 每單位**下檔**風險的報酬 |
| **Calmar** | CAGR ÷ `close_to_close_maxdd` | 每單位**最大回撤**的報酬 |

**專案的用法：三個都算、都輸出在報告，但只當「描述性資訊」，不當通關門檻。**
唯一例外是 diagnostic 淘汰器的 `sharpe<=0 → 明顯爛、可淘汰`（verdict.py:67-69），
那只是「明顯在虧就丟」的粗篩，不是正式門檻。

> **Sortino 為何適合趨勢/波段策略**：趨勢策略報酬不對稱（少數大賺、平常小賠）。
> Sharpe 連**上行**波動都當風險罰（不公平）；Sortino 只罰下檔，更貼合趨勢策略的本質。
> 研發排序時 Sortino 優於 Sharpe。

---

## 2. 真正的裁決門檻（gate）

[verdict.py:91-100](../../src/application/runners/verdict.py#L91-L100) 的 `regime_gate` 看的是**這四個，不是比率**：

| gate 指標 | 門檻 | 程式 |
|---|---|---|
| raw 最大回撤 `close_to_close_maxdd` | ≤ `max_regime_drawdown` | verdict.py:91 |
| 逐筆期望值 bootstrap **CI 下界** | ≥ `min_expectancy_ci_lower` | verdict.py:93-95 |
| **有效樣本數** effective_sample_size | ≥ `min_effective_sample_size` | verdict.py:96 |
| 獲利 **Herfindahl 集中度** HHI | ≤ `max_profit_concentration` | verdict.py:98-100 |

（門檻值須**看結果前寫死**於 `regime_gate_thresholds` 表，事後依結果回填等於沒 gate。
另有 `max_bear_underperformance` 先寫死但需 regime 標記才能評估，暫不參與 Phase 1 裁決。）

---

## 3. 上線 bar：完整檢查清單

比率(Sortino/Calmar)當「描述」沒問題，但「具備實戰價值」的**充分條件**是下面 7 條全過——
缺一條，漂亮的比率都可能是假的：

| # | 條件 | 防的是什麼 | 對應機制/位置 |
|---|---|---|---|
| ① | 報酬算在**成本後**（modeled_executable_return） | edge 被費稅滑價吃光（本專案實測 ~30%） | P1-T4 報酬分層、cost_ratio |
| ② | 回測窗**含一次真實空頭**（如 2022） | Calmar/MaxDD 窗沒踩崩盤就灌水 | verdict.py:77-79 硬性檢查 |
| ③ | **有效樣本數足** + 逐筆**期望值 CI 下界 > 0** | 運氣、樣本不足、雜訊冒充顯著 | gate ②③ |
| ④ | 獲利**不集中**少數幾筆（HHI / 去最佳5筆） | 脆弱、靠少數運氣 | gate ④、robustness |
| ⑤ | **無偏誤污染**（PIT universe，非後見之明池） | survivorship / 選股後見之明 | 待 R-T4b（資料層） |
| ⑥ | **OOS / walk-forward** 過（非 in-sample 調出來的） | 過擬合 | lockbox、參數高原 |
| ⑦ | （理想）**回撤持續時間**可接受 | 水下太久、資金卡死、撐不住 | 尚未實作（見 §7） |

> 口訣：**①成本後 ②含空頭 ③樣本顯著 ④不集中 ⑤無偏誤 ⑥OOS ⑦不水太久**。
> 前四條 gate 已實作、②已硬擋；⑤⑥在研究流程、⑦待補。

---

## 4. 為什麼比率不當 gate

一個好的 gate 要：**難造假、有絕對意義、抗過擬合、看結果前能寫死**。比率每條都踩雷，
病根只有兩個字：**壓縮**——把報酬與風險用乘除壓成一個數。壓縮造成兩個致命後果：

### 病根 A：比率是「點估計」（一個數，沒有誤差範圍）
Calmar=2.0 看不出是 2.0±0.1（紮實）還是 2.0±3（雜訊）。

### 病根 B：比率「可互相抵銷」（高報酬能買回高風險）
Calmar = 報酬÷回撤 → 報酬夠大就能把危險的回撤除掉、藏起來。

### 其餘衍生問題
- **看不出樣本夠不夠**：Calmar=2.0 來自 3 筆幸運或 300 筆，比率長一樣。
- **對極端值/窗極度敏感**：MaxDD 是單一最差點，窗沒踩到崩盤就爆高（假的）。
- **死值難 justify**：「Calmar 要 >1.0 還 >2.0?」隨市場/策略變，講不出理由。
- **用比率當 gate = 鼓勵朝它調參 = 過擬合溫床**。
- **比率是「相對排序」，gate 要回答「絕對活不活得下來」**。

---

## 5. 案例評析 + 真實數字示範

### 5a. 評那句流行說法：「研發看 Sortino、上線死盯 Calmar，兩個過就有實戰價值」

**對的地方** ✅：① Sortino 適合趨勢策略研發（只罰下檔、不罰上行）；② Calmar 管資金/上線
方向對（最大回撤決定能押多大、會不會斷頭/認賠）；③ 分工合理。

**危險的地方** ⚠️：「兩個過就有實戰價值」是**致命的過度簡化**——兩者都是比率，高值可能是
運氣或偏誤。最有力的反例就在本專案：

> **trend_rider 在 Sortino/Calmar 上漂亮過關**（總報酬 +121.9%、Sharpe 1.20、maxDD 僅 10.5%、
> Calmar ≈ 0.9），照「死盯 Calmar」它**會通過**。但它的 +122% 跑在**手挑已知贏家的 diagnostic 池**
> 上、是**後見之明污染**的數字（HHI 0.073 最高、去最佳5筆只留 22%）。**比率全過，edge 卻未證實。**

所以那句話漏掉的，正是決定實戰價值的東西：**含空頭窗（否則 Calmar 灌水）、成本後、樣本/期望值
顯著、不集中、無偏誤、OOS**——也就是 §3 的 7 條。比率是「最後一哩的描述」，不是「過了就行的及格線」。

### 5b. 真實數字：每個 gate 怎麼抓到比率漏掉的東西

（2018-2026, diagnostic universe；對照 0050 buy-hold +34.6%、等權 universe +510%）

| | trend_breakout | pullback_rebound | trend_rider |
|---|---|---|---|
| Sharpe / Sortino / Calmar | 0.49 / 0.73 / 0.24 | 0.82 / 1.19 / 0.49 | 1.20 / 高 / ≈0.9 |
| 總報酬 / maxDD | +32.9% / 14.6% | +44.2% / 9.4% | +121.9% / 10.5% |
| **去最佳5筆後留** | **9%**（90%靠5筆）| 71% | 22% |
| **HHI 集中度** | 0.047 | 0.019 | 0.073 |
| **成本占毛利** | 34.3% | 29.2% | 5.6% |

**讀法**：
- **trend_breakout**：比率看起來「普通但不算爛」→ 若只看比率可能放它過。但**去最佳5筆只留 9%
  （90% 獲利靠 5 筆運氣）**——這是 **HHI / 去最佳5筆 / 期望值CI** 抓到的，**比率完全看不到**。
- **trend_rider**：比率最漂亮 → 但 HHI 最高 + 後見之明池 → **PIT universe（§6 第二層）才擋得住**，
  gate 統計層擋不住。
- **成本欄**：三支毛報酬比率都不錯，但 trend_breakout/pullback **~30% 毛利被費稅吃掉**——
  上線 bar 第①條（成本後）才看得出，比率（用毛報酬算）會誤導。

---

## 6. gate 怎麼來的（出處）+ 為什麼能補比率

### 出處：三個既有領域的組裝（非專案發明）
1. **經典回撤風控**（實務）：「最大回撤 ≤ X%」是 CTA/避險基金最老的風險限額。
2. **統計學重抽樣**：bootstrap（Efron 1979）給信賴區間；有效樣本數（design effect / 聚類）。
3. **抗「回測過擬合」金融文獻**（靈魂）：
   - **Bailey & López de Prado** — Deflated Sharpe Ratio、《The Probability of Backtest
     Overfitting》、《Pseudo-Mathematics and Financial Charlatanism》。核心：你的回測指標被
     多重檢定灌水，要去膨脹。（`_deflated_sharpe_ratio` docstring 直接掛此名。）
   - **Harvey, Liu & Zhu** — 金融號稱的數百個「因子」多是 false discovery。

→ 比率是 1960s 的工具；這套 gate 是 2010s「發現回測會系統性騙人」之後的修正工具，
**本來就是為補比率不足而生**。

### 機制：每個 gate 對應比率的哪個盲點

核心差異——**比率讓維度互相抵銷後壓成一個數；gate 把維度拆開、各設不可抵銷的絕對門檻、AND 串起來**：

```
比率：  (報酬, 風險, 樣本, 集中度) ──壓成→ 一個數      ← 維度可互相抵銷、補償
gate ： 報酬期望顯著? AND 回撤可活? AND 樣本夠? AND 不集中?  ← 全部要過、不能互補
```

| gate | 補比率哪個盲點 | 機制 |
|---|---|---|
| raw 回撤（不除以報酬） | Calmar 讓報酬把回撤買回去 | 只看分母——生存是絕對約束，不准用報酬抵銷 |
| 期望值 CI 下界 | 比率是點估計、不分顯著 vs 雜訊 | **重抽樣幾千次**得期望值分布，取第 5 百分位：「連悲觀 5% 都還正嗎」 |
| 有效樣本數 | 比率藏住證據量 | 把同日相關交易塌縮成獨立單位，量真正獨立證據 |
| HHI 集中度 | 比率對少數極端值脆弱 | 把總報酬**拆回各筆貢獻**算平方和，直接量「靠不靠少數幾筆」 |

> DSR（去膨脹 Sharpe，`_deflated_sharpe_ratio`）是另一條：按「試了幾次（num_trials，
> 接 Research Ledger）」把 Sharpe 打折，回答「扣掉多重檢定後還顯著嗎」。

---

## 7. 已知缺口與限制（這套量尺還沒覆蓋的）

別把這套神化。誠實列出目前還沒做/有假設的地方：

- **回撤持續時間（recovery / time-underwater）未量**：Calmar 與 raw 回撤都只看「跌多深」，
  不看「水下多久」。「深但兩週回來」vs「淺但水下 18 個月」對實戰（資金卡死、撐不撐得住）
  天差地別——目前沒指標。上線 bar 第⑦條因此仍是 TODO。
- **DSR 的 num_trials 偏樂觀**：接了 Research Ledger，但 ledger 只有系統內試驗，**低估了人類
  過去手動試過幾版** → DSR 算出來偏高（偏寬鬆）。
- **rf=0 假設**：Sharpe/Sortino 沒扣無風險利率。台股低利環境影響小，但技術上這是「資訊比率」
  而非嚴格 Sharpe。
- **滑價固定 bps、未換流動性**：成交模型用固定 10/30/50bps 壓測，沒跟單量/跳空/零股流動性
  真正掛鉤；正式校準要等 Phase 4 影子用真實 fill。所以 `modeled_executable_return` 仍是模型估計。
- **有效樣本數是保守啟發式**：只按「進場日」聚類（同日視為一個），非精確 design-effect / ρ 估計；
  產業/事件聚類需產業分類資料（FinMind 無），未做。
- **gate 門檻值仍待註冊**：Thesis 寫了提議數值，但 `regime_gate_thresholds` 表尚未寫入真實門檻
  （所以目前所有 run 因「無 gate 數值」或「diagnostic universe」一律 INVALID）。

---

## 8. 誠實的邊界：兩層防線

gate 補的是：**「給定這份資料，結果是不是運氣/過擬合」**。
gate **補不了**：**資料本身有偏誤**（survivorship / 後見之明 / lookahead）。

> trend_rider 的期望值 CI、HHI 全部會過，但它跑在**手挑贏家的 diagnostic 池**上 ——
> gate 看不出，因為 gate 信任餵進去的資料。那一層要靠 **PIT universe**（資料層）擋。

**完整防線是兩層**：
1. **PIT universe（資料層）**：管「資料乾不乾淨」（消除 survivorship/選股偏誤；待 R-T4b）。
2. **gate（統計層）**：管「乾淨資料上是不是真 edge」。

比率連第二層都守不住，所以專案把它降級成純資訊。

---

## 9. 一句話總結

**比率管「研發排序」（哪個策略相對好），gate 管「上線門檻」（這策略絕對上能不能活、是不是真的）。**
各司其職——比率是資訊壓縮、方便快速比較；gate 是抗自欺的絕對門檻、把被壓縮的維度逐一拉出來檢查。

### 延伸閱讀
- 大計畫與裁決語意（S1-S5）：`~/.claude/plans/2455-cosmic-fountain.md`
- 研究回測工作流：[README.md](../../README.md) §8
- 施工記錄（2026-06-23 誠實回測實驗室）：[engineering-log.md](../development/engineering-log.md)
