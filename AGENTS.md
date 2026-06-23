# AGENTS.md - tw-swing-trading-mvp

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

當前處於「誠實回測實驗室建成、trend_rider 識別為有潛力 Challenger」的里程碑之後。下一段＝ plan `2455-cosmic-fountain.md` 的 **R-T4b**：

1. **Track 1（優先、輕）— trend_rider 影子上線驗證**：per-account 策略範圍（`config/trading.yaml` 加 `account_overrides`，`simulation-main` 跑 trend_rider、`國泰` 維持兩支不污染），累積**零偏差** live forward 證據（S4 唯一真乾淨證據，比歷史 PIT 更直接答「真有 edge？」）。
2. **Track 2（較重、後排）— PIT 流動性排名 universe**：消除後見之明/survivorship（FinMind 探測：指數成分史不可行/付費；**流動性 top-N 可行、下市股歷史可查 → survivorship 可消除**）。需解 `fingerprint.py` 寫死 `diagnostic:` 前綴、backtest 接 `UniversePolicy`、backtest 支援 per-date 變動 universe → 首個非 `INVALID` 裁決。
3. **backtest 冪等**：signal `bundle_id` 改 run-scoped（現以 copy-per-run 繞過）；成本歸因拆解（P3-T5）。

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
4. **尚未證實任何策略「會賺錢」（最重要的研究限制）**:
   - 今日固定 21 檔 universe 是 **diagnostic（非 PIT）**，回測只能判 `INVALID`，只能淘汰、不能晉級。所有 diagnostic 回測的**報酬數字受後見之明/survivorship bias 污染**（手挑已知大贏家），不可當賺錢證據。可信的只有「不受標的池影響」的結構面（崩盤防守、成本占比）。要正式裁決需 PIT universe（見 plan R-T4b）。
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
