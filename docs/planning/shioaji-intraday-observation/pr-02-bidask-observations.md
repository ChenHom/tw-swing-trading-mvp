# PR-2 — BidAsk 五檔與 ObservationEvent

狀態：DRAFT（以 PR-1 資料契約已驗收為前提；D3 待確認）
目的：**觀察價位附近的掛單、成交量與流動性，而非從委買量直接預測價格。**

## 工程邊界

在 PR-1 的同一 Collector 中增設 BidAsk subscribe 與 raw writer，不建立第二套身份認證/下單服務。股票整股與盤中零股訂閱必須分流；同一 symbol 在 BOARD / ODD 可有獨立委託簿。權威來源是 event_time 當下最接近的有效五檔快照，**五檔只有可見五層，不代表完整委託簿或真實主力意圖**。

新增建議檔案：src/market_data/orderbook_contracts.py（MarketBookV1）、src/market_data/orderbook.py（snapshot / metrics）、src/market_data/observations.py（純函式事件判斷）、src/market_data/observation_replay.py；實際位置須以 PR-1 的 repo 版面確認後固定。

raw：data/raw/shioaji/bidask/YYYY-MM-DD/{BOARD|ODD}/{symbol}.jsonl。保持 append-only，加入 exchange, event_time, received_at, session_id, sequence, bid_prices_x10000[0..4], bid_volumes_shares[0..4], ask_prices_x10000[0..4], ask_volumes_shares[0..4], feed flags, quality_flags。缺層使用 null / missing，不以 0 冒充無委託；provider diff_bid_vol/diff_ask_vol 的語意必須先由 SDK fixture 驗證。

## V1 只做四類觀察

1. SPREAD_OBSERVED：ask1-bid1 / tick-size-normalized spread（缺任一側或 cross book 則 UNKNOWN）。
2. BOOK_IMBALANCE_OBSERVED：sum(bid volume) / (sum bid+ask)，明確標示僅五檔可見量；不當買進訊號。
3. SUPPORT_TESTED / SUPPORT_HELD / SUPPORT_BROKEN：依事先固定的支撐區間、回測持續時間、成交 Tick 確認；若沒有預先設定支撐、不能事後挑最低點。
4. BREAKOUT_OBSERVED / BREAKOUT_RETESTED：用前一日已知阻力/訊號價與當下 Tick；需有可重播的成交確認窗口（持續時間/實際成交筆數），避免一次跳價就宣稱「突破成功」。

先不做「假單識別」、「主力洗盤」、「吸收籌碼」、「隔日必漲」等無可觀測標準的解釋。主動買賣方向僅使用 provider 有定義且可信的 tick_type；無法分類則 UNKNOWN，禁止從五檔量比反推成交方向。

## ObservationEvent V1（提案）

event_id（deterministic）、rule_version、symbol、exchange、lot_type、kind、event_time、observed_at、source_session_id、input_range / raw pointer、thresholds、metrics、status=OBSERVED|INCONCLUSIVE|INVALID、reason_codes、data_health。事件基於 event_time 而非機器現在時間；重新 replay 同一批 raw 與規則版本須產出 byte-equivalent 的 canonical 結果。

事件寫入 data/observations/ 的可重播產物；**不可寫入 order_intents、approval、fills、cash_ledger，不可觸發任何買賣**。保留事件到唯一已知候選股 ID / support plan 的 lineage；沒有預先來源的支撐價視為不可分析。

## 異常與錯誤案例

- bid1/ask1 交叉、任一五檔陣列長度異常、0 / 負價格、錯誤 lot、價位不按順序、上一筆資料過舊 → INVALID / INCONCLUSIVE，絕不當成 100% 買盤。
- 掛單數減少不等於撤單：可能成交、修改或價格檔位移動；只報「顯示掛單減少」，不用取消委託的確定措辭。
- 價格檔位移動時，逐價格 key 對照，而非把前後「買一」不同價直接相減。
- 沒 Tick 卻有 BidAsk 更新、Tick 時間落後 BidAsk、零股 quote 稀疏，都記品質狀態。
- feed 中斷後恢復前的事件一律不輸出 CONFIRMED；需要重建最新有效快照、重新滿足觀察窗口。

## 驗收測試

- 5 檔排列、分別的買/賣價量、整股/零股換算與缺層測試。
- 在非連續 tick、超時、掛單變價、撤單疑似、快速大單、同時多股票下，指標可解釋且不誤判成交。
- replay 相同輸入相同 ObservationEvent；版本改動有差異說明與 fixture 更新。
- 所有事件只有唯讀副作用；關閉 feature flag 不影響 PR-1 Tick、原 daily run 與既有風控。
- 盤中真實 smoke 需實際驗證官方 BidAsk 欄位與零股支援；測試不到時列 NOT_VERIFIED，不能假設已可用。
