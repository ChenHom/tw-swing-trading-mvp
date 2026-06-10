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

---

## Next Development Priority (下一步開發優先順序)

MVP 核心功能與手動錄入長期持有功能已全部實作完成。後續開發方向包含：

1. **多策略並行支援**:
   - 擴充資料庫與 CLI，在同一個帳戶下支援多個策略（如 trend_pullback、mean_reversion）並行運行與歸因。
2. **更精細的成本與滑價模型**:
   - 引入盤中撮合模擬或根據交易量比重（Volume Participation）估算動態滑價成本。
3. **對帳報表與權益曲線視覺化**:
   - 整合前端 UI 或產生靜態圖表以直觀呈現 PnL 變化與對帳明細。

---

## Important Docs (重要文件)

- [tw-swing-trading-mvp-implementation-plan.md](file:///home/hom/services/stock/tw-day-trading/tw-swing-trading-mvp-implementation-plan.md) - 核心業務規則與決策記錄。
- [implementation_plan.md](file:///home/hom/.gemini/antigravity-cli/brain/51d942a2-225b-433a-913f-6889f769c880/implementation_plan.md) - 具體實作里程碑與程式結構規劃。

---

## Known Architectural Limits & Risks (已知架構限制與風險)

後續開發前，必須注意當前 MVP 實作存在的以下設計限制與風險，這也是下一步優先修正的範疇：

1. **資金配置與超額分配 (PortfolioOrderAllocator)**:
   - 當前 `OrderPlanner` 是針對單個訊號單獨規劃，未做帳戶層級的整體現金配置與排序限制。當存在多個 BUY 訊號時，會造成可用現金被重複/超額分配的風險。
2. **長期持有 (Long-term) 與策略持倉之 FIFO 污染**:
   - 目前長期持有僅由 `is_long_term` 標記。在執行 SELL 訊號時，資料庫的 FIFO 撮合仍會優先匹配最早買入的批次（可能是長期持有部位），造成策略部位與長期部位的配對污染。需要引入更細緻的 `management_mode` (或 `strategy_id`) 進行 FIFO 隔離。
3. **模擬重置 (Simulation Reset) 的事實刪除邊界**:
   - `simulation reset` 命令目前允許清空指定日期的模擬狀態。但對於已成交的事實數據（`fills`, `cash_ledger`）不應允許任意刪除或修改。重置指令必須受到限制（或改為 `retry` 僅重試未成功階段），以維護不可變權威事實的嚴謹性。
4. **交易宇宙 (Universe) 責任混淆**:
   - `record-fill` 錄入新股票時會自動寫入 `universe.yaml`。這將「策略候選股池 (StrategyUniverse)」與「需要更新現價的持股估值池 (ValuationUniverse)」混淆，且動態修改版控下的設定檔會破壞回測重現性。
5. **排程運行的隱式帳戶解析風險**:
   - CLI 支援省略 `--account` 進行動態解析，這方便了手動查詢，但對於自動排程 (cron) 存在極大風險。自動化任務必須強制指定明確的 `account_id` 與 `strategy_id`。
6. **手動成交的事實完整性**:
   - 目前 `record-fill` 僅能使用估計費率，且未記錄來源資訊（如 `source = MANUAL_IMPORT`、`broker`、真實手續費與稅額等），亦缺乏沖銷修正（reversal / corrected fill）的模型支援。
7. **其他限制**:
   - 零股與整張股票採用相同的成交滑價模型；未追蹤除權息等公司行動；缺乏並行運行鎖（Mutex Lock）與詳細的排程異常告警閉環。

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
