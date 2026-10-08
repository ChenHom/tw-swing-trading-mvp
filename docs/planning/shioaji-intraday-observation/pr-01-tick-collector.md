# PR-1 — Tick 即時行情 Collector / Raw / Replay

狀態：DRAFT（D1 / D2 / D3 已確認；其餘全域決策仍待完成）
依賴：僅依賴既有 repo；PR-2 / PR-3 / PR-4 的前置資料契約
成功意義：**確實知道盤中成交了什麼、什麼時候知道，以及哪段資料不可靠**；不是增加獲利能力的宣稱。

## 問題與範圍

**2026-10-08 已確認設計：**Collector 歸屬波段 repo，複用 lab 純函式邏輯，不跨 repo runtime import；訂閱持倉、手動 watchlist、影子候選股，不掃 top-150；整股與零股分開儲存與計算。

現有 src/market_data/provider.py 的 ShioajiMarketDataProvider 以歷史 fetch_kbars 為主，缺少 streaming 事件收集、持續健康狀態、可重播的盤中證據。當沖 lab 的 market_data.py 已有大部分 pure logic，但它的 runtime / command 專屬 lab；先逐項測試後複用算法與 fixtures，不直接 import 另一個 repo 的 src package。

輸入：已存在之持倉、明確 watchlist、simulation-main 候選（由當日已知資料生成）；不能在每次 callback 動態掃 top-150。
輸出：raw append-only 事件、normalized MarketTick、健康指標、1m 聚合（可使用 lab bars.py 的規則）、replay 命令或純函式。
不包含：觀察策略、五檔、任何買賣或告警、將 Tick 寫回盤後 market_bars 作為權威日資料。

## 建議實作切面

- src/market_data/intraday_contracts.py：RawEnvelope、MarketTickV1、CollectorHealth、訂閱標的識別（symbol + exchange + lot_type）。資料類型驗證、事件時點與收到時點分離。
- src/market_data/shioaji_stream.py：Shioaji SDK adapter，唯一擁有 read-only login 與 subscribe/unsubscribe 的邊界；注入 fake SDK；開啟前檢查不載入 CA、沒有交易路徑。
- src/market_data/tick_collector.py：bounded callback queue、worker、raw writer、metrics、session id、retry/backoff、graceful shutdown、watchlist 更新與重新訂閱。
- src/market_data/tick_replay.py：raw 重播 / dedupe / 1m 聚合可確定性輸出；先盤點 lab 的 market_data.py、bars.py、shioaji_compat.py，避免重複造輪子。
- src/cli/market.py：只新增獨立明確的 market intraday-tick-collect / replay 命令，不改既有 backfill 或日常 run。
- tests/unit/test_intraday_* / tests/integration/test_intraday_*：假 SDK + 固定 fixture；此為建議檔名，不是已存在檔案。

先在隔離 venv 確認 Shioaji 1.7.x 的 login/contract/subscribe/stream 實際相容性，**不要無測試直接提升全 repo 的 shioaji>=1.0.0 到浮動最新版**。既有 provider 依賴須保持原本定時同步可運作。

## Canonical Tick V1（提案）

必填：schema_version, symbol, exchange, quote_type=tick, lot_type=BOARD|ODD, event_time (Asia/Taipei offset-aware), received_at (UTC offset-aware), price_x10000 (int), trade_volume_shares (int), session_id, source, collection_seq。
附加/可為 null：provider sequence、累計量、tick_type、原始 SDK 欄位、duplicate_key、quality_flags。
Raw 不得省略原始 volume、simtrade、suspend、intraday_odd、provider datetime；必要時 raw 另存原始欄位與接收 metadata，容許未來修正 normalizer。買量與賣量不是 Tick 的必然欄位，缺乏可靠方向時為 UNKNOWN。

整股與零股成交量從官方實際 payload 驗證單位後才轉成股；lab 既有整股 *1000 不能不分條件套到零股。價格沿用 repo x10000 並檢查 Decimal 精度。事件時間相等不代表事件重複，dedupe 規則必須使用 provider 提供的可靠特徵與本地 session，不可直接用 (symbol, timestamp) 刪除。單次 SDK callback enqueue 不作同步磁碟寫入。

## 落地 / 故障處理

- raw: data/raw/shioaji/ticks/YYYY-MM-DD/{BOARD|ODD}/{symbol}.jsonl（明訂 rotation / sync / disk cap / crash 尾行修復；資料不進 Git）。
- health 狀態：DISABLED / CONNECTING / HEALTHY / DEGRADED / FAILED / CLOSED；包含 last_event_at、last_received_at、last_heartbeat_at、queue_depth、dropped_count、gap_count、reconnect_count、scope_count。
- 階段失敗：auth 失敗、訂閱被拒、無 heartbeat、queue overflow、raw 寫入失敗、未知 timestamp/volume、行情時段之外、日界/輪盤中重連。
- 交易時間內 stale 判斷須基於 heartbeat/連線狀態與個股成交特性，不可將低流動性股票「未成交」直接認成 feed 斷線。沒有成交不製造零量 1m bar；盤後正常無行情不是 DEGRADED。
- fake event replay 要保留是否錯亂、缺洞與 replay cursor，否則無法證明資料品質。

## 測試 / DoD

1. 同時訂閱上市/上櫃兩檔的 fake SDK；原始與正規化資料之時間、價格、數量、market / lot 分別正確。
2. 模擬重複、同 timestamp 不同成交、晚到、亂序、corrupt event、simtrade、suspend；不錯刪成交，不把試撮作真成交。
3. queue 滿、SDK 斷線重登、重新訂閱、重啟寫入、磁碟權限與空間不足，都可觀察 DEGRADED / FAILED 與缺口。
4. 以固定 session replay raw，normalized Tick 與 1m 產物相同（版本與 ordering policy 固定）。
5. 假日與收盤後不連線、不冒出已成交或買進警示。
6. 安全掃描證明無 place_order / cancel_order / CA，原 DailySimulationRunner 與市場回補測試未回歸。
7. 交易日上午現場只讀 smoke 需人工允許，並附環境版本、起訖時間、訂閱結果及健康報告；無實測前標 NOT_VERIFIED。

## Rollback

預設 feature flag OFF；停 collector / 移除獨立排程即可回復，raw 可留存供分析。不得因資料不可用阻擋原本盤後資料匯入與每日影子流程。
