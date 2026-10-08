# Shioaji 盤中行情四 PR — Cross-PR Code Review

審查日期：2026-10-08；狀態：**OFFLINE CODE REVIEW DONE / NOT READY TO MERGE TO PRODUCTION**。
審查對象：docs PR #1、Tick PR #2、BidAsk PR #3、forward-study PR #4、dashboard PR #5。
審查範圍：架構、安全、PIT、數據正規化、測試、研究稽核、部署/回退及跨 PR 依賴。
**未連券商、未操作正式主機，不能把程式碼審查當成實盤驗證。**

## 已修正並以離線測試覆核

| ID | 發現 | 修正 |
| --- | --- | --- |
| R-01 | 同秒不同成交容易被去重誤刪 | PR-1 raw-first replay 不使用單純 timestamp 去重；既有回歸測試涵蓋 |
| R-02 | 假成交 / 錯誤成交量 / 零股可能混算 | PR-1/2 區分 BOARD/ODD，價格 x10000、量換成股，無有效量或 simtrade 拒納入確認 |
| R-03 | 五檔買價交錯、跨價、缺整側會製造假訊號 | PR-2 拒絕不連續層與 crossed book；缺整側一律顯示不確定，不把 imbalance 當買單 |
| R-04 | 事後建立的價位計畫、跨 session 資料可能產生時間穿越 | PR-2/3 比對 plan 時點/digest、事件發生/收到時間與 collector session；新增負例測試 |
| R-05 | 同一候選改 opportunity_id 重複計數，造成錯誤 60/100/20 門檻通過 | PR-3 依 symbol/exchange/lot/date/plan_id 限制獨立機會；只把有有效 baseline quote 的樣本計入可評估門檻；空行情一律 EVIDENCE_PENDING |
| R-06 | 研究輸出只看成功配對、忘記錯失樣本 | PR-3 仍保留 MISSED/INVALID/INSUFFICIENT_DATA，輸出完整 cohort/exclusions/lineage |
| R-07 | CSV CRLF 讓同一研究結果不能冪等重播 | PR-3 強制 LF 換行，報告 bundle 差異不可被同 experiment_id 默默覆寫 |
| R-08 | Web cache 或未知行情狀態可能顯示看似可交易價格 | PR-4 feature flag 預設 OFF、未驗證心跳/過期/損壞/未開市則隱藏 Tick 和五檔價格，額外檢查報價型別與時間範圍 |
| R-09 | Web 舊功能可能因新行情 SDK import 被干擾 | 新頁面/API 使用唯讀 service / snapshot，無 Shioaji 進入 Web request，無交易 DB 寫入 |

## 仍阻擋合併或正式上線的項目（未假裝修好）

| Severity | 對應 | 問題 | 必須通過的補強 |
| --- | --- | --- | --- |
| BLOCKER | PR-1, PR-2 | 缺少經 SDK 實測的斷線偵測、補訂、資料缺口/重放品質；真實 1.7.x callback/contract/BOARD/ODD 行情配額未在隔離真實帳號驗證 | fake SDK 重連/重複啟動/亂序回歸；獨立**授權後**執行唯讀 smoke；記錄通道與品質證據 |
| BLOCKER | PR-1, PR-2 | D6 180 交易日保存、gzip 每日 rotation、80% 告警 / 90% 專用區停收還沒有自動安全生命週期；raw append flush 非 fsync 強持久承諾，手動壓縮不可與仍在寫入的 collector 並行 | 完成 graceful stop → flush/fsync → checksum/rotation → 壓縮 → 研究保留保護 → 容量監測 → crash recovery 測試 |
| BLOCKER | PR-4 | 只有明確手動執行的離線 snapshot publisher。PR-1 沒有證明真實健康的 heartbeat/trading_session_verified，因此無法建立可持續、可宣稱 OBSERVING 的盤中資料流 | Collector 一個可測試的持續只讀 snapshot publisher，可靠心跳/交易日、快照原子落地，斷線顯示 STALE；真行情實測後另授權每日啟用 |
| BLOCKER | PR-4 | 真實站台 `trading-web.service` 未部署且 /healthz、/trading/、其他既有路由並未實機回歸 | 完成端到端測試並按 AGENTS.md 另批准部署、重啟及 GET 200 驗證 |
| MAJOR | PR-3 | 目前只比較最早可觀察 best ask，**不是完整滑價／成交機率、後續績效、錯失大漲樣本、統計 CI 或 PF/Expectancy/MDD** | 使用真實候選資料線、價格與未成交狀況，補 OOS、bootstrap、交易成本及完整策略研究，不可用現有輸出宣稱獲利改善 |
| MAJOR | PR-3 | 「60 個交易日」目前以提供的日期標籤計算，尚未串接官方 XTAI 行事曆／券商交易 session 證據 | 正式前向樣本需驗證 XTAI 行事曆、來源 PIT、封存時點與獨立機會定義，未滿門檻 EVIDENCE_PENDING |
| MAJOR | ALL | GitHub Actions 執行的是 PR 專用隔離測試，不等於現有全部 pytest、真實 SDK、整套 Web 服務與 cron 都已通過；既有部分 Web suite 明確 skip | 完成原 repo 全套回歸與部署前驗證；CI 不得因單元測試全綠宣稱正式驗收 |
| PROCESS | PR-1→4 | 四個實作 PR 仍是 stacked Draft，部分上游修補在下游以同內容同步，尚未整理最終 branch ancestry | 合併順序 PR-1→PR-2→PR-3→PR-4；每次 upstream merge 後 rebase/刷新子 PR 並重新跑 CI |

## 安全簽核

- 沒有發現新增的券商 place_order/cancel_order 或 CA 載入路徑；任何未來 live smoke 只讀行情且仍需使用者**額外單獨核准**。
- 仍保留真帳號人工下單、現有 REJECTED 策略、不修改 fills/cash_ledger/order_intents。
- 當資料健康為 UNKNOWN、STALE、CLOSED、DEGRADED 時，不得從五檔事件宣稱確定買入機會或真實執行價。
- **Reviewer verdict：四個 PR 的離線實作與專項回歸可供繼續研發，但目前不應標 ready、合併到正式 master 或啟用真實盤中服務。**

## 驗證紀錄

- PR-1/2: synthetic Tick/BidAsk + fake SDK 回歸；
- PR-3: Github Actions 針對 63 項 PR-1～3 離線測試（含研究候選獨立性及報告冪等）；
- PR-4: 18 項 Web/service/API 測試 + 63 項 PR-1～3 回歸；正式主機未執行。
- 官方 SDK 訂閱型別參考：https://sinotrade.github.io/zh/tutor/market_data/streaming/stocks/ ；官方示例與 `api.subscribe` / BidAsk / intraday_odd 方向一致，但此證據不構成實際帳號連線成功。
