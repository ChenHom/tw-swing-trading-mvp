# AGENTS.md - tw-swing-trading-mvp

本專案為台股波段量化交易系統 MVP (Taiwan Stock Swing Trading Quantitative Trading System MVP)。本文件旨在為後續參與開發的 AI 協作代理 (Agents) 提供全局上下文、邊界規則與當前進度指引。

## Project Mission (專案任務)

本專案的核心目標是建構一個**高確定性、可重現、可安全對帳**的量化交易閉環。MVP 階段著重於流程的正確性與數據的一致性，而不負責證明策略的盈利能力：

1. **確定性回測與模擬**: 歷史回測與每日模擬共用同一個交易核心 (`TradeExecutionEngine`)。
2. **防範未來資料**: 策略只能透過 `PointInTimeMarketData` 讀取截止至 `as_of_date` 的市場數據，杜絕 Lookahead Bias。
3. **安全授權與風控**: 所有的 `BUY` 動作必須嚴格通過 `StrategyApprovalManifest` 的有效期、參數 Hash、單筆/每日額度限制查驗。
4. **精確的交易帳務事實**:
   - `fills` (成交紀錄) 與 `cash_ledger` (現金流水帳) 為系統的不可變權威事實 (Authority Truth)。
   - 持倉與損益均自事實重建 (Projections)，不使用直接 `UPDATE` 事實表之操作。
5. **每日流程冪等性**: 當日流程出錯後，可安全地在同一日期重跑，不重複成交或扣款。

---

## Hard Boundaries (硬性安全邊界)

- **嚴禁進行正式實盤交易**：本 MVP 絕不串接實盤交易 API 憑證 (CA)。
- **Shioaji 權限限制**：僅允許在同步行情時調用 Shioaji API，且帳號不可載入交易 CA 憑證。
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

---

## Next Development Priority (下一步開發優先順序)

MVP 核心功能、首批架構缺陷修正、排程安全強化、與多策略並行第一階段已完成。後續優先開發方向包含：

1. **公司行動處理**（多策略上線後風險被放大，列為最優先）:
   - 實作對除權息、股票分割等公司行動的檢測與資料調整，避免持倉均價、`position_high_watermarks` 與停損基準失真。
2. **手動成交事實完整性與修正模型**:
   - 擴展 `record-fill` 以支援手動輸入外部券商實際手續費與交易稅，並實作沖銷修正流水紀錄（reversal / corrected fill）以維持不可變 facts 的完整。
3. **零股與整張股票成交模型分流**:
   - 在 Fake Broker 成交模擬中，引進整張股票 (Round Lot) 與零股 (Odd Lot) 的撮合流動性與滑價成本分流模型。
4. **權益曲線視覺化報表**與多策略上線後的實際運行觀察（含 `execution_events` 審計回顧）。

---

## Important Docs (重要文件)

- [tw-swing-trading-mvp-implementation-plan.md](file:///home/hom/services/stock/tw-day-trading/tw-swing-trading-mvp-implementation-plan.md) - 核心業務規則與決策記錄。
- [multi_strategy_plan.md](file:///home/hom/services/stock/tw-day-trading/multi_strategy_plan.md) - 多策略架構設計與風險評估規劃書 (v3，含四項拍板決策與程式碼審查修正)。
- [implementation_plan.md](file:///home/hom/.gemini/antigravity-cli/brain/51d942a2-225b-433a-913f-6889f769c880/implementation_plan.md) - 具體實作里程碑與程式結構規劃。

---

## Known Architectural Limits & Risks (已知架構限制與風險)

後續開發前，必須注意當前 MVP 實作存在的以下設計限制與風險：

1. **手動成交的事實完整性**:
   - 目前 `record-fill` 已標記 `source = MANUAL_IMPORT` 並落入 `MANUAL` 策略 bucket（結構性排除於 risk_exit 之外），且 `report pnl` 已支援依交易來源/策略分流，但手動錄入仍僅能使用估計費率，且缺乏沖銷修正的模型支援（reversal / corrected fill）。
2. **撮合模型與公司行動限制**:
   - 零股與整張股票採用相同的成交滑價模型；未追蹤除權息等公司行動（會使加權均價與 `position_high_watermarks` 失真，多策略上線後此風險被放大）；缺乏詳細的排程異常告警閉環。
3. **進場策略相關性高**:
   - `trend_breakout` 與 `pullback_rebound` 皆為 long-only 順勢策略，大盤 60MA 濾網可規避空頭但無法規避高檔盤整鈍刀；中期方向為波動率/盤整偵測濾網或防禦型第三策略。
4. **舊 `trend_pullback` 授權檔 digest 不一致（升級前即存在）**:
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
