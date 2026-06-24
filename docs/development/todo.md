# 待辦 / Roadmap

專案待辦的單一來源（cross-cutting）。決策脈絡見 [`engineering-log.md`](engineering-log.md)、UI 細節見 [`ui-development.md`](ui-development.md)。
狀態：⬜ 未開始 ／ 🔄 進行中 ／ ✅ 完成。

> 更新慣例：完成的項目標 ✅ 並保留一行（含 commit / 日期）；新待辦補到對應分區。

---

## A. 立即（本週）

- ✅ **A1 record-fill 可歸策略**（2026-06-14）。`record-fill` 新增 `--strategy-id`（預設仍 `MANUAL` 向後相容、結構性排除於監控）；指定具 exit 區塊的策略後，部位自當日起由 daily run 的 `update_high_watermarks` 納入、risk_exit 監控、策略別損益歸因。未知 strategy_id 拒絕。下游 FIFO/PnL/watermark 機制原即以 strategy bucket 運作，故僅改 record-fill 一處。詳見 engineering-log 2026-06-14、記憶 `record-fill-strategy-attribution`。後續 C1 Web 寫入 UI 可沿用此參數。
- ✅ **A2 systemd 常駐安裝**（2026-06-15）：`trading-web.service` 已 `enable --now`，`systemctl status` active (running)、本地 `curl /` 200、開機自啟。日後改 code 需 `sudo systemctl restart trading-web`。
- ✅ **A3 影子先行開跑**（2026-06-15）：crontab 原有 `run_daily_sim.sh`（每交易日 15:10、只跑 run-daily 不產報告/告警）已**替換**為 `scripts/shadow_daily.sh simulation-main`（同一時點 15:10，13:30 收盤後資料沉澱；其餘排程未動）。明日 6/16 15:10 起自動跑 run-daily + 每日報告 + 失敗告警。

## B. go-live 收尾（gate，源自 2026-06-14 CEO review / HOLD SCOPE）

- ✅ git tag `v0.1.0-pre-golive` 回滾錨點（commit fc432d4）
- ✅ 清理殘留 `active-approval.json`（commit fc432d4）
- ✅ 每日影子報告 + `shadow_daily.sh`（commit a833141）
- 🔄 **B1 影子先行 ≥3 個交易日**並人工核對（2026-06-15 實施）。`docs/shadow-signoff.md` 簽核表已備。
- ✅ **B2 cron 失敗告警接 Discord**（2026-06-15）：`src/notification/discord_alert.py` 模組已就位（httpx），`shadow_daily.sh` 已改進，`config/alert.local.yaml.example` 與 `.gitignore` 已備。Token 走 `~/.openclaw/.env`，channel_id 走 gitignored 的 `config/alert.local.yaml`，測試全綠（9 項）。**go-live 啟用步驟已完成並實測**：`config/alert.local.yaml` 已建立、實際發送成功收到 Discord 告警；修正 `DiscordAlertConfig` 的 dotenv 載入缺口（改用 `dotenv_values` 讀取 `~/.openclaw/.env`）。
- ⬜ **B3 開實單起步**：B1+B2+D2 全綠後，建議路徑 C（先單策略 `trend_breakout`、限額小量），再放第二支。

詳見記憶 `ceo-review-golive-2026-06-14`。

## C. UI 分期

- ✅ 分期 1：唯讀 Web 儀表板（commit 2dd94cd）＋常駐 unit（0d7dc04）＋預設日期/移除底部（2ad8aed）
- ✅ service 層讀取側雛形（`services/dashboard.py`）
- 🔄 **C1 分期 2：service 層寫入側 + 寫入操作**：**寫入 service 骨架已備（2026-06-15，`services/trade_write.py`：record_fill / reject_signal / un_reject_signal，CLI 已改薄消費者、14 直測綠燈）**；**Web POST 路由待 go-live**。設定成交結果（含策略歸屬，A1 ✅ `--strategy-id`，Web 表單沿用）、拒絕訊號（`reject-signal`）。寫入必走既有 engine/projection 邏輯（不可繞過）。**go-live 影子驗證通過前不啟用。**
- ⬜ **C2 分期 4：CLI TUI（Textual）**：SSH/本機操作中心，與 Web 共用 service 層。
- 🔄 **C3 圖表**：
  - ✅ **C3-1 資金管理卡 + 持倉資產配置圓環圖**（2026-06-15）：儀表板新增「淨投入本金/可用現金/持倉總市值/總權益/總報酬率%」5 卡 + doughnut（含現金一塊，分母=總權益）。純讀 `cash_ledger/cash_balances/position_lots/market_bars`，無寫入/無新表/無回測觸發。市值 `int(qty*close//10000)`、當日無 bar 以均價 fallback 標「估算」；報酬率分母=`INITIAL_DEPOSIT` 合計（0 顯示「—」）。新 service `dashboard.build_capital_overview`、market_repo 由路由注入。Chart.js v4 **self-host** 於 `static/js/`（零 CDN，區網可用）。對拍 report.py 市值差異 0。測試：`test_capital_overview.py` 9 項 + `test_web_server.py` 補渲染斷言，unit 全綠 160。
  - ✅ **C3-2a 回測權益曲線持久化 + Web 顯示**（2026-06-15）：`cmd_backtest_run` 跑完後落檔 `equity_curve`/`statistics`/meta 成 JSON（仿 `write_daily_report` 三件套：結果檔 + `LATEST.txt` + `INDEX.tsv`，`profit_factor` inf→null），印 `BACKTEST_RESULT_PATH=`。Web 新增 `/backtests`（列表）與 `/backtests/{name}`（統計卡 + equity curve 折線圖，Chart.js self-host，`profit_factor=null`顯示「∞」）。新 service `list_backtest_results`/`read_backtest_result`（防目錄穿越）。不碰 DB schema、不碰 daily run、不影響 B1 影子驗證。測試：`test_backtest_report.py` 3 項 + `test_backtest_results_service.py` 5 項 + `test_web_server.py` 補 5 項，unit 全綠 173。實測一次回測（2026-04-09~06-12）驗證落檔與頁面渲染正常。
  - ⬜ **C3-2b 實盤/影子每日權益曲線**：需新增 `equity_snapshots` 表並改動 `DailySimulationRunner.run_daily()`（B1 驗證中的核心路徑），留待 B1 完成後再做。
  - ✅ **C3-3 持倉股票名稱顯示**（2026-06-15）：儀表板 5 張表格（持倉／今日成交／下次執行／公司行動／執行事件）顯示中文股名。`STOCK_NAMES` 由 `src/cli/common.py` 搬至共用 `src/contracts/stock_names.py`（cli 與 service 共用、不反向依賴）。持倉表獨立「名稱」欄；其餘 4 表代號後接 `.tag-muted` 小字。圓環圖 label 不改。測試：`test_web_server.py` 補名稱斷言。
- ⬜ **C4 neumorphism 風格**：套用指定設計稿（只動 `static/style.css` 與模板 class，不動資料層）。非現在。

詳見 [`ui-development.md`](ui-development.md) §1、§9。

## D. 資料正確性

- ✅ **D1 record-fill 策略歸屬**（= A1，資料側，2026-06-14）：`--strategy-id` 已可將手動成交落入策略 bucket，恢復停損涵蓋與損益歸因準確度。後續可選增 `--from-signal`（由來源訊號帶出歸屬），併 C1 Web 寫入 UI 設計。
- ✅ **D3 單筆出場試算 + 既有持倉長期重分類**（2026-06-15）：
  - `trade exit-check --symbol X --strategy Y`：對單一持倉套某策略 `exit:` 規則跑一次（共用 `RiskExitEngine.explain_exit`，與每日引擎同源；`_evaluate_position` 改委派此方法、行為不變、回歸測試綠），dry-run 報告四條件數值與是否觸發。純唯讀、不發 SELL、不碰 `run_daily`。新 service `services/exit_check.dry_run_exit`。
  - `trade set-long-term --symbol X [--unset] [--strategy-id ...]`：更新 `fills.is_long_term` + `rebuild_from_ledger` 把既有持倉改列長期。**預設只動 MANUAL bucket**（避免誤把同 symbol 的策略部位排除於 risk_exit）。新 service `trade_write.set_long_term`。已對 `data/app.db` 三檔 ETF（00400A/00981A/00994A）手動持倉標長期，00994A 的 pullback_rebound 200 股策略部位維持受監控，reconcile 通過。測試：`test_exit_check.py` 4 項 + `test_risk_exit.py` explain_exit 2 項 + `test_trade_write_service.py` set_long_term 4 項，unit+integration 全綠 187。
- ✅ **D2 除權息 / 公司行動追蹤（MVP 人工標記+調整）**（2026-06-15）：新增 `corporate_actions` + `position_cost_adjustments` 事實表、`projection.apply_corporate_action` 方法（冪等、保對帳平衡）、CLI `corporate-action record/apply/list/check` 指令。支援現金股利、股票股利。**下午修正三個潛伏 bug**：現金股利單位錯配（cash 是整數元、price 是×10000，配息入帳須 ÷10000，否則大 10000 倍）、現金股利未更新 cash_balances（破 reconcile）、股票股利未寫合成 fill（破 fills↔lots 數量不變式）。新增 `corporate-action check` 盤點持倉/除息登錄狀態、daily_report §9 與儀表板「公司行動」區塊露出（未套用以 ⚠ 標示）。測試 6 項全綠（含 RECONCILE_OK 強斷言）；端到端驗證 00994A 配息 1.5 元 → 現金 +7500、均價 17.30→15.80、reconcile 仍 OK。單位慣例見記憶 `unit-conventions`。自動抓取（FinMind）與減資完整支援為範圍外。

## E. 技術債

- ✅ **E1 拆 `src/cli.py`**（2026-06-15）：2053 行單檔 → `src/cli/` 套件（11 域模組 + `common.py` + `main.py` + `__init__.py`），最大 409 行（trade）、多數 <300。共用 helper 集中 `common.py`，領域 handler 以 `common.X()` 模組限定呼叫（保住測試 monkeypatch）。`__init__.py` re-export 所有 `cmd_*` 與 `resolve_account_id`/`sign_manifest`（測試直接 import 不變）；`app.py` 的 `from src.cli import main` 不動。測試僅改 3 個 patch 目標（→ `src.cli.common.*`）。行為/參數/輸出零變動，全套件 154 綠、CLI 冒煙一致。**第一刀**（trade_write 寫入 service，2026-06-15）已先行。

## F. 流程 / 工具

- 🔄 **F1 施工記錄維護**：每次開發變更記於 `engineering-log.md`。
- ⬜ **F2 施工記錄封裝為 skill / MCP**：待 UI 建置完成後，自動產出開發記錄（目前手動 md）。詳見記憶 `ui-requirements`。

## G. 研究回測 / 策略驗證（Phase 0/1/2 + R；詳見 plan `2455-cosmic-fountain.md`）

- ✅ **誠實回測實驗室 milestone**（2026-06-23，311 tests）：Phase 0（雙價/CA 帳本、FinMind/TWSE provider、PIT universe 骨架）+ Phase 1（風險/穩健指標、四 benchmark、五級裁決）+ Phase 2（Thesis/Research Ledger/lockbox/參數高原）首度在**真實資料**上跑通。回補 research.db（44,959 bars, 2018-2026, 含 2022）、修 3 個整合 bug。commits `277c096`/`08b09a5`/`4cdd1d3`。
- ✅ **trend_rider「順勢交易者」Challenger**（2026-06-23）：「讓贏家跑」exit config（不動現役兩支）。回測亮眼（+121.9%/Sharpe 1.20/成本 5.6%、崩盤防守保住）但 **+122% 受後見之明污染、報酬 edge 未證實**。
- ✅ **儀表板 UI**：持倉部位加「最後收盤」欄 + 策略別損益顯示中文名（commit `443ce86`）。
- ✅ **R-T4b Track 2 — PIT 流動性排名 universe**（2026-06-24）：`liquidity-top150-v1`（451 檔/月再平衡）+ 三支 regime_gate 註冊 → **專案首批非 INVALID 裁決**：trend_breakout=RESEARCH_PASS（唯一），pullback_rebound/trend_rider=REJECTED（CI 下界為負）。commits `40eb1ef`/`c48b391`，詳見 engineering-log 2026-06-24。殘留缺口：survivorship（roster 單一快照）、regime/bear gate 未評估。
- ✅ **R-T4b Track 1 — account_overrides + 治理退役**（2026-06-24）：per-account 進場策略 override 機制；pullback_rebound（REJECTED）從國泰退役（`account_overrides: {國泰: [trend_breakout]}`）、不再產生新 BUY 建議，既有持倉由 risk_exit 照常出場；trend_breakout 兩帳號續留、simulation-main 回退兩支續觀察 pullback。詳見 engineering-log 2026-06-24。
- 🔄 **優化迴圈（首輪誠實負面）**：trend_rider 不對稱停損高原（fixed_stop 700/800/900）**整片 REJECTED**＝「讓贏家跑」PIT 不成立、結構性修補救不了；pullback 成本>毛利不調（避免過擬合）。下一步：若要救須**新 thesis/新策略**（非 tweak），或接受兩支淘汰。
- ⬜ **backtest 冪等**：signal `bundle_id` 改 run-scoped（現以 copy-per-run 繞過）。

---

### 關聯記憶
`goal-validate-profit`、`manual-only-execution`、`ceo-review-golive-2026-06-14`、`multi-strategy-rollout-state`、`ui-requirements`、`record-fill-strategy-attribution`、`discord-alert-config`。
