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
| `src/application/services/__init__.py` | services 套件 |
| `src/web/server.py` | FastAPI app 與路由 |
| `src/web/templates/base.html` | 版型骨架（topbar/nav，`{{ base }}` 連結前綴） |
| `src/web/templates/dashboard.html` | 主儀表板 |
| `src/web/templates/reports.html` | 歷史報告清單 |
| `src/web/static/style.css` | 基本款樣式（行動友善、單欄優先） |
| `scripts/web_ui.sh` | 手動啟動（uvicorn） |
| `deploy/trading-web.service` | systemd 常駐 unit |
| `deploy/README.md` | 常駐安裝/管理說明 |
| `docs/nginx-trading.conf.sample` | nginx 子路徑反代範例 |
| `tests/unit/test_web_server.py` | TestClient 冒煙測試 |

---

## 4. 路由（src/web/server.py）

| 方法 | 路徑 | 說明 | Query |
|---|---|---|---|
| GET | `/` | 主儀表板 | `account`（預設第一個帳戶）、`view_date`（預設**最近有 run 的日期**，見 §6） |
| GET | `/reports` | 歷史每日報告清單（讀 `INDEX.tsv`） | — |
| GET | `/reports/{name}` | 單一報告純文字（**防目錄穿越**，僅取檔名、限 `.txt`） | — |
| GET | `/healthz` | 健康檢查，回 `ok` | — |

**子路徑前綴**：`root_path` 由環境變數 `TRADING_WEB_ROOT_PATH`（預設 `/trading`）決定，注入 Jinja 全域 `base`。模板所有內部連結以 `{{ base }}/...` 產生，確保 nginx 子路徑與本機直連皆正確。

---

## 5. 讀取 service API（dashboard.py）

| 函式 | 回傳 | 用途 |
|---|---|---|
| `list_accounts(conn)` | `list[str]` | 帳戶下拉來源（`cash_balances`） |
| `latest_run_date(conn, account_id=None)` | `str \| None` | 最近有 `daily_run` 的日期（預設日期用） |
| `build_dashboard(conn, projection, account_id, view_date)` | `dict` | 組整頁資料（見下） |
| `list_reports(base_dir, limit=30)` | `list[dict]` | 由 `artifacts/reports/daily/INDEX.tsv` 取歷史報告（新到舊） |
| `read_report(name, base_dir)` | `str \| None` | 安全讀單一報告檔 |

`build_dashboard` 回傳鍵：`account_id, date, cash, run_status, positions[], monitored_count, pnl[], fills_today[], next_signals[], events[], reconcile_ok, reconcile_detail`。各區段對應模板同名表格。

> 內部 `_run_status/_positions/_pnl_by_strategy/_fills_today/_next_signals/_events` 為私有查詢，新增區段時於此擴充。

---

## 6. 重要行為與資料語意

- **預設日期 = 最近一個有 `daily_run` 的日期**（非今天）。避免假日/收盤前開啟整頁空白。週一起每日 run 後會自動前進。
- **`next_signals`**：以 `signal_bundles.signal_date == view_date` 取當日**收盤產生、target 為次一交易日**的訊號（語意為「明日將執行」）。
- **`monitored_count` / 持倉「監控」欄**：僅計**非 MANUAL 且非長期**的策略持倉（risk_exit 監控對象）。MANUAL 部位結構性排除，故顯示 `—`。go-live 前持倉多為 MANUAL → 監控常為 0，屬正常；待 [`record-fill` 可歸策略](record-fill-strategy-attribution 的修正) 後手動成交方可納入監控。
- **`reconcile_ok`**：`projection.reconcile()` 回 `{"status":"RECONCILE_OK"}` 視為通過（注意此契約，勿用真值判斷）。

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

**常駐（正式）**：systemd，見 [`deploy/README.md`](../deploy/README.md)。改 code 後 `sudo systemctl restart trading-web`（uvicorn 未開 `--reload`）。

**nginx 子路徑**：見 [`docs/nginx-trading.conf.sample`](nginx-trading.conf.sample)。`proxy_pass` 結尾斜線剝掉 `/trading` 前綴 → app 於 `/` 接收；手機網址 `http://<主機IP>/trading/`。

**前置**：專案 `.venv`（`uv venv && uv pip install -r requirements.txt -r requirements-web.txt`）。Web 額外依賴在 `requirements-web.txt`。

---

## 9. 擴充指南

**加一個新面板（純讀）**：
1. 在 `dashboard.py` 寫一個 `_xxx(conn, account_id, d)` 查詢函式，回傳 list/dict。
2. 在 `build_dashboard` 的回傳 dict 加上該鍵。
3. 在 `dashboard.html` 新增對應 `<section>` 表格。
4. 在 `test_web_server.py` 補渲染斷言。

**加一個新頁面**：於 `server.py` 加 `@app.get(...)` 路由 → 呼叫 service → `templates.TemplateResponse(request, "x.html", {...})`（**注意新版 Starlette 簽名為 `(request, name, context)`**）。模板 extends `base.html`，連結用 `{{ base }}/...`。

**加寫入操作（分期 3，需謹慎）**：
1. 先抽 `services/<域>.py` 寫入 service，內部呼叫既有 engine/projection 邏輯（**不可繞過**）。
2. 路由用 `POST`，搭配 `python-multipart` 表單；成功後 redirect 回儀表板（PRG 模式）。
3. 先處理 [`record-fill` 策略歸屬](record-fill-strategy-attribution) 待修項。
4. go-live 影子驗證通過前不啟用。

**換樣式（neumorphism）**：只動 `static/style.css` 與模板 class，資料/路由層不變。

---

## 10. 測試

`tests/unit/test_web_server.py`（FastAPI `TestClient`）：healthz、儀表板渲染、預設日期落最近 run、報告清單、404、目錄穿越阻擋。執行：`.venv/bin/python -m pytest tests/unit/test_web_server.py -q`。

> 測試以 env `TRADING_WEB_ROOT_PATH=""` 匯入 server，避免子路徑前綴干擾斷言。

---

## 11. 已知限制 / 待辦

- 無圖表（權益曲線待後續）。
- 無自動刷新（資料一天一更新，手動重整即可）。
- 無認證（信任區網；如需，nginx basic-auth 一行）。
- 持倉監控欄受 [`record-fill` 全歸 MANUAL](record-fill-strategy-attribution) 限制，待修。
- 寫入操作、CLI TUI、neumorphism 風格為後續分期。

完整待辦與分期進度見 [`todo.md`](todo.md)。

相關記憶：`ui-requirements`、`record-fill-strategy-attribution`、`ceo-review-golive-2026-06-14`、`discord-alert-config`。
