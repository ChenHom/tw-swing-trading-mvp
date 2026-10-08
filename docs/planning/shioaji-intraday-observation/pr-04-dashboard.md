# PR-4 — /trading/ 即時觀察唯讀 Dashboard

狀態：DRAFT（D5 已確認；PR-1 / PR-2 事件契約為必要前提）
目的：讓波段交易者看到盤中價格與五檔依據、資料時間和缺口，**不建立另一個下單入口**。

## 既有架構與改動範圍

repo 已有 src/web/server.py（FastAPI / Jinja，/trading/ 根路徑）、src/application/services/dashboard.py（read side），前端模板在 src/web/templates/，static 在 src/web/static/。依現行主站延伸「盤中觀察」頁或頁籤，無須新的 SPA、broker 連線或額外資料庫服務。

建議新增 src/application/services/intraday_dashboard.py（讀取唯讀資料快照與 observations）、對應的 server 路由、templates/intraday.html、static/intraday.js/css，並在導航顯示連結。頁面資料透過 server 取得；不得從瀏覽器直接拿 API Key 或 Shioaji stream。URL 與 API 契約先寫測試，維持 root_path=/trading 的所有連結正常。

## 已確認：提醒與展示邊界（D5；2026-10-08）

V1 僅在既有 /trading/ Web 顯示 Tick、五檔、觀察事件與 Collector 資料品質；**不新增 Discord / Telegram 盤中逐筆通知**。原有 cron 故障告警保持不變。畫面不得把觀察事件當成經核准的正式買進訊號。

## 第一版介面內容

- 每檔：股票代號/名稱、BOARD / ODD、最新成交價、價格事件時間、接收時間、最新 Tick 狀態、當日量與 1m 觀察（須明示整股與零股單位）。
- 五檔：bid/ask 各五筆價量、spread、可見五檔 imbalance，無資料與過期一律顯示「— / 資料過期」；不得把五檔失衡稱為「主力買超」。
- 事件：支撐測試、突破、回測、無結論、資料品質問題；展示 rule_version、trigger price、引用的原始資料與發生時點。
- Health：Collector CONNECTING / HEALTHY / DEGRADED / FAILED / CLOSED，最後行情時間、最近 heartbeat、落地檔缺口；明顯區分「無成交」與「資料斷線」。
- 訂閱範圍與開關只供系統管理；本 PR 不提供 Web 含 API key 的設定表單，不新增人工填買單與證券委託按鈕。

## API 與畫面契約提案

GET /api/intraday/status → collector status、last heartbeat、disabled / stale；GET /api/intraday/symbols → 當前允許股票與類別；GET /api/intraday/{symbol}/snapshot?lot_type=BOARD|ODD → 標的行情、五檔與事件，不提供券商交易能力。回傳 schema_version, generated_at, as_of, health, freshness, provenance, payload；資料夠舊時 status=STALE 且行情報價不顯示有效可下單價格。API 預設走 localhost / trusted LAN 的原本服務；若往外公開需另行評估授權、資訊授權條款。

前端優先採定期輪詢唯讀快照（頻率與負載先做測試再定），不直接從 SDK SSE 推流給 Web；若盤中使用者需求證明輪詢不足，再另列基礎效能設計，不增開功能 PR。

## 正常 / 失敗 / 暫停路徑

- feature flag 關閉：導覽隱藏或顯示服務未啟用，絕不影響首頁。
- 非交易時間：顯示最後交易日快照與「已收盤」，不可誤稱即時資料。
- Collector 斷線：顯示紅字資料不可用、最後更新時間；舊快照仍可供歷史檢視，但不可呈現為可交易狀態。
- 單檔沒有成交：標 WAITING_FOR_TICK；五檔有資料可獨立顯示，不當作系統停擺。
- 有觀察事件，但原策略裁決為 REJECTED：只顯示「研究/觀察中」，不顯示 BUY / APPROVED。
- 上游資料不合規、symbol/lot_type 不合法、JSON 損毀、檔案不存在：明確 4xx/5xx 與唯讀降級，不能破壞整頁渲染。

## 測試 / DoD

1. FastAPI 路由使用 fixture 與依賴注入，不 import 真正 Shioaji SDK；頁面沒有憑證、交易按鈕或真實 broker side effect。
2. BOARD/ODD、非交易日、延遲報價、沒五檔、重複事件、非法 symbol、時間轉換與字串跳脫皆有測試。
3. route root_path /trading、既有首頁、reports、sector-flow、LLM 頁面回歸通過。
4. PR-4 程式合併後依 AGENTS.md 必須重啟 trading-web.service，實際 GET /healthz 與 /trading/ 回 200 才算部署完成；在文件 PR 階段不聲稱已執行。
5. 盤中顯示資料延遲時不誤導為行情仍即時；資料層失敗不影響 15:10 / 15:12 cron。
6. 視覺界面不把「ObservationEvent」與「正式 SignalItem / OrderIntent」混用。

## 回退

功能旗標關閉後回到既有儀表板。保留 PR-1/PR-2 原始資料，無需資料庫 schema rollback；不動既有交易排程。
