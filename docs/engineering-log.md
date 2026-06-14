# 施工記錄 (Engineering Log)

> 本檔記錄每一次有意義的開發變更：**動哪裡、為什麼要動、會怎麼動、為什麼這樣動、考慮了什麼、優缺點、結果**。
> 目的是讓未來的自己（或協作者/AI）能還原當時的決策脈絡，而不只看到 git diff。
>
> **格式約定**：每筆一個 `##` 區段，由新到舊（最新在上）。每筆至少包含：背景/觸發、變更內容、為什麼這樣做、考慮的替代方案與取捨、優缺點、驗證結果、關聯 commit。
>
> **未來規劃**：本「施工記錄」流程於 UI 建置完成後，封裝為 skill 或 MCP，讓每次開發自動產出記錄。目前先以本 md 手動維護。

---

## 2026-06-14 ｜ 儀表板預設日期改最近 run 日 + 移除底部字串

**背景／觸發**：使用者反映明日訊號、執行事件、risk_exit 監控、持倉監控欄皆空。診斷 live DB 後確認**非 bug**：(1) 預設日期是今天（週日 06-14 無 run）→ 全空；(2) 8 筆訊號全屬已退役 `trend_pullback`，分布於 06-10/06-11，06-12 無訊號、僅 1 筆 APPROVAL_INVALID 事件；(3) 9 筆持倉全 MANUAL → risk_exit 監控結構性為 0。

**怎麼動**：
- `dashboard.latest_run_date()`：取最近有 daily_run 的日期；`server.index` 無 `view_date` 時預設落於此（避免假日/未跑日全空）。
- 移除 `base.html` 底部「唯讀儀表板 · 區網單人使用」字串與對應 `.foot` CSS。
- 新增測試 `test_dashboard_defaults_to_latest_run_date`。

**為什麼這樣動**：不捏造資料；以「預設落在最近有資料的日期」改善空白觀感，其餘空段為 go-live 前的真實狀態（策略尚未實際交易、持倉全 MANUAL）。MANUAL 不受監控連動 [[record-fill-strategy-attribution]] 待修項——修好後手動成交可歸策略、納入 risk_exit。

**驗證**：115 passed；live 預設日期落 2026-06-12，APPROVAL_INVALID 事件顯示、底部字串移除；歷史訊號可由日期選 06-11 檢視（4 筆 trend_pullback）。

---

## 2026-06-14 ｜ Web 儀表板常駐 (systemd) + 區網上線

**背景／觸發**：phase 1 儀表板已用 nohup 跑起、手機經 `http://192.168.50.109/trading/`（nginx→127.0.0.1:8800）可見，但 nohup 重開機不會自啟。長期工具需常駐。

**怎麼動**：新增 `deploy/trading-web.service`（systemd system unit，User=hom、用 `.venv` 執行 uvicorn、綁 127.0.0.1:8800、root_path=/trading、Restart=on-failure、開機自啟）+ `deploy/README.md` 安裝/管理說明。

**為什麼這樣動**：
- system unit（非 user unit）→ 不需登入即於開機啟動，符合區網常駐主機。安裝需一次 sudo。
- 綁 127.0.0.1：8800 對外被防火牆擋，僅 nginx 反代，攻擊面最小。
- 直接 ExecStart uvicorn（不經 shell）較易被 systemd 追蹤；`web_ui.sh` 保留供手動執行。

**考慮點／取捨**：未開 `--reload`（正式服務不需熱載，改 code 後 `systemctl restart`）。最小安全強化（NoNewPrivileges/PrivateTmp）；未加 ProtectSystem 以免擋到 sqlite 檔存取。

**驗證**：安裝由使用者以 sudo 執行（harness 的 `!` 無法輸入 sudo 密碼）；本機 `curl 127.0.0.1:8800/` 200、手機 `/trading/` 已確認可見。

---

## 2026-06-14 ｜ 唯讀 Web 儀表板 第一版（FastAPI + Jinja，分期 1）

**背景／觸發**：UI 分期第一步 —— 唯讀儀表板，服務手機/區網查看與每日影子核對。使用者確認：子路徑 `ip/trading`、預設畫面、不認證、預設帳戶 `simulation-main`、頁面載入即時性。

**怎麼動**：
- `src/application/services/dashboard.py`：**service 層讀取側雛形**（純讀 projection/SQLite，回傳結構化 dict）。提供 `build_dashboard` / `list_accounts` / `list_reports` / `read_report`。
- `src/web/server.py`：FastAPI app，`root_path` 由 `TRADING_WEB_ROOT_PATH`（預設 `/trading`）控制；路由 `/`（儀表板,可切帳戶/日期）、`/reports`、`/reports/{name}`(防目錄穿越)、`/healthz`。
- 模板 `src/web/templates/*`（base/dashboard/reports）+ `static/style.css`（基本款、行動友善、單欄優先）。連結以 Jinja 全域 `base`=root_path 前綴，子路徑與本機直連皆正確。
- `scripts/web_ui.sh`：uvicorn 啟動（綁 127.0.0.1:8800）。`docs/nginx-trading.conf.sample`：子路徑反代範例（proxy_pass 結尾斜線剝前綴 + X-Forwarded-Prefix）。
- `tests/unit/test_web_server.py`：TestClient 冒煙（healthz、儀表板渲染、報告清單、404、目錄穿越阻擋）。

**為什麼這樣動**：
- 讀取邏輯先抽 service 層（不直接在路由查 DB），為日後寫入操作與 CLI TUI 共用鋪路；寫入暫不做（go-live 安全）。
- 唯讀、不認證（信任區網、不對外）；DB 每請求開關。
- 風格先基本款，neumorphism 之後再換（不影響資料層）。

**過程中抓到的問題**：
- 新版 Starlette `TemplateResponse` 簽名改為 `(request, name, context)`；舊式 `(name, {"request":...})` 會把 context dict 當成 template name → `unhashable type: dict`。已改新簽名。
- 測試種子帳戶 balance 與 cash_ledger 不一致會被 reconcile 正確擋下（印證 reconcile 有效）；改種一致帳戶。

**優缺點**：優 — 零寫入風險、service 層起步、手機/區網可用、報告可線上瀏覽。缺 — 尚無圖表（權益曲線待後續）、無自動刷新（需手動重整,符合一天一更新的資料節奏）。

**驗證**：全套件 **114 passed**；對 live `data/app.db` TestClient 渲染 2026-06-12，現金 5,987、MANUAL 持倉、各區段皆正確輸出；`/healthz` ok、`/reports` 200。

---

## 2026-06-14 ｜ 建立 uv venv + 修正依賴宣告缺漏（pytz）

**背景／觸發**：UI/Web 需引入相依套件，使用者決定改用 venv 並偏好 uv。需先確認建 venv 對現行指令的影響。

**影響檢查（重要）**：`scripts/run_daily_sim.sh` 與 `shadow_daily.sh` 已寫「有 `.venv/bin/python` 則優先用、否則 python3」。故一旦建 `.venv`，每日流程會**自動改用 venv**，venv 必須裝齊全部依賴（含 shioaji），否則日跑會壞。今天為週末非交易日，是安全切換+驗證窗口。

**怎麼動**：
- 新增 `requirements-web.txt`（fastapi/uvicorn/jinja2/python-multipart/httpx），與核心 `requirements.txt` 分離。
- `uv venv --python python3`（解析到 CPython 3.11.13）；`uv pip install -r requirements.txt -r requirements-web.txt`。
- venv 跑全測試**揪出隱性依賴**：`pytz` 被 `src/market_data/provider.py` 執行期使用，卻**從未宣告於 requirements**（先前靠系統環境碰巧有）。補進 `requirements.txt`。

**考慮點／取捨**：
- venv 解析到 **Python 3.11.13 + shioaji 1.5.3**（系統原為 3.10 + shioaji 1.3.2），因 requirements 用 `>=` 未鎖版。風險為「新版/新 Python 破壞既有行為」→ 以全測試驗證收斂。
- 依賴分兩檔（核心 vs web）：優點是只跑交易核心者可略過 web stack；缺點是多一個檔。
- 未鎖版（沿用專案既有 `>=` 風格）：優點維護簡單；缺點重建可能再漂版，日後可用 `uv pip compile` 產 lock。

**優缺點**：優 — 環境隔離、uv 安裝快、順手揪出 pytz 宣告缺漏（潛在 Shioaji 同步炸點）。缺 — Python/套件版本較系統新，需持續以測試把關。

**驗證**：`.venv` 下 `pytest` **109 passed**；`-m app portfolio reconcile` 通過。`.venv` 已在 `.gitignore`。

---

## 2026-06-14 ｜ Web/CLI UI 架構與技術選型（討論定案中）

**背景／觸發**：使用者要為系統做長期使用的 UI（單人、區網主機、手機經內網查看、SSH/本機操作），且需要寫入操作（設定手動成交結果、拒絕訊號等）。主機已有 nginx 並接了其他服務。未來想套用 neumorphism 風格（https://github.com/joshhu/uitest 02-neumorphism），但非現在。

**決策 1 — UI 寫入 ⇒ 必須抽 service 層**
- 為什麼：UI 寫入若直接改 DB，會繞過 FIFO 隔離、冪等、授權閘門等 invariant。寫入必須走與 CLI 相同的、已驗證的邏輯。
- 怎麼做：從 1853 行 `src/cli.py` 抽出 `src/application/services/` 純函式層（輸入參數 → 操作 projection/engine → 回傳結果），CLI / Web / 未來 TUI 共用。
- 取捨：一次還掉 CEO review 列的第一順位技術債；但工程量較大，需測試保護。`src/application/reporting/daily_report.py` 已是純讀層雛形。

**決策 2 — Web 技術：FastAPI + Jinja + HTMX（反轉先前的 Streamlit 推薦）**
- 初始傾向 Streamlit（開發最快）。**反轉原因**：使用者明確表示未來要套特定 neumorphism HTML/CSS 風格。
- 比較：
  - Streamlit：開發最快，但深度自訂 CSS/版面困難，難還原指定設計稿；接 nginx 需 websocket proxy 設定。
  - FastAPI + Jinja + HTMX：對 markup/CSS 完全掌控（neumorphism HTML 可直接模板化）；標準 ASGI，nginx 反向代理最單純；單人填表單 HTMX 原生支援。工程量略大。
- 結論：未來客製化需求 + 已有 nginx ⇒ 選 FastAPI 系。優：長期可控、風格自由、部署標準。缺：比 Streamlit 多前期工。

**決策 3 — 分期：先唯讀、後寫入（go-live 安全）**
- 為什麼：go-live 影子驗證未過前，任何能改帳務的新入口都是新風險面。
- 順序：(1) 唯讀 Web 儀表板（純讀 projection，零風險，立即服務手機/區網查看與每日影子核對）→ (2) 抽 service 層 → (3) 補寫入操作 + CLI TUI。
- 取捨：唯讀畫面之後沿用、不白做；最快見效又不破壞 go-live 安全。

**決策 4 — 部署走既有 nginx**；**風格**先用基本款，neumorphism 列入後續（非現在）。

**狀態**：技術方向討論中，待使用者確認分期與技術棧後動工。關聯記憶 `ui-requirements`、`record-fill-strategy-attribution`。

---

## 2026-06-14 ｜ 修正 portfolio reconcile 誤報失敗（commit 0c991dd）

**背景／觸發**：使用者要求跑 `portfolio reconcile`，輸出「Reconciliation failed! Errors found: - status」並 exit 1。直接以 Python 呼叫 `projection.reconcile()` 確認回傳為 `{"status": "RECONCILE_OK"}` ⇒ 帳務實際一致，是 **CLI 誤報**。

**根因**：`projection.reconcile()` 成功時回傳 `{"status":"RECONCILE_OK"}`（非空 dict），但 `cmd_portfolio_reconcile` 仍用舊契約 `if not errors:` 判斷。非空 dict 恆為真 ⇒ 永遠走 failed 分支，並 `for err in errors` 印出 dict 的 key `status`。

**怎麼動**：`src/cli.py` 判斷改為 `not result or (isinstance(result, dict) and result.get("status")=="RECONCILE_OK")`；新增回歸測試 `tests/unit/test_cli_commands.py::test_cmd_portfolio_reconcile_reports_success_on_ok`（OK 須印 successful、不得出現 failed）。

**為什麼這樣動**：最小變更修正契約不一致，不改動 `reconcile()` 既有回傳（其他呼叫端如 `daily_report.py` 已用對的契約）。

**考慮點／取捨**：本可改 `reconcile()` 回傳空 dict 表成功，但那會牽動所有呼叫端、風險較大；改 CLI 判斷影響面最小。

**優缺點**：優 — 一行修好、有回歸測試、低風險。缺 — 同類「dict 真值誤判」可能散落他處（未全面掃，列為待觀察）。

**為什麼重要**：runbook 叫操作者每週跑 reconcile，卻永遠回報失敗 ⇒ go-live 危害（操作者無法信任對帳輸出）。

**驗證**：live `data/app.db` reconcile → "Reconciliation successful"；全套件 109 passed。

---

## 2026-06-14 ｜ 每日影子報告產生器 + 影子先行流程（commit a833141）

**背景／觸發**：CEO review（HOLD SCOPE）指出兩個 go-live 阻擋項 —（1）多策略閉環從未在真資料跑過一筆策略 BUY（`position_high_watermarks=0` 為證）；（2）cron run FAILED 無告警。且 `run-daily` 的 Stage 4「Reporting」原為空殼（只翻狀態、不產報告）。

**怎麼動**：
- 新增 `src/application/reporting/daily_report.py`：純讀 DB/projection 的文字報告產生器，涵蓋 RUN 狀態、**RISK_EXIT 監控部位數+移動停利水位**（補 §7 觀測缺口）、成交、明日訊號、執行事件審計、策略別損益、對帳。
- `scripts/daily_report.py`：CLI 入口，落檔並記錄路徑（`<acct>_<date>.txt` / `LATEST.txt` / `INDEX.tsv`），stdout 末行印 `REPORT_PATH=`。
- `scripts/shadow_daily.sh`：cron 可掛 = run-daily(paper) + 報告 + run 非 0 時輸出 `⚠ ALERT`。
- 5 個單元測試；`.gitignore` 忽略生成物；runbook 補「上線 Gate」與核對清單。

**為什麼這樣動**：
- 報告產生器**刻意獨立於 cli.py god-file**（純讀、無副作用、可測），作為未來 service/讀取層的雛形。
- 用獨立 script + shell wrapper 而非塞進 cli.py，避免加重 churn 之冠的 god-file。
- 系統本質是 paper-trading（FakeBroker、Hard Boundary 永不串實盤），故 `run-daily` 本身即「影子」，無需另建 dry-run 機制。

**考慮點／取捨**：曾考慮把報告直接寫進 runner Stage 4（每次 run-daily 自動產），但那較侵入、需改 runner 並補測試；先以獨立 script 解耦，日後可再由 Stage 4 呼叫。

**優缺點**：優 — 零風險、即時補上觀測、報告 §6 已在 live 揪出退役 trend_pullback 的舊 APPROVAL_INVALID（印證價值）。缺 — 報告非由 run-daily 自動觸發，需 shell wrapper 串接。

**驗證**：108 passed；對 live `data/app.db` 實測報告產出正常。

---

## 2026-06-14 ｜ go-live 回滾錨點 + 殘留授權清理（commit fc432d4）

**背景／觸發**：CEO review 列上線前置 —（1）高/不可逆決策卻無 git tag 回滾錨點；（2）磁碟殘留 legacy `active-approval.json`（指向已退役的 trend_pullback）。

**怎麼動**：`git rm artifacts/approvals/active-approval.json` 並 commit；打 annotated tag `v0.1.0-pre-golive`。

**為什麼這樣動**：先驗證 `src/approval/store.py:29-39` — active map（`active-approvals.json`）存在時 legacy 單檔**永不被讀**，故刪除對 runtime 無害，僅清掉誤導除錯者的殘留。tag 釘在清理後的乾淨狀態，作為任何壞日的回滾點。

**優缺點**：優 — 零風險、回滾有依據。缺 — 無。

**驗證**：working tree clean；tag 可見。
