# Shioaji 盤中觀察整合 — 四個實作 PR 規劃

狀態：**SPEC_CONFIRMED / IMPLEMENTATION_PENDING**（D1–D10 設計裁決已完成；**未授權真實行情連線、每日自動蒐集或正式部署**）
編寫日：2026-10-08
對應 repo：ChenHom/tw-swing-trading-mvp（master）
主目標：驗證盤中成交與五檔資訊能否改善波段進場執行品質，而不是重造當沖機器人。

## 0. 基線與事實

- 本 repo 的 src/market_data/provider.py 已有 ShioajiMarketDataProvider，但職責是 fetch_kbars 歷史分鐘 K、名稱與指數合約，不是盤中 streaming。
- src/cli/market.py 使用上列 Provider；src/web/server.py 是 FastAPI / Jinja；src/application/services/dashboard.py 是唯讀 read model。
- ChenHom/tw-day-trading-lab 的 src/tw_day_trading_lab/market_data.py 已含 Tick 正規化、raw JSONL、健康度與 replay；bars.py 已有 1m/5m 聚合。應讀取並驗證後「複用純邏輯」，不得直接以跨 repo runtime import 形成強耦合。
- config/trading.yaml（2026-10-07）國泰 account_overrides 是空清單，真實帳號不產生新 BUY；simulation-main 僅保留 trend_breakout 供前向觀察。現有 trend_breakout / pullback_rebound 研究裁決為 REJECTED。不可把即時行情接上就解除限制。
- AGENTS.md 明確限制：真實帳號人工下單、不得呼叫券商交易 API；Shioaji 僅可讀取行情、不得載入 CA。fills/cash_ledger 是權威帳務事實，持倉影子模擬與真實帳務隔離。
- 15:10 / 15:12 現有盤後排程與 /trading/ 頁面不受新功能影響；所有新入口預設關閉。
- 官方 Shioaji 文件有股票整股與盤中零股 Tick/BidAsk 訂閱；API 可用細節應用 isolated smoke 在欲採用 SDK 版本重驗。

## 1. PR 依賴與判定

| 實作 PR | 文件 | 交付結果 | 進入下一階段條件 |
| --- | --- | --- | --- |
| PR-1 | [Tick Collector](./pr-01-tick-collector.md) | 限定股票清單的 read-only stream、raw、正規化、健康監控、replay | 能在真實盤中只讀訂閱；斷線與遺失有明確狀態；同資料可重播 |
| PR-2 | [BidAsk / Observation](./pr-02-bidask-observations.md) | 同一 Collector 增加五檔、可解釋的觀察事件 | 盤中與 replay 輸出一致；不能從掛單推斷已成交 |
| PR-3 | [Forward Comparison](./pr-03-forward-evaluation.md) | 基準組 vs 觀察組、成本/錯失/樣本外對照報告 | 指標、資料時點、缺漏與裁決均可稽核；不以績效變好作工程完成條件 |
| PR-4 | [Read-only Dashboard](./pr-04-dashboard.md) | /trading/ 查詢與唯讀展示；盤中健康、行情時間與觀察事件 | Web 健康檢查、stale UI、隔離測試通過 |

PR-4 可依賴 PR-1/PR-2 的穩定資料契約先實作唯讀展示，不必等待 PR-3 的完整前向樣本；但 PR-3 未通過前，不得把觀察事件提升為買進核准。

## 2. 功能分層

Shioaji（僅 quote） → 獨立 Collector（同一個登入與訂閱管理） → append-only raw Tick / BidAsk（含接收時間與 session） → normalized events / health → deterministic 1m features 與 observations → read-only snapshot / research replay → /trading/。

研究 DB data/research.db 與真實 app.db 保持分離；原始串流優先落 data/raw/shioaji/，不污染 market_bars、fills、cash_ledger、approval、order_intents。資料消費者僅使用已正規化的資料契約。UI 不可直接持有 Shioaji 物件。

## 3. 全域不可違反條件

1. 不新增 place_order / cancel_order / CA login；即使是 simulation-main 也不讓五檔事件自動送單。
2. 不更動既有 DailySimulationRunner、TradeExecutionEngine、strategy approval、帳務事實或目前 REJECTED 裁決。
3. 所有 raw event 帶 version、source、market / session、symbol、trading_date、event_time、received_at、quote_type、lot_type、collector_session_id、sequence；收不到的欄位必須標未知，不能猜。
4. 正規化金額用 Decimal / scaled int；股票價格持久化遵循現有 x10000，成交量 canonical 為股；整股與零股不同來源，不可混算。
5. 以交易所事件時間為準，接收時間用來評估延遲；晚到、重送、斷線缺口不得靜默補假資料。
6. callback 不阻塞；bounded queue 遺失必須有 DEGRADED / FAILED 健康狀態；下游禁止產出肯定的支持/突破判斷。
7. 原始資料可離線重播；不可使用 replay 時刻之後的行情作出當時決策。
8. Collector、觀察引擎及 Web 均 feature flagged、預設停用；可單獨回復到既有每日排程。
9. source 需可稽核、沒有 API key / secret / 個資；服務採本機或內網權限；行情資料保存需遵循券商授權條款，不公開原始即時串流。
10. 測試不能只檢查綠燈；需要負例、corrupt payload、stream gap、非交易日、價格跳檔、零股、斷線、停機與重複啟動測試。

## 4. 原則性排除

- 全市場行情掃描、盤中 kbars 掃描全市場。
- 用買五賣五量差直接生成 BUY/SELL。
- 為這批資料引入 Kafka / Redis Cluster / 重型事件框架或獨立大型微服務。
- 以事後完整高低點判定「當下」已知的支撐/突破。
- 宣稱可用沒有歷史五檔的日 K 回測驗證五檔訊號。
- 手動持倉的即時監控取代原本 risk_exit 的日線規則或越權執行交易。

## 5. Grill-me / grilling 決策紀錄（設計確認完成）

官方原始參考：https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md
其實際流程：https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md

使用者已於 2026-10-08 同意全部 D1–D10 的**設計建議**。已完成 grilling，以下事項從待決改為已核准規格，但仍須按四個實作 PR 驗收；特別是 D10 只確認雙重授權流程，**這次同意並不是任一真實連線或日常排程的授權**。

| ID | 首輪可裁決的問題 | 建議的預設答案 | 狀態 |
| --- | --- | --- | --- |
| D1 | Collector 由波段 repo 自己持有，複用 lab 的純邏輯，避免共享 runtime/登入？ | 是；不建新共用服務 | AGREED 2026-10-08 |
| D2 | 訂閱僅限持倉 + 手動 watchlist + simulation-main 候選股，而非整個 top-150 / 全市場？ | 是；加可配置上限 | AGREED 2026-10-08 |
| D3 | 整股與盤中零股都收集，但獨立保存/計算，不相互推算成交深度？ | 是 | AGREED 2026-10-08 |
| D4 | PR-3 第一個研究目標以「實際可執行性、滑價與錯失交易」為主，原 REJECTED 策略不復活？ | 是 | AGREED 2026-10-08 |
| D5 | 盤中事件僅在 Web 顯示，第一版不主動 Discord/Telegram 推播？ | 是；保留原系統既有排程失敗告警 | AGREED 2026-10-08 |
| D6 | raw Tick/BidAsk 至少保留 180 交易日，盤後 gzip，磁碟使用率達警戒值停收？ | 是；首週量測、專用分割區 80% 告警 / 90% 停收、保留研究原始來源 | AGREED 2026-10-08 |
| D7 | 60 交易日 + 100 獨立候選機會 + 後段 20 交易日樣本外？ | 是；不足 EVIDENCE_PENDING；只評估執行品質、不解禁 REJECTED 策略 | AGREED 2026-10-08 |
| D8 | Collector 使用獨立且預設 disabled 的 systemd service？ | 是，不干擾既有 Web / cron | AGREED 2026-10-08 |
| D9 | 依交易日曆啟停，固定去重清單，盤中手動 reload？ | 是，上限以 SDK 測試為準 | AGREED 2026-10-08 |
| D10 | 真實行情 smoke 與每日啟用分別取得明確授權？ | 是，兩道授權閘門 | AGREED 2026-10-08（僅流程規格；連線與啟用尚未授權） |

## 5A. 第二輪提問 — 已同意（2026-10-08）

使用者已同意 D5–D7 的原始建議。下述問句保留為原始決策紀錄；其內容已是**核准的規劃要求，不等於程式已實作、資料已收集或服務已啟用**。

### Q5 / D5：推播

是否同意 **V1 只在既有 /trading/ Web 顯示盤中觀察事件與資料健康**，不新增 Discord / Telegram 的逐筆市場提醒；現有 cron 失敗通知保持不變？

建議 YES。盤中事件尚未驗證交易價值，不應先導入高頻通知。

### Q6 / D6：原始資料保存與磁碟安全

是否同意 **當日原始 Tick/BidAsk 寫 append-only JSONL、收盤後 gzip 壓縮、至少保留 180 個交易日；不自動刪除尚未裁決的研究來源**？初期一週測實際日用量再訂磁碟配額；磁碟使用率達 80% 告警、達 90% 停止收集且標 DEGRADED，不能影響既有交易排程。

建議 YES。門檻適用專用儲存分割區；若與交易 DB 共用分割區，實作前須定義保護區間及更低的停收條件。gzip 壓縮檔仍需直接被 replay 支援。

### Q7 / D7：前向研究的初步樣本門檻

是否同意 **先收集至少 60 個交易日，且至少 100 個相互獨立、事前定義的候選進場機會**，以最後至少 20 個交易日作時間順序樣本外；未達任何門檻只提供探索報告 `EVIDENCE_PENDING`？即使達標也只是執行品質的初步裁決，不足以解除原 REJECTED 策略或取代原波段 5 年以上、100 筆交易的正式研究標準。

建議 YES。統計單位是「獨立候選進場機會」，不是同一股票每分鐘的重複 Tick 或五檔快照。指標、成本、排除規則及假設鎖定於蒐集前，避免根據結果調整門檻。

## 5B. 第三輪確認 — D8–D10 已同意（2026-10-08）

使用者已同意以下**執行邊界設計**。此同意不包含實際啟動 systemd、安裝排程、登入 Shioaji 或訂閱真實行情。

- **D8 — Collector 啟動方式（AGREED）**：波段 repo 使用獨立、最小權限的 systemd service（例如 `trading-quote-collector.service`），安裝預設 disabled；不修改現有 `trading-web.service`、盤後 cron，Collector 故障不得影響交易資料庫。
- **D9 — 訂閱清單／時間（AGREED）**：依交易日曆在開盤前啟動、收盤後結束；以持倉 + 明確 watchlist + 前一盤後影子候選為去重清單，來源可稽核。盤中修改清單僅允許明確 reload；不自動掃 top-150。BOARD/ODD 與 Tick/BidAsk 配額須透過 isolated smoke 驗證後設定上限。
- **D10 — 連線授權雙閘門（AGREED）**：先用 fake SDK / replay 驗證，不連真實行情；其後須另獲一次性明確核准才能做真實唯讀 Shioaji smoke（不載 CA、不下單），通過後仍須再次另獲明確核准才啟用每日收集。此輪 Q8–Q10 同意**不代表已授權登入或新增排程**。

**Grilling 已結束。** 四份規劃文件統一為 SPEC_CONFIRMED / IMPLEMENTATION_PENDING；下一步是依順序建立實作 PR-1、PR-2、PR-3、PR-4。對尚無充分行情樣本的 PR-3，可先實作研究框架，待前向資料累積再判定 EVIDENCE_PENDING / 結果。**本文件 PR 不代表任何實作、實測或部署完成。**

## 6. 施工檢查

- 開每一個實作 PR 前先確認對應文檔的 PENDING 都已決定，補上資料契約、mock/fixture 路徑、feature flag 名稱。
- 變更需追加 docs/development/engineering-log.md，並針對 docs/development/todo.md 更新狀態；各實作 PR 各自合併，不擴張成第五個功能 PR。
- 每個 PR 都跑適用的單元/整合測試；觸及 Web 的 PR-4 需重啟 trading-web.service 並 readback /healthz 與 /trading/。
- 本次文件 PR 僅建立規劃，不表示已執行任何 live smoke、測試、部署或策略驗證。
