# AGENTS.md - tw-swing-trading-mvp

> **本資料夾**：這個資料夾是台股**波段**系統（GitHub `tw-swing-trading-mvp`），名稱 `tw-day-trading` 是歷史遺留，與當沖實驗室 `tw-day-trading-lab` 無關。站台：`https://192.168.50.109/trading/`。詳見文末「台股相關資料夾對照」。

本專案為台股波段量化交易系統 MVP (Taiwan Stock Swing Trading Quantitative Trading System MVP)。本文件旨在為後續參與開發的 AI 協作代理 (Agents) 提供全局上下文、邊界規則與當前進度指引。

## Project Mission (專案任務)

本專案的核心目標是建構一個**高確定性、可重現、可安全對帳**的量化交易閉環。

> **北極星目標（2026-06-23 使用者釐清）**：**建策略 → 回測 → 驗證是否「真的」賺錢 → 持續優化**。MVP 階段先把「流程正確性 + 資料一致性」做穩（下列 1-5），其上的「研究回測層（Phase 0/1/2 + R）」才是回答「會不會賺錢」的地方（見 README §8、記憶 `goal-validate-profit`）。治理護欄（kill-switch/lineage/影子升級階梯）是次要，**先驗證出 edge 再說**。

MVP 流程閉環的不變式：

1. **確定性回測與模擬**: 歷史回測與每日模擬共用同一個交易核心 (`TradeExecutionEngine`)。
2. **防範未來資料**: 策略只能透過 `PointInTimeMarketData` 讀取截止至 `as_of_date` 的市場數據，杜絕 Lookahead Bias。
3. **安全授權與風控**: 所有的 `BUY` 動作必須嚴格通過 `StrategyApprovalManifest` 的有效期、參數 Hash、單筆/每日額度限制查驗。
4. **精確的交易帳務事實**:
   - `fills` (成交紀錄) 與 `cash_ledger` (現金流水帳) 為系統的不可變權威事實 (Authority Truth)。
   - 持倉與損益均自事實重建 (Projections)，不使用直接 `UPDATE` 事實表之操作。
5. **每日流程冪等性**: 當日流程出錯後，可安全地在同一日期重跑，不重複成交或扣款。

---

## Hard Boundaries (硬性安全邊界)

- **下單全手動、不接券商交易 API**：`國泰`＝**真實帳號**但採 `run-daily --no-auto-execute`（plan-only，只產訊號/計畫），**所有下單由人工處理、永不呼叫券商 API 自動成交**；實際成交以 `record-fill` 事後補登。⇒ `broker_orders` 是死表（設計如此）、不做券商對帳。`simulation-main`＝影子帳號（FakeBroker 全自動，永不串實盤）。見記憶 `manual-only-execution`、`real-shadow-account-split`。
- **Shioaji 權限限制**：僅允許在同步行情時調用 Shioaji API（read-only 行情），帳號不可載入交易 CA 憑證。
- **cron 跑工作區程式**：每交易日 15:10/15:12 cron 跑「當下 checkout 的程式」，改 code 即影響下次 live run，務必確認 live-safe（如 backtest-scoped 改動不影響 `simulation` mode）。
- **Web 部署必須 restart + readback**：凡修改 `src/web/`，或修改 Web 會 import 的 service/read model（例如 `src/application/services/dashboard.py`），交付前必須執行 `sudo systemctl restart trading-web.service`，再確認 `/healthz` 與 `/trading/` 首頁皆回 200。Jinja 會從磁碟讀到新模板，但未重啟的 uvicorn 仍持有舊 Python module；兩者混版可造成首頁 `500 Internal Server Error`（2026-09-07 曾發生）。只看到 unit tests 通過或 service 為 active 不算部署完成。
- **秘密資料保護**：不得在 log、commit、或對話中洩露 `SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY` 等敏感變數。本機應使用 `.env` (必須加入 `.gitignore`) 進行設定。
- **防止浮點數精度問題**：
  - 帳戶現金與交易金額以**整數 (TWD)** 儲存。
  - 成交價與日 K 價格以**固定縮放 4 位小數的整數 (Price x 10000)** 儲存 (例如價格 `102.5` 儲存為 `1025000`)。
- **資料完整性**：交易日曆由 `TradingCalendar` (XTAI) 主導，缺漏日 K 資料時返回 `WAITING_MARKET_DATA` / `DATASET_INCOMPLETE`，嚴禁跳過缺漏日進行假交易。

---

## Current Status (當前狀態)

- [x] 完成 `tw-swing-trading-mvp-implementation-plan.md` 需求定義。
- [x] 在 Artifacts 中生成 [implementation_plan.md](file:///home/hom/.gemini/antigravity-cli/brain/51d942a2-225b-433a-913f-6889f769c880/implementation_plan.md) 實作方案，待用戶核准。
- [x] Milestone 0: Foundation (已完成)
- [x] Milestone 1: Simulation Closed Loop (已完成)
- [x] Milestone 2: 20~60 日確定性回測 (已完成)
- [x] Milestone 3: 3~6 個月初步觀察 (已完成)
- [x] Milestone 4: 每日模擬運行 (已完成)
- [x] 長期持有部位支援 (手動錄入新增 `--long-term` 參數並於策略出場邏輯中排除) (已完成)
- [x] 資金配置與超額分配優化 (實作 `plan_all` 與 `PortfolioOrderAllocator` 規則) (已完成)
- [x] 長期持倉與策略持倉之 FIFO 隔離 (已完成)
- [x] 模擬重置邊界安全限制 (已完成)
- [x] 交易宇宙與估值宇宙拆分 (已完成)
- [x] 非互動式環境強制指定 `--account` 參數（cron/CI 安全防護）(已完成)
- [x] 每日報告頂層輸出 Manifest Preflight 狀態與過期預警 (已完成)
- [x] `record-fill` 成交事實補上 `source` 欄位，手動補錄標記 `MANUAL_IMPORT` (已完成)
- [x] `simulation run-daily` 加入進程級 file lock 防止雙重執行 (已完成)
- [x] `trade reject-signal` / `trade un-reject-signal`：訊號人工拒絕閘門，`signal list` 顯示 `signal_id` 與拒絕狀態 (已完成)
- [x] `report pnl` 支援依交易來源 (`STRATEGY` / `MANUAL_IMPORT`) 進行損益與部位的分流顯示與篩選 (已完成)
- [x] **多策略並行第一階段**（依 `multi_strategy_plan.md` v3，2026-06-12 完成）:
  - 帳務隔離：`fills`/`position_lots`/`fifo_matches`/`realized_pnl` 新增 `strategy_id`，FIFO 扣帳限定同策略 bucket；`scripts/migrate_multi_strategy.py` 冪等回填（已對 `data/app.db` 執行，9 筆手動 fills → `MANUAL`）。
  - Approval 多策略並存：`active-approvals.json` map（strategy_id 為鍵）、`approval list`/`deactivate`、引擎按訊號策略路由驗證；**SELL 永不受授權閘門阻擋**。
  - `risk_exit` 執行引擎：固定停損/移動停利/均線失效（buffer+連續確認）/時間停損，參數來自各策略 YAML `exit:` 區塊（納入 params_hash）；移動停利最高價持久化於 `position_high_watermarks` 事實表（rebuild 不清除）。
  - TSE 指數同步（`Contracts.Indexs` 路由、INDEX 校驗放寬、估值池隔離）與兩個新進場策略 `trend_breakout`/`pullback_rebound`（含大盤 60MA 濾網、已持有不加碼）。
  - 多策略 Allocator：固定管線順序（exit → breakout → pullback）、同日同標的 netting（`NETTING_SUPPRESSED` 入 `execution_events` 審計表）、策略/全局雙層限額、T+1 賣出款次日可用。
  - `daily_runs` 單一 orchestrator run（`strategy_id='MULTI'`）；bundle/signal id 納入 strategy_id 防撞鍵；執行時取回當日全部 bundle。
  - 舊策略 `trend_pullback` 退役：補 `exit:` 由 risk_exit 管理存量倉位，不再進場（回測仍可顯式指定）。
  - `report pnl --by-strategy` 策略別損益歸因。
- [x] **單筆部位出場試算與長期重分類**（2026-06-15 完成）:
  - `trade exit-check --symbol X --strategy Y`：對單一持倉套某策略 `exit:` 規則跑一次（共用 `RiskExitEngine.explain_exit`，與每日引擎同源），dry-run 報告四條件數值與是否觸發。**純唯讀**、不發 SELL、不碰每日執行路徑。
  - `trade set-long-term --symbol X [--unset] [--strategy-id ...]`：將既有持倉重分類為長期持有（更新 `fills.is_long_term` 後 `rebuild_from_ledger`）；**預設只動 `MANUAL` bucket**，避免誤把同 symbol 的策略交易部位排除於 risk_exit。已對 `data/app.db` 三檔 ETF（00400A/00981A/00994A）手動持倉標長期，對帳通過。
  - Web 儀表板多張表格（持倉／今日成交／下次執行／公司行動／執行事件）顯示中文股名（`src/contracts/stock_names.py` 共用對照，cli 與 service 共用）。
- [x] **現金異動（append-only）與 rebuild 開帳修正**（2026-06-17 完成）:
  - `account adjust --amount ±N --reason "<原因>"`：append 一筆不可變 `CASH_ADJUSTMENT`（`source_type=MANUAL`）事件（提領為負、補入為正），原因存 `cash_ledger.memo`。**不改寫既有 `INITIAL_DEPOSIT`**（與會刪除重寫初始入金的 `adjust-cash` 區別）。`PortfolioLedger.adjust_cash`。
  - `rebuild_from_ledger` 開帳餘額由「只認 `INITIAL_DEPOSIT`」改為「SUM 所有非 FILL 現金事件（`source_type != 'FILL'`）」。同時修好潛在 bug：`DIVIDEND` 配息原本 rebuild 後會從餘額消失、打破 reconcile（live DB 尚無配息，未爆）。
- [x] **「下次執行」股數/金額/可讀理由 + 復活 `order_intents`**（2026-06-17 完成）:
  - `engine.execute_bundles` 規劃段抽成純讀 `plan_bundles()`（無 broker/無寫入）；run-daily Stage 3c 產生隔日訊號後以同一路徑 dry-run 並冪等寫入 `order_intents`（`PENDING`+規劃股數 / `BLOCKED`+原因）。Web「下次執行」LEFT JOIN 該表顯示股數、金額（qty×reference_price），純唯讀。
  - reason_code → 可讀句子集中於 `src/contracts/reason_codes.py`（`signal_reason_text` 預留 `llm_explanation` 參數作為日後 LLM 補強理由的掛載點）；委託被擋原因 humanize 為 `block_reason_text`。被使用者 `reject-signal` 拒絕的訊號於儀表板標「已拒絕」。
- [x] **誠實回測實驗室 + trend_rider Challenger**（2026-06-23 完成，311 tests；plan `2455-cosmic-fountain.md`、README §8）:
  - **Phase 0/1/2 首度在真實資料上跑通**（先前全是合成單元測試）：回補 `data/research.db`（44,959 bars, 2018-2026, 含 2022 完整空頭），修 3 個整合 bug（fingerprint `set(dict)`、TSE→TAIEX data_id、approval 時效閘擋歷史回放）。
  - 量尺：風險/穩健指標、四 benchmark、報酬分層、DSR/bootstrap/Herfindahl/有效N gate、成本占比、分年表；**五級裁決狀態機**（今日 diagnostic universe → 只能 `INVALID`+diagnostic_result）；Research Ledger / 家族級 lockbox / 參數高原。
  - 三支真實回測：現役兩支真 edge 是**崩盤防守**（COVID/2022 都只虧 ~3%），但持續上升趨勢嚴重低捕獲（2024 AI 年）。新增 **`trend_rider`「順勢交易者」**（讓贏家跑，純靠 exit config、零引擎改動、保留 index 60MA 防守）→ +121.9%/Sharpe 1.20/成本 5.6%，但 **+122% 受後見之明污染、報酬 edge 未證實**（待 PIT universe）。
  - UI：儀表板持倉部位加「最後收盤」欄、策略別損益顯示中文名。

---

## Next Development Priority (下一步開發優先順序)

> **2026-10-07 更新**：以現行程式碼（2026-07-03 錯估稽核修復後，gate 統計改吃淨損益）重跑，**trend_breakout = REJECTED**（期望值 CI 下界 −108.53）。**專案目前沒有任何 RESEARCH_PASS 策略**；國泰 `account_overrides` 已設為空清單（不產生任何新 BUY 建議，既有持倉 risk_exit 照常出場），simulation-main 續跑兩支做 forward 觀察。研究 Challenger `breakout_shadow_filter` 主檢定 NO_INCREMENT（剔除率 61.3% > 60%）。下方 2026-06-24 敘述保留為歷史。詳見 engineering-log 2026-10-06。

當前處於「**Track 2 PIT 公平裁決完成、專案首批非 INVALID 裁決已出**」的里程碑之後（2026-06-24）。三支 PIT 重跑（liquidity-top150-v1、451 檔）：**trend_breakout = RESEARCH_PASS（唯一）**，pullback_rebound / trend_rider = **REJECTED**（後兩支逐筆期望值 bootstrap CI 下界為負；trend_rider diagnostic +122% 幾乎全是後見之明）。詳見 engineering-log 2026-06-24。下一段：

1. ✅ **Track 1 / account_overrides + 治理退役（2026-06-24 完成）**：per-account 進場策略 override 已實作；pullback_rebound（REJECTED）從國泰退役、trend_breakout（RESEARCH_PASS）兩帳號續留、simulation-main 續觀察 pullback。既有持倉由 risk_exit 照常出場。
2. **優化迴圈（首輪誠實負面）**：trend_rider 不對稱停損高原（fixed_stop 700/800/900）整片 REJECTED＝「讓贏家跑」PIT 不成立、結構性修補救不了；pullback 成本(146%)>毛利、不調（調＝過擬合，違反 S4）。**下一步要救須新 thesis/新策略（非 tweak），或接受兩支淘汰、專注 trend_breakout**。連虧/REJECTED 非自動調參理由。
3. **backtest 冪等 + 殘留缺口**：signal `bundle_id` 改 run-scoped（現以 copy-per-run 繞過）；PIT 殘留 survivorship（roster 單一快照漏部分早期下市股）；regime/bear gate 未評估（需 regime 偵測）；成本歸因拆解（P3-T5）。

> 歷史 backlog（公司行動處理、零股撮合分流、權益曲線等）多已於 Phase 0/2026-06 完成或併入研究層；治理護欄（Phase 3A kill-switch/lineage、Phase 3-5）延後到驗證出會賺錢策略之後。

---

## Important Docs (重要文件)

- `~/.claude/plans/2455-cosmic-fountain.md` — **現行主計畫**「波段策略賺錢 — 交易治理閉環」（Phase 0-5 + R 全紀錄、現況落差盤點、R-T4b 下一段）。
- [docs/development/engineering-log.md](docs/development/engineering-log.md) — 施工記錄（每次變更的決策脈絡，新到舊）。
- [docs/development/todo.md](docs/development/todo.md) — 路線圖（A-G 分區，含研究回測 G）。
- [README.md](README.md) §8 — 研究回測工作流（backfill + backtest --db + 量尺/裁決）。
- [docs/strategies/](docs/strategies/) — 各策略 Strategy Thesis（看結果前寫死）。
- [tw-swing-trading-mvp-implementation-plan.md](docs/planning/tw-swing-trading-mvp-implementation-plan.md) - 核心業務規則與決策記錄。
- [multi_strategy_plan.md](docs/planning/multi_strategy_plan.md) - 多策略架構設計與風險評估規劃書 (v3)。

---

## Known Architectural Limits & Risks (已知架構限制與風險)

後續開發前，必須注意當前 MVP 實作存在的以下設計限制與風險：

1. **手動成交的事實完整性**:
   - 目前 `record-fill` 已標記 `source = MANUAL_IMPORT` 並落入 `MANUAL` 策略 bucket（結構性排除於 risk_exit 之外），且 `report pnl` 已支援依交易來源/策略分流，但手動錄入仍僅能使用估計費率，且缺乏沖銷修正的模型支援（reversal / corrected fill）。
   - MANUAL／長期持倉**不會被 risk_exit 自動賣出**。要讓某策略賣出，須在補錄成交時以 `--strategy-id` 歸入具 `exit:` 區塊的策略；要試算「若交由某策略管理會否觸發」可用 `trade exit-check`（dry-run、唯讀）；要把手動持倉永久免除自動出場可用 `trade set-long-term`。目前沒有「將既有 MANUAL 部位永久轉歸某策略並自動賣出」的工具（與長期持有需求相衝，刻意不做）。
2. **撮合模型與公司行動限制**:
   - 零股與整張股票採用相同的成交滑價模型；未追蹤除權息等公司行動（會使加權均價與 `position_high_watermarks` 失真，多策略上線後此風險被放大）；缺乏詳細的排程異常告警閉環。
3. **進場策略相關性高 + 上升趨勢低捕獲**:
   - `trend_breakout` 與 `pullback_rebound` 皆為 long-only 順勢策略，大盤 60MA 濾網可規避空頭但無法規避高檔盤整鈍刀。真實回測（2018-2026）另證實：兩支在持續上升趨勢中**嚴重低捕獲**（緊出場太早砍贏家，2024 AI 年幾乎零捕獲）。研究 Challenger `trend_rider`（讓贏家跑）即針對此缺口，但尚未上線/未證實 edge。
4. **尚無策略證明「會賺大錢」（PIT 公平裁決後更新，2026-06-24）**:
   - **2026-10-07：現行程式碼下 trend_breakout 亦 REJECTED（淨損益口徑期望值 CI 下界 −108.53），目前無任何 RESEARCH_PASS 策略；以下為 2026-06-24 舊口徑紀錄。**
   - 已用 PIT 流動性 universe（liquidity-top150-v1、451 檔、無後見之明）對三支正式裁決：**僅 trend_breakout = RESEARCH_PASS**（逐筆期望值 bootstrap CI 下界 +1.35、366 有效筆），pullback_rebound / trend_rider **REJECTED**。trend_breakout 的 PASS 是**統計穩健但經濟邊際**（PIT 僅 +8.75%/8.5 年、成本吃毛利 68.7%、去最佳5筆轉負、輸 0050 buy-hold +34.6%）＝「可進影子驗證」非「會賺大錢」。diagnostic（固定 21 檔）回測數字受後見之明污染，僅結構面（崩盤防守、成本占比）可信。
5. **舊 `trend_pullback` 授權檔 digest 不一致（升級前即存在）**:
   - `artifacts/approvals/approval-trend_pullback-20260610202219.json` 的 digest 與其內容不符（preflight 顯示 INVALID）。該策略已退役且 SELL 不受授權閘門影響，無實際風險；存量倉位出清後可清理。

---

## Architecture Rules (架構設計守則)

- **Port-Adapter 隔離**：將外部依賴 (如 Shioaji SDK、SQLite) 與核心業務邏輯 (策略計算、風控查驗、成交模擬) 分離，核心邏輯採用 Protocols/Interface 進行隔離。
- **無狀態核心**：`TradeExecutionEngine` 必須完全依賴傳入的 `ExecutionContext` 與明確參數，不得在其內部讀取系統時鐘 (`datetime.now()`)，便於測試與重播。
- **單一 Transaction 寫入**：成交事實的 insert、現金扣除、與 FIFO 持倉投影更新必須綁定在同一個資料庫交易中，嚴防半完成狀態。

---

## Testing / Verification (測試與驗證)

所有功能實作均需搭配對應的單元或整合測試，預設檢驗指令如下：

```bash
# 執行所有測試
pytest tests/

# 執行特定模組單元測試
pytest tests/unit/test_canonicalizer.py
```

## 族群資金流（Sector Flow V1，2026-10-03 自 tw-day-trading-lab 搬入）

獨立的報表軌道，**不碰交易、不碰 Shioaji / Telegram / GitHub 發佈**；唯一的對外通知是 cron 失敗時的 Discord 告警（與 `sync_chips.sh` 相同）。設計見 `docs/superpowers/specs/2026-10-02-sector-flow-v1-design.md`（文末含報表資料合約），頁籤 JSON 合約見 `docs/superpowers/specs/2026-10-03-sector-flow-tab-contract.md`，原實作計畫見 `docs/superpowers/plans/2026-10-02-sector-flow-v1.md`，開發紀錄（含在 lab 的歷史）見 `docs/development/sector-flow-history.md`。

### 指令

```bash
# 唯一連網的一步：只做唯讀 GET（TWSE T86 / MI_INDEX、TPEx、TDCC），寫入 data/raw；有來源 failed 時 exit 1
python3 -m app market sync-sector-flow --start-date 2026-09-24 --end-date 2026-10-01 --cache-dir data/raw

# 產業分類快照（FinMind TaiwanStockInfo，唯讀 GET）；內容與最新快照相同就不寫；寫在 data/raw/finmind/TaiwanStockInfo/{as_of}/
python3 -m app market sync-sector-taxonomy [--as-of YYYY-MM-DD]

# 回補過去的 TDCC 週快照（open data 只給最新一週）：TDCC 個股查詢頁 qryStock，一檔一週一次 POST，
# 只查快取法人資料出現過的股票（約 2,000 檔，約 2 檔／秒）。整週全成功才寫，已存在的日期不覆蓋；另寫 backfill.json 記來源與查無資料的股票
python3 -m app market backfill-tdcc-holdings --dates 2026-08-28,2026-09-04

# 完全離線，由快取重播；報表 blocked 時 exit 1（degraded 仍 exit 0）
python3 -m app report sector-flow --start-date 2026-09-24 --end-date 2026-10-01 --cache-dir data/raw \
  --output reports/2026-09-24_2026-10-01-sector-flow.json --report-output reports/2026-09-24_2026-10-01-sector-flow.md

# 族群細看（--category 可重複；--top 預設 10）
python3 -m app report sector-flow ... --category 電子工業 --category 半導體業 --top 10
```

### 網頁「族群資金」「大戶持股」頁籤的資料（dashboard）

```bash
# 完全離線；輸出 data/sector_flow/dashboard.json（暫存檔 + rename 原子寫入）；blocked（無任何交易日）exit 1
python3 -m app report sector-flow-dashboard [--end-date YYYY-MM-DD] [--cache-dir data/raw] [--output data/sector_flow/dashboard.json]
```

- 產生器 `src/application/reporting/sector_flow_dashboard.py`（`build_sector_flow_dashboard`）：end_date 往前 140 日曆天找出最近 90 個交易日，每個視窗（20/30/60/90）各跑一次 `build_sector_flow_report(..., max_days=None)`，輸出各族群每日淨額、類股指數與個股排行。數字與同日期區間的 `report sector-flow` 完全一致（2026-10-03 以真實資料逐視窗核對）。
- 網頁經 `GET /api/sector-flow` 原樣讀這個檔。格式是 producer 與 web 共用的合約，不可單方面改。
- cron 腳本 `scripts/sync_sector_flow.sh [days=7]`：依序 `market sync-sector-taxonomy`、`market sync-sector-flow`（今天往前 days 天，Asia/Taipei），無論成敗都接著跑 dashboard；任一步失敗 exit 非 0 並發 Discord 告警。crontab（2026-10-03 已安裝於使用者 crontab）：
  `0 22 * * 1-5 /usr/bin/flock -n /tmp/sector_flow_sync.lock /home/hom/services/stock/tw-day-trading/scripts/sync_sector_flow.sh >> /home/hom/services/stock/tw-day-trading/logs/sync_sector_flow_cron.log 2>&1`
- 31 天上限只限制連網的 `market sync-sector-flow` 與 CLI `report sector-flow`；`build_sector_flow_report` 的 `max_days=None` 只給離線長區間（dashboard）用。不要改回分段計算再加總：分段會讓缺價股票只被部分計入，占比和子類檔數都會偏掉。
- 視窗若因缺價改用 `net_shares` 排名，個股 `amt` 為 null，網頁顯示「—」。`stocks` 內的 f/t/dl 單位是股，`rows` 內是元。
- 大類的 `subs[].in/out` 是該子類**在這個大類裡**的成員排名，不是同名頂層族群：FinMind 給上櫃股的分類幾乎沒有「電子工業」標籤，所以「電子工業 › 半導體業」幾乎只有上市股，頂層「半導體業」約一半是上櫃。
- `large_holder`：TDCC 大戶（分級 12–15，400 張以上）近 20 個交易日內每週的變化。視窗內每一期週快照各跑一次 `build_sector_flow_report(start=dates[0], end=該週, large_holder_stocks=True, taxonomy_date=end_date)`，所以每週數字等於該日期的 `report sector-flow`（只差產業分類固定用頁籤那份）；族群列有每週估算金額 `wk`、20 日累計 `amt`（排序依據）、20 日淨增減檔數、20 日增減前 5 名個股。`large_holder_stocks` 與 `taxonomy_date` 預設關閉，所以報表輸出不變。不要用跨股票加總的張數當主指標：會被低價股主導。
  - 集保總股數（分級 17）兩期相差 ≥1% 的股票，該週不計入（`TDCC_MAX_CUSTODY_CHANGE`，2026-10-04 與使用者決定），`report sector-flow` 的 `excluded_symbols.custody_shares_changed` 記數量。沒有這條時，9 月配股季的 2884 玉山金（+9.5%）、6949 沛爾生醫（合併後總股數 20 倍）、6488 環球晶（+10.5%）會主導整個族群，例如生技醫療業單週 +1.9 兆。
  - 每週必須用同一份產業分類：報表預設用「≤ end_date 的最新 snapshot」，週報表若各自挑會用到 06-03 那份不完整的分類，多出「未分類」、數字也會偏（2026-10-04 實際踩到）。

程式位置：`src/market_data/sector_flow_sources.py`（網路邊界 + raw cache）、`src/application/reporting/sector_flow.py`（彙整與狀態判定）、`src/application/reporting/sector_flow_report.py`（Markdown）；CLI 在 `src/cli/market.py` / `src/cli/report.py`；測試 `tests/unit/test_sector_flow*.py`，fixtures 在 `tests/fixtures/sector-flow/`。快取放 `data/raw/{twse,tpex,tdcc,finmind/TaiwanStockInfo}`（`data/` 已 gitignore）。

### 語意規則（一條都不能漏）

- **網路邊界**：`sector_flow_sources.py` 是唯一網路出口（`market sync-sector-flow`、`market sync-sector-taxonomy`）；`report sector-flow` 與 dashboard 完全離線，可只靠 `data/raw` 重播。
- **金額是估算值**：金額為 `net_shares_times_close`（法人淨股數 × 收盤價）估算，**不可稱為精確資金流**。法人淨股數本身是官方值。
- **多分類全部計入、族群間不可加總**：一檔股票有多個 FinMind 分類時，正規化後計入所有分類（`CATEGORY_SYNONYMS` 合併 TPEx／舊名；創新板股票／創新版股票剔除；`其他` 只在唯一分類時才算）。族群互相重疊，**絕不可跨族群加總**。電子工業 / 化學生技醫療為 `is_broad`。不要改回一檔一分類：那會讓族群歸屬取決於快取列順序。
- **宇宙**：只含四碼普通股；**91xx 台灣存託憑證排除**。
- **細看**：`--category NAME [--top N]` 加上 `category_detail`（流入／流出前 N 檔，含外資／投信／自營商拆分、佔該側比重、每日序列；大類先列子類小計，每個子類也有自己的流入／流出前 N 檔）。不帶 `--category` 時輸出必須與之前逐位元組相同（2026-10-04 加子類排名與大戶個股明細後仍以 09-24..10-01 報表的 sha256 核對過）。
- **交易日判定**：一個日期只要至少一個來源 `ok` 即為交易日，該日任何非 `ok` 來源都會讓報告 `degraded`。**四個來源全部 `missing` / `no_data` 的日期**進 `dates_without_data`，**視為休市**（使用者 2026-10-03 決定；不另建假日曆）。抓取失敗仍會浮現，因為 `sync-sector-flow` 會 exit 1，所以只有「從沒抓過」的日子可能被誤判為休市。**`schema_error` 一律 degrade**。
- **`source_status[*][*].cache_path` 是相對於 `--cache-dir` 的路徑**，不論 cache dir 怎麼寫，JSON 都逐位元組相同。不要把絕對路徑放回報告。
- **TDCC 大戶要兩期**：levels 12-15 需要兩期週 snapshot；少於兩期時報告 `insufficient_data`，不可宣稱大戶增減。過去的週快照可用 `market backfill-tdcc-holdings` 從 qryStock 回補（約保留一年）；2026-10-04 抽 42 檔 × 2 週與 open data 比對，人數、股數、比例 0 差異。qryStock 的「合計」列編號會是 16 或 17（看有沒有差異數調整列），回補一律存成 open data 的 17。
- **TWSE 日期不符視為 `no_data`**：TWSE 在非交易日可能回前一交易日資料；payload 日期與請求日期不同就是 `no_data`。
- **現況（2026-10-03）**：快取涵蓋 2026-05-15..10-02 共 97 個交易日，TWSE / TPEx 四個資料集日期完全對齊（5 秒間隔回補，0 失敗；先前的 HTTP 520 未再出現）。TDCC 已有 2026-08-28..10-02 共 6 期週 snapshot（前 4 期由 qryStock 回補，有 `backfill.json`）。產業分類 snapshot 已更新為 2026-10-03（4,329 筆），分類覆蓋率 100%，「未分類」族群消失（38 → 37 個），報告狀態 `ok`。產業分類是有日期的 snapshot：報表用「≤ end_date 的最新一份」，所以 end_date 早於 10-03 的報表仍用 06-03 那份。

### 後續維護（原 Track 3）

- 「大戶持股」頁籤顯示近 20 日的大戶週走勢（2026-10-04 起；同日從「族群資金」頁籤移出成獨立頁籤）。2026-08-28..09-18 四週由 qryStock 回補，之後由 cron 每週從 open data 接續。cron 若漏掉某週，用 `market backfill-tdcc-holdings` 補；qryStock 只保留約一年。
- 產業分類由 cron 每天檢查，內容有變才寫新 snapshot；FinMind 若出現新的分類名稱，要檢查是否需補 `CATEGORY_SYNONYMS`。
- 維持唯讀公開資料：不碰 Shioaji、Telegram 或 GitHub 發佈。
- 平日 22:00 cron 已安裝（2026-10-03）；網頁「族群資金」與「大戶持股」頁籤共用 `GET /api/sector-flow`，先打開哪個就由哪個抓，只抓一次。

## 台股相關資料夾對照（2026-10-03 盤點）

本機有多個名稱相近、目標不同的台股專案。各資料夾的文件都放同一張表；有變動時請一併更新。

| 資料夾 | GitHub repo | 目標 | CLI | 站台 | 狀態 |
|---|---|---|---|---|---|
| `~/services/stock/tw-day-trading` | `ChenHom/tw-swing-trading-mvp` | 台股**波段**量化交易 MVP：回測、每日模擬（paper / FakeBroker）、風控授權、FIFO 對帳。另有族群資金流報表與站台「族群資金」頁籤（2026-10-03 自 lab 搬入）。資料夾名稱是歷史遺留，**不是當沖** | `python3 -m app <account\|market\|simulation\|backtest\|report…>` | 有：`https://192.168.50.109/trading/`（台股波段交易儀表板，`trading-web.service` → 127.0.0.1:8800） | 運作中：平日 15:10 / 15:12 影子模擬、21:00 籌碼同步、22:00 族群資金（cron） |
| `~/services/stock/tw-day-trading-lab` | `ChenHom/tw-day-trading-lab` | 台股**當沖**重建實驗室：候選名單、replay / paper 驗證、Shioaji **模擬**執行鏈驗證 | `tw-daytrade`（`PYTHONPATH=src python3 -m tw_day_trading_lab.cli …`） | 無 | 開發中；無有效排程（crontab 內 8/19–21 的收集排程已過期） |
| `~/services/stock/quantitative-trading-decision-system` | `ChenHom/quantitative-trading-decision-system` | 舊版 Shioaji 盤中當沖機器人；`tw-day-trading-lab` 只把它當資料來源與失敗案例 | `scripts/run_trading_system.sh`、`scripts/run_intraday_event_monitor.sh` | 無 | 程式凍結於 2026-04，但平日 08:30 / 08:58 仍由 cron 以**模擬模式**執行 |
| `~/services/stock/quant-feather-integration` | 無（非 git） | 整合 quantitative-trading-decision-system 與 StrategyExecutor_feather 的骨架 | 無 | 無 | 封存（2026-03） |
| `~/services/stock/StrategyExecutor_feather` | `phenomenoner/StrategyExecutor_feather`（第三方） | 富邦 Neo SDK 當沖機器人，本機分支改寫為 Shioaji | `python strategy_async_demo.py` | 無 | 封存（本機改寫停在 2026-02） |
| `~/services/stock/taiwan-stock-market-evaluation` | 無（非 git） | 空資料夾（只有 `.serena/`） | 無 | 無 | 可刪除 |
| `~/services/AI-Trading-Copilot` | `ChenHom/AI-Trading-Copilot` | FinMind 盤前 / 盤後分析與投資組合助手 | `./run.sh`、`python main.py --mode OPEN\|CLOSE` | 無 | 本機停用；GitHub Actions 排程是否仍啟用未查證 |
| `~/services/stocks-db` | 無遠端（本機 git） | FinMind → 本機 TiDB 匯入 | `./run.sh import-stocks` | 無 | 封存（TiDB 未啟動） |
| `~/services/tw-stock-research-platform` | `ChenHom/tw-stock-research-platform` | 以公開資訊為核心的台股研究決策平台（TypeScript CLI） | `npm run research` 等 | 無 | 停用（`redis-cache` 容器仍在執行） |
