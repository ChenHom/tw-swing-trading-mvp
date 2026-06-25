# 系統說明:這套價格型態波段交易系統做了什麼

> 目的:把「資料 → 篩選 → BUY/SELL 建議 → 成交 → 驗證成效」整條鏈講清楚,對得上實際程式碼,
> 讓使用者能確認「我們到底在驗證什麼」。
>
> 範圍:現役 = 純價格型態波段(無籌碼/基本面/LLM)。

## 一句話

每天收盤後跑一次,用**純價格與成交量**的規則掃股票,吐出「明天要下的 BUY/SELL 計畫」;
影子帳號機器自動成交、真實帳號(國泰)人工成交回填;成交後用 FIFO 已實現損益記成效。

## 最重要的心智模型:Layer A vs Layer B

「驗證有沒有賺」其實是**兩個不同的問題**,別混為一談:

| | Layer A — 帳房 | Layer B — 證明 edge |
|---|---|---|
| 問的是 | 這些交易**帳面**賺賠多少 | 這策略**到底有沒有真 edge、會不會持續賺** |
| 怎麼算 | FIFO 已實現損益 + 費稅(精確) | 回測 PIT 裁決 + 夠長 forward 樣本(統計) |
| 狀態 | **早做完、精確、沒爭議** | **真正難的、可能花幾年甚至證不出來** |
| 快慢 | 即時 | 慢(時間 ≈ (2/Sharpe)² 年) |

**系統(① ② ③ 的 Layer A)沒壞、也不需要再蓋;卡住的是 Layer B,而 Layer B 不是多寫程式能解的——
要嘛策略有 edge、要嘛沒有。** 專案至今所有關於「賺不賺、要驗多久、有沒有意義」的討論,全是 Layer B。

---

## 每天一次 `run-daily`:4 個 Stage

骨架在 [run_daily](../src/application/runners/simulation.py#L140),由 cron 每日觸發(國泰 15:10、simulation-main 15:12):

| Stage | 做什麼 | 程式 |
|---|---|---|
| **1. 市場同步** | 從 Shioaji API 抓**今天**的日 K(個股+大盤指數),寫進 `market_bars` | `sync_market_data` |
| **2. 執行昨天的訊號** | 把昨天算好、目標今天執行的 BUY/SELL 成交(影子自動;國泰跳過、等手動) | [execute_bundles](../src/application/execution/engine.py#L157) |
| 2.5 更新高水位 | 記錄持倉移動停利的高點(出場用) | `update_high_watermarks` |
| **3. 產生今天的訊號** | 收盤後跑策略 → BUY/SELL 建議,**目標執行日 = 下一交易日** | Stage 3a/3b/3c |
| **4. 報告** | 標記完成,日報/儀表板可讀 | `report daily` / web |

### 關鍵時序(PIT 防未來函數)
訊號在 **D 日收盤後**算出,`target_execution_date = D+1`(下個交易日)。
→ **今天篩出來的,是「明天才執行」的計畫單。** 收盤才看得到完整資料,不偷看未來。

---

## ① 訊號怎麼生:BUY 三濾網 + SELL 四層(Stage 3)

兩條獨立的線,各自跑、各自打包成 `signal_bundle` 存 DB([_save_bundle](../src/application/runners/simulation.py#L289))。

### BUY(進場)— [trend_breakout.py](../src/strategy/trend_breakout.py)

**先看大盤(總開關)**:大盤指數今天收盤 **> 60 日均線**?否 → **整天不產生任何 BUY**(空手避崩盤,防守來源)。

大盤 OK,才逐檔掃 universe(已持有的跳過,不加碼)。每檔要**同時**滿足三條:

| 濾網 | 條件 | 白話 |
|---|---|---|
| ① 突破 | `latest_close > 前20日最高收盤` | 創 20 日新高 |
| ② 帶量 | `今日量 > 1.5 × 前20日均量` | 量能放大 1.5 倍(防假突破) |
| ③ 個股趨勢 | `latest_close > 60日均線` | 個股本身在上升趨勢 |

三條全中 → 一張 **BUY**(`reference_price`=今天收盤,理由碼 `TREND_BREAKOUT_ENTRY`)。
**純價格與成交量,沒有籌碼、基本面、LLM。**

### SELL(出場)— [risk_exit.py](../src/strategy/risk_exit.py)

跟進場完全分開。對**每個現有持倉**看四層,**誰先觸發就 SELL**:

| 層 | 觸發 |
|---|---|
| 固定停損 | 跌破成本 X%(`fixed_stop_loss_bps`) |
| 移動停利 | 從持有期高點回落 X%(`trailing_stop_bps`) |
| 均線跌破 | 跌破 N 日均線、連續確認 |
| 時間停損 | 持有超過 N 天 |

**SELL 永不被任何閘擋(S5 鐵律,保命優先)。**

> bundle 歸屬:BUY(entry)bundle 全域共用(突破=市場事實);SELL(exit)bundle 各帳號私有
> (成本/高水位是帳號專屬)。執行邊界另有 per-account 進場閘,只讓本帳號 pipeline 內的策略進場。

---

## ② 建議怎麼變成成交:影子自動 vs 國泰手動(Stage 2)

由 `auto_execute` 旗標決定,這是兩帳號的根本差異。

### 影子 simulation-main（`auto_execute=True`)— 機器全自動

**1. 規劃股數/金額** — [MultiStrategyAllocator.plan](../src/trading/allocator.py#L52)

- **SELL 股數** = 該策略該股的**全部持倉**(全砍,不留尾)。
- **BUY 股數**,三步:
  1. **可動用預算 = 五個上限取最小** `min()`(allocator.py:173):
     | 上限 | 來源 | 現值 |
     |---|---|---|
     | 每筆預算 `order_budget_twd` | 策略 yaml | trend_breakout = **20000 元** |
     | 授權單筆上限 `max_order_value` | manifest | |
     | 策略當日剩餘買額 | `max_daily_buy_value − 已花` | |
     | 帳號當日剩餘買額 | `GlobalLimits.max_daily_buy_value − 已花` | 200000 |
     | 現金(T+1:當日賣出收益不算) | projection | |
  2. **股數 = `預算 // 參考價`**(無條件捨去到「股」),再**一股股縮量**直到「金額 + 手續費 ≤ 現金」。
     手續費 = `max(20, 金額 × 0.1425%)`。
  3. **規劃金額 = 股數 × 參考價(D 收盤)** —— 這是估計值;真正扣的錢用 D+1 開盤成交價算。
- **拆單**([_split_lot_orders](../src/trading/allocator.py#L28)):≥1000 股 → 「整張 + 零股」;<1000 → 純零股。
- **一排 BUY 閘**(任一不過 → BLOCKED 記原因):同日同股有賣單→抑制買、授權存在、策略/帳號日限額、
  no-add(已持有)、策略持倉檔數上限、帳號持倉上限 8 檔、每日新建倉上限 2 檔、現金。
- ⚠️ 每筆預算才 **20000 元 = 很小的部位**(一檔常常只有幾股到幾十股),刻意的小注。

**2. 成交價** — [FakeBroker](../src/broker/fake_broker.py#L50)
```
fill_price = D+1 當天「開盤價」bar.open × (1 ± 滑價)
```
- 用**執行日(D+1)的開盤價,不是訊號日(D)的收盤價**。BUY 加價、SELL 減價(保守)。
- 滑價預設 `slippage_bps=10` = 0.1%;**零股 ×3 = 0.3%**(流動性薄)。
- 零成交量 / 漲停鎖死(BUY)/ 跌停鎖死(SELL)→ UNFILLED 順延。
- ⚠️ 訊號參考價是 D 收盤,實際成交是 D+1 開盤 ± 滑價 → 中間隔夜有**跳空風險**(刻意模擬真實)。

**3. 落帳** — [apply_fill_transaction](../src/application/execution/engine.py#L202):每筆 fill 寫
`position_lots`(持倉)、`cash_ledger`(現金流)、`fifo_matches`(FIFO 配對 → 已實現損益)。

### 真實國泰（`auto_execute=False`)— 機器出計畫、人成交
- Stage 2 **整個跳過**,不建 broker、不自動成交。
- 只在 Stage 3c 把「明天該下的單」寫進 `order_intents`(計畫),儀表板「下次執行」顯示。
- **看儀表板 → 自己去券商手動下單 → 回來 `record-fill` 回填實際成交價/量** → 同樣走
  `apply_fill_transaction` 落帳。
- 全手動、不碰券商 API(設計如此,故無券商對帳)。

> **一句話對比**:影子=機器自動成交、用**假設價**(D+1 開盤);國泰=機器只給計畫、你手動成交回填**真實價**。
> 兩者落帳路徑相同,差別只在「誰成交、用什麼價」。

---

## ③ 成交後怎麼驗證有沒有賺

### Layer A:帳房——「到目前為止實際賺賠多少」(精確、可信)

成交落帳時算的真相,在 [apply_fill_transaction](../src/portfolio/projection.py#L364) 的 SELL 分支:

**每次 SELL → FIFO 配對**(同策略的買進 lot,先進先出):
```
單筆已實現損益(毛) = (賣價 − 買價) × 配對股數 ÷ 10000     # projection.py:402
```
- 這是**毛損益**(只算買賣價差),寫進 `fifo_matches.realized_pnl`。
- **費 + 稅另記**:手續費 0.1425%(買賣各一次,最低 20 元)、證交稅 0.3%(只賣出收)。
- 現金實扣:賣出收回 = `成交額 − 手續費 − 稅`(projection.py:436)。

**完整算式**:
```
淨已實現損益 = Σ毛損益(FIFO) − 買手續費 − 賣手續費 − 賣出證交稅
總報酬       = (現金 + 未平倉持倉市值) − 淨投入本金
```

> 關鍵數字:**成本占毛損益的比例**。trend_breakout 在 PIT 上是 **68.7%**——毛賺 100 元,費稅吃掉近 69 元。
> 這就是「薄 edge」的具體長相。

**在哪讀 Layer A**:
- **日報**:每天落 `artifacts/reports/daily/{帳號}_{日期}.txt`([daily_report.py](../src/application/reporting/daily_report.py))。
- **Web 儀表板**:資金總覽**總報酬率** + 策略別「毛損益/規費/淨損益」表。
- **reconcile**:內部帳冊一致性(現金流水 vs 投影對得起來),抓記錄錯誤。

### Layer B:證明 edge——「這策略到底會不會賺」(慢、統計,Layer A 答不了)

**最容易誤會的地方**:Layer A 告訴你「這些交易帳面賺賠多少」,但**它證明不了策略有沒有 edge**。

- forward 的已實現損益是**真的**,但**樣本太少 = 雜訊**。看 forward 短期賺或賠,讀不出任何結論。
- 真正「證明賺錢」的機制是**回測 PIT 裁決**(歷史、幾分鐘):決勝 gate = 逐筆期望值 bootstrap CI 下界 ≥ 0。
  這才是把「運氣 vs edge」分開的尺。詳見 [verdict.py](../src/application/runners/verdict.py)、engineering-log 2026-06-24。
- **時間 ≈ (2 / 年化Sharpe)²**:trend_breakout PIT Sharpe ≈ 0.1 → 要證明賺錢需數十至上百年 = 實務上證不出來
  (edge 太薄,淹沒在雜訊裡)。要把「3-4 年證明」變可能,前提是真實 Sharpe ≈ 1.0(更強 edge × 更多獨立下注)。
- **誠實缺口**:目前**沒有** forward 的滾動期望值 / 累積報酬曲線讀數——只有當下快照。

---

## 整條鏈接起來

```
① API 抓日K → 純價格三濾網 → BUY/SELL 建議(D 收盤算、D+1 執行)
② 影子機器自動成交(D+1開盤±滑價)/ 國泰出計畫、你手動回填 → FIFO 落帳
③ Layer A:FIFO 算淨已實現損益 → 日報/儀表板讀「帳面賺賠」(精確)
   Layer B:「證明有 edge」要靠回測 PIT 裁決 + 夠長 forward(慢、可能證不出)
```

**一句話總結**:這是一台**誠實的帳房(Layer A)+ 誠實的歷史裁決機(Layer B)**。帳房精準記錄每一塊錢;
裁決機用 PIT 判斷 edge 真假。它**不能**做的,是讓你在幾週內從 forward 損益看出賺不賺——那是物理限制,不是缺陷。

---

## 兩帳號角色對照

| | 國泰(真實) | simulation-main(影子) |
|---|---|---|
| 成交 | 人工(plan-only,看計畫手動下單回填) | 機器自動(FakeBroker) |
| 進場策略 | 只 `trend_breakout`(pullback 已 PIT REJECTED 退役) | 全域清單(trend_breakout + pullback) |
| 成交價 | 真實(record-fill 回填) | 假設(D+1 開盤+滑價) |
| 角色 | 真錢、保守 | forward 觀察、累積證據 |
| cron | 15:10 | 15:12 |
