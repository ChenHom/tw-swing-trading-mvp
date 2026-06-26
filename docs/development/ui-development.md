# UI 開發文件

本文件說明 tw-day-trading 的使用者介面（Web 儀表板，未來含 CLI TUI）的**架構、資料流、執行/部署方式與擴充指南**，供後續開發者（含 AI 協作）依循。決策脈絡與變更歷史另見 [`engineering-log.md`](engineering-log.md)；待辦/分期進度見 [`todo.md`](todo.md)。

---

## 1. 目標與範圍

- **使用者**：單人、自用（自看 + 自操作），不對外、不多帳戶權限。
- **存取**：本機 / SSH / 手機，皆經**區網**；手機走 nginx 子路徑，不走外網。
- **長期工具**：非僅 go-live 期間使用。
- **分期**：
  1. **唯讀 Web 儀表板**（✅ 已完成，本文件主體）。
  2. 從 `cli.py` 抽 **service 層**（讀取側雛形已隨儀表板建立）。
  3. **寫入操作**（設定成交結果含策略歸屬、拒絕訊號…）→ Web + 之後 CLI TUI。
  4. **CLI TUI**（Textual）。
- **風格**：目前基本款 CSS；未來換 neumorphism（不影響資料/邏輯層）。

> **go-live 安全原則**：寫入型 UI 會新增可改帳務的入口，屬新風險面。go-live 影子驗證通過前，**只開唯讀**。

---

## 2. 架構分層

```
┌─────────────┐   HTTP    ┌──────────────┐   呼叫    ┌──────────────────────┐   讀    ┌─────────────┐
│  瀏覽器/手機 │ ───────► │ FastAPI 路由  │ ───────► │ service 層（讀取側）  │ ─────► │ projection  │
│  (nginx /trading) │      │ src/web/server│          │ services/dashboard.py │         │ + SQLite    │
└─────────────┘           └──────┬───────┘          └──────────────────────┘         └─────────────┘
                                 │ render
                                 ▼
                          Jinja 模板 + static CSS
```

**鐵律**：
- 路由層**不直接查 DB / 不放商業邏輯**，一律呼叫 service 層。
- service 層**純讀** projection / SQLite，回傳結構化 dict，無副作用、可單元測試。
- 寫入（分期 3）另立 write service，但**必須走與 CLI 相同、已驗證的 engine/projection 邏輯**，以維持 FIFO 隔離、冪等、授權閘門等 invariant；UI 不得直接 `INSERT/UPDATE` 事實表。

---

## 3. 檔案結構

| 路徑 | 角色 |
|---|---|
| `src/application/services/dashboard.py` | **讀取 service 層**：組儀表板資料、報告清單。純讀。 |
| `src/application/services/trade_write.py` | **寫入 service 層**（C1/E1 骨架）：record_fill / reject_signal / un_reject_signal。純資料進出（不 print/exit/argparse），走既有 projection／signal_items SQL，不繞過。CLI 已為第一個薄消費者；**Web 路由 go-live 影子驗證通過前不接**。 |
| `src/application/services/__init__.py` | services 套件 |
| `src/web/server.py` | FastAPI app 與路由 |
| `src/web/templates/base.html` | 版型骨架（topbar/nav，`{{ base }}` 連結前綴） |
| `src/web/templates/dashboard.html` | 主儀表板 |
| `src/web/templates/reports.html` | 歷史報告清單 |
| `src/web/templates/backtests.html` | 回測結果清單（C3-2a） |
| `src/web/templates/backtest_detail.html` | 回測結果詳情：統計卡 + equity curve 折線圖（C3-2a） |
| `src/web/static/style.css` | 基本款樣式（行動友善、單欄優先） |
| `src/web/static/js/chart.umd.min.js` | Chart.js v4 UMD（**self-host**，零 CDN 依賴；區網/手機可用） |
| `src/web/static/js/dashboard-charts.js` | 前端圖表 render（讀 `#allocData` JSON 畫資產配置 doughnut；優雅降級） |
| `src/web/static/js/backtest-charts.js` | 前端圖表 render（讀 `#equityCurveData` JSON 畫權益曲線 line chart：equity/cash/position_value；優雅降級） |
| `src/application/reporting/backtest_report.py` | `write_backtest_result`：回測結果落檔（JSON + `LATEST.txt` + `INDEX.tsv` 三件套，`profit_factor` inf→null） |
| `scripts/web_ui.sh` | 手動啟動（uvicorn） |
| `deploy/trading-web.service` | systemd 常駐 unit |
| `deploy/README.md` | 常駐安裝/管理說明 |
| `docs/operations/nginx-trading.conf.sample` | nginx 子路徑反代範例 |
| `tests/unit/test_web_server.py` | TestClient 冒煙測試 |
| `tests/unit/test_trade_write_service.py` | 寫入 service 直測（bucket 落點、monitor_status、TradeWriteError） |
| `tests/unit/test_capital_overview.py` | 資金總覽 service 直測（市值/報酬率/fallback/聚合/邊界） |
| `tests/unit/test_backtest_report.py` | `write_backtest_result` 直測（落檔三件套、inf→null、INDEX 多筆追加） |
| `tests/unit/test_backtest_results_service.py` | `list_backtest_results`/`read_backtest_result` 直測（空目錄/roundtrip/防穿越/非 .json 拒絕） |

---

## 4. 路由（src/web/server.py）

| 方法 | 路徑 | 說明 | Query |
|---|---|---|---|
| GET | `/` | 主儀表板 | `account`（預設第一個帳戶）、`view_date`（預設**最近有 run 的日期**，見 §6） |
| GET | `/reports` | 歷史每日報告清單（讀 `INDEX.tsv`） | — |
| GET | `/reports/{name}` | 單一報告純文字（**防目錄穿越**，僅取檔名、限 `.txt`） | — |
| GET | `/backtests` | 回測結果清單（讀 `artifacts/reports/backtest/INDEX.tsv`，C3-2a） | — |
| GET | `/backtests/{name}` | 回測結果詳情：統計卡 + equity curve 折線圖（**防目錄穿越**，僅取檔名、限 `.json`；不存在回 404） | — |
| GET | `/healthz` | 健康檢查，回 `ok` | — |

**子路徑前綴**：`root_path` 由環境變數 `TRADING_WEB_ROOT_PATH`（預設 `/trading`）決定，注入 Jinja 全域 `base`。模板所有內部連結以 `{{ base }}/...` 產生，確保 nginx 子路徑與本機直連皆正確。

---

## 5. 讀取 service API（dashboard.py）

| 函式 | 回傳 | 用途 |
|---|---|---|
| `list_accounts(conn)` | `list[str]` | 帳戶下拉來源（`cash_balances`） |
| `latest_run_date(conn, account_id=None)` | `str \| None` | 最近有 `daily_run` 的日期（預設日期用） |
| `build_dashboard(conn, projection, account_id, view_date)` | `dict` | 組整頁資料（見下） |
| `build_capital_overview(conn, projection, account_id, view_date, market_repo)` | `dict` | 資金總覽卡 + 資產配置圓環（含現金一塊，見 §6） |
| `list_reports(base_dir, limit=30)` | `list[dict]` | 由 `artifacts/reports/daily/INDEX.tsv` 取歷史報告（新到舊） |
| `read_report(name, base_dir)` | `str \| None` | 安全讀單一報告檔 |
| `list_backtest_results(base_dir, limit=30)` | `list[dict]` | 由 `artifacts/reports/backtest/INDEX.tsv` 取回測結果摘要（新到舊，C3-2a） |
| `read_backtest_result(name, base_dir)` | `dict \| None` | 安全讀單一回測結果 JSON（防穿越、限 `.json`，C3-2a） |

`build_dashboard` 回傳鍵：`account_id, date, cash, run_status, positions[], monitored_count, pnl[], fills_today[], next_signals[], events[], reconcile_ok, reconcile_detail`。各區段對應模板同名表格。

**股票名稱顯示**：`positions / fills_today / next_execution / corporate_actions / events` 每筆皆帶 `name`（中文股名），來源為共用對照 `src/contracts/stock_names.py` 的 `stock_name(symbol)`（查無回 `""`；cli 與 service/web 共用，service 不反向依賴 cli）。持倉表為獨立「名稱」欄；其餘 4 表於代號後接 `.tag-muted` 小字。**資產配置圓環圖 label 維持代號**（不改）。

> 內部 `_run_status/_positions/_pnl_by_strategy/_fills_today/_next_signals/_events` 為私有查詢，新增區段時於此擴充。

`build_capital_overview` 回傳鍵：`account_id, as_of_date, net_principal, cash, positions_value, total_equity, total_return, return_pct(float|None), any_stale, allocation[]`（金額皆整數元；`allocation` 每塊 `{label, symbol, value, ratio, kind:"cash"|"position", stale}`）。`market_repo`（`SqliteMarketBarRepository`）由**路由注入**（比照 `exit_strategy_ids`），connection 生命週期仍由路由 own。

---

## 6. 重要行為與資料語意

- **預設日期 = 今天**（2026-06-15 改）。日期欄反映當下；**只影響三塊日期範圍面板**——`run_status`、`fills_today`、`events`（交易日盤前自然為空，屬正確）。cash/positions/pnl/monitored/reconcile 為即時狀態、`next_execution` 已與日期解耦，皆不隨日期變動。模板另傳 `today`，檢視非今天時於頂部 hint 標示「目前檢視 X，今天是 Y」。
- **`next_execution`（下次執行）**：**與 `view_date` 解耦**，查 `signal_date == (SELECT MAX(signal_date) FROM signal_bundles)`，即**最近一批產生的訊號**（其 target 為下一交易日的執行計畫）。故交易日盤前也看得到「下次開盤要執行什麼」，不因日期欄停在他日而變空。最新批次若無 item（如 06-12 收盤未產生訊號）則顯示「無」，屬真實狀態。（取代舊 `next_signals`／標題「明日將執行訊號」。）
- **`events` 中文化**：`_events` 每列附 `event_label`，由 `EVENT_TYPE_LABELS` 將 `execution_events.event_type`（授權閘門代碼，來源 `engine._validate_buy_gate`）譯為中文，未收錄者退回原碼；模板以「中文（原碼小字 tag）」呈現，`detail`（含 bundle id／sha256）留為技術明細。
- **`monitored_count` / 持倉「監控」欄**：監控對象 = **非長期、且 strategy_id 屬具 exit 區塊的策略**（即 `load_exit_managed_definitions` 範圍，與 `RiskExitEngine`／CLI 一致）；MANUAL 與無 exit 區塊的策略皆排除，顯示 `—`。server 會把該集合（`_exit_strategy_ids()`）注入 `build_dashboard`，`dashboard._positions` 據以判定（非僅排除 MANUAL/長期）。go-live 前既有持倉多為 MANUAL → 監控常為 0，屬正常。**（2026-06-14 已修）** `record-fill --strategy-id` 可將手動成交歸入策略 bucket，**歸入具 exit 區塊的策略後**該部位即納入 risk_exit 監控、於此欄打勾；既有 MANUAL 部位若要納入須以正確 strategy_id 重新補錄（或日後提供轉歸工具）。
- **`reconcile`（對帳）**：`build_dashboard` 以 `_reconcile_summary()` 把 `projection.reconcile()` 的 dict 轉 `{ok, code, detail_zh}`；通過/失敗皆有中文說明，失敗時 `detail_zh` 含具體差異數字（現金帳本 vs 餘額、成交淨額 vs 庫存、策略桶）。模板卡片顯示 badge + `detail_zh`，並附一行「對帳在比對什麼」。（仍保留 `reconcile_ok` 布林鍵向後相容；`projection.reconcile()` 契約為 `{"status":"RECONCILE_OK"}`，勿用真值判斷。）
- **`capital`（資金總覽 / 資產配置，C3-1）**：`build_capital_overview` 算市值 = `int(qty*close//10000)`；當日無 bar 以持倉均價 fallback 並標 `stale`（卡片顯示「估算」badge、圓環不缺塊）。圓環**含現金一塊**，各塊比例分母 = 總權益，與卡片一致、加總相等。`return_pct` 分母 = 淨投入本金（`INITIAL_DEPOSIT` 合計，DIVIDEND 不計），分母 0 顯示「—」。跨策略持有同一 symbol 在圓環聚合為一塊。資料以 `<script type="application/json" id="allocData">{{ ...|tojson }}</script>` 傳前端，`dashboard-charts.js` 讀取渲染（無 JS / 載入失敗則圖不顯示、其餘照常）。
- **回測結果 / equity curve（C3-2a）**：`app backtest run` 跑完後由 `write_backtest_result` 落檔至 `artifacts/reports/backtest/<strategy_id>_<run_id>.json`（含 `equity_curve` 逐日 `{date, cash, position_value, equity}` 與 `statistics`），並追加 `INDEX.tsv`、更新 `LATEST.txt`，CLI 印 `BACKTEST_RESULT_PATH=<path>`。`statistics.profit_factor` 若為 `inf`（無虧損交易）落檔時轉成 `null`，detail 頁顯示「∞」。`/backtests/{name}` 以 `<script type="application/json" id="equityCurveData">{{ result.equity_curve|tojson }}</script>` 傳前端，`backtest-charts.js` 畫三條線（總權益/現金/持倉市值）。本機制只多寫結果檔，不改 DB schema、不影響 daily run 或 B1 影子驗證。

---

## 7. 設定（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `TRADING_WEB_HOST` | `127.0.0.1` | 綁定位址（僅 nginx 反代，毋須對外） |
| `TRADING_WEB_PORT` | `8800` | 連接埠 |
| `TRADING_WEB_ROOT_PATH` | `/trading` | 子路徑前綴。**直連 :8800 測試時設為空字串**，否則連結會指向 `/trading/...` |

DB 路徑來自 `AppSettings().trading.database_path`（沿用核心設定）。

---

## 8. 執行與部署

**開發/手動**：
```bash
scripts/web_ui.sh                       # 預設 127.0.0.1:8800, root_path /trading
# 直連手機測試（繞過 nginx）：
TRADING_WEB_HOST=0.0.0.0 TRADING_WEB_ROOT_PATH="" scripts/web_ui.sh   # 開 http://<IP>:8800/
```

**常駐（正式）**：systemd，見 [`deploy/README.md`](../../deploy/README.md)。改 code 後 `sudo systemctl restart trading-web`（uvicorn 未開 `--reload`）。

**nginx 子路徑**：見 [`docs/operations/nginx-trading.conf.sample`](../operations/nginx-trading.conf.sample)。`proxy_pass` 結尾斜線剝掉 `/trading` 前綴 → app 於 `/` 接收；手機網址 `http://<主機IP>/trading/`。

**前置**：專案 `.venv`（`uv venv && uv pip install -r requirements.txt -r requirements-web.txt`）。Web 額外依賴在 `requirements-web.txt`。

---

## 9. 擴充指南

**加一個新面板（純讀）**：
1. 在 `dashboard.py` 寫一個 `_xxx(conn, account_id, d)` 查詢函式，回傳 list/dict。
2. 在 `build_dashboard` 的回傳 dict 加上該鍵。
3. 在 `dashboard.html` 新增對應 `<section>` 表格。
4. 在 `test_web_server.py` 補渲染斷言。

**加圖表**：service 回乾淨資料 dict → 模板以 `<script type="application/json">{{ data|tojson }}</script>` 傳遞（autoescape 下 `|tojson` 會跳脫 `<` 等，安全嵌入 `<script>`）→ `static/js/*.js` 用 Chart.js 渲染。Chart.js 一律 **self-host**（區網無外網），`{% block scripts %}` 內**先**載 `chart.umd.min.js`**再**載 render script；render script 對缺 canvas/資料/`Chart` 未定義時靜默 return（優雅降級）。base.html 已備 `{% block head %}`/`{% block scripts %}` 供插入。

**加一個新頁面**：於 `server.py` 加 `@app.get(...)` 路由 → 呼叫 service → `templates.TemplateResponse(request, "x.html", {...})`（**注意新版 Starlette 簽名為 `(request, name, context)`**）。模板 extends `base.html`，連結用 `{{ base }}/...`。

**加寫入操作（分期 3，需謹慎）**：
1. ~~先抽 `services/<域>.py` 寫入 service~~ → **已備（2026-06-15）**：`services/trade_write.py` 封裝 record_fill / reject_signal / un_reject_signal，內部走既有 projection／signal_items SQL（**不繞過**）；成功回結構化 dict、驗證錯誤拋 `TradeWriteError`，presentation 由呼叫端渲染。CLI 三個 handler 已改為薄消費者。
2. 路由用 `POST`，搭配 `python-multipart` 表單；成功後 redirect 回儀表板（PRG 模式）。呼叫 `trade_write.*`，connection 生命週期由路由 own（比照讀取側）；`TradeWriteError` 渲染為表單錯誤、`ValueError`（SELL_WITHOUT_POSITION / LONG_TERM_PROTECTED）渲染為操作失敗。record_fill 的 `exit_strategy_ids` 由路由以 `load_exit_managed_definitions` 算出傳入（長期/MANUAL 免查），`monitor_status` 枚舉自行映射文案。
3. ~~先處理 record-fill 策略歸屬待修項~~ → **已完成（2026-06-14）**：CLI `record-fill --strategy-id` 已可歸策略並驗證；Web「設定成交結果」表單應提供策略下拉（來源 `registry.PARAMS_MODELS`）並沿用同一驗證／監控語意。
4. go-live 影子驗證通過前不啟用。

**換樣式（neumorphism）**：只動 `static/style.css` 與模板 class，資料/路由層不變。

> **⚠️ 改 CSS 後看不到效果＝瀏覽器快取**：`base.html` 以 `style.css?v={{ static_v }}` 引用，`static_v`＝`style.css` 的 mtime（`server.py` 啟動時算進 `templates.env.globals`）。改 CSS 後 **需重啟 web 服務**讓 `static_v` 更新、`?v` 變動自動破快取（免使用者硬刷新）。曾踩：新模板加了 `.ext-icon` 但舊 CSS 被永久快取 → 無尺寸約束的 SVG 撐滿整格（2026-06-26）。
>
> **代號連 Yahoo**：模板用 macro `symcell(symbol, name)`（dashboard.html 內）渲染「可點代號＋灰字名稱」，連結 `tw.stock.yahoo.com/quote/{symbol}`（**無後綴，Yahoo 自動解析上市/上櫃**，勿硬加 `.TW`／`.TWO`——live `app.db` 的 `exchange` 欄全標 'TSE' 不可靠）。

---

## 10. 測試

`tests/unit/test_web_server.py`（FastAPI `TestClient`）：healthz、儀表板渲染、預設日期落最近 run、報告清單、404、目錄穿越阻擋、回測結果清單/詳情（C3-2a）。執行：`.venv/bin/python -m pytest tests/unit/test_web_server.py -q`。

> 測試以 env `TRADING_WEB_ROOT_PATH=""` 匯入 server，避免子路徑前綴干擾斷言。

---

## 11. 已知限制 / 待辦

- 資金卡 + 資產配置圓環（C3-1）、回測權益曲線（C3-2a）已具備；**實盤/影子每日權益曲線（C3-2b）待後續**（需新增 `equity_snapshots` 表並改動 `DailySimulationRunner.run_daily()`，留待 B1 影子驗證完成後再做）。
- 無自動刷新（資料一天一更新，手動重整即可）。
- 無認證（信任區網；如需，nginx basic-auth 一行）。
- ~~持倉監控欄受 record-fill 全歸 MANUAL 限制，待修。~~ → **已修（2026-06-14）**：`record-fill --strategy-id` 可歸策略並納入監控；既有 MANUAL 部位需重新補錄方納入。
- 寫入操作、CLI TUI、neumorphism 風格為後續分期。

完整待辦與分期進度見 [`todo.md`](todo.md)。

相關記憶：`ui-requirements`、`record-fill-strategy-attribution`、`ceo-review-golive-2026-06-14`、`discord-alert-config`。
