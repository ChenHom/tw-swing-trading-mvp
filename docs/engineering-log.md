# 施工記錄 (Engineering Log)

> 本檔記錄每一次有意義的開發變更：**動哪裡、為什麼要動、會怎麼動、為什麼這樣動、考慮了什麼、優缺點、結果**。
> 目的是讓未來的自己（或協作者/AI）能還原當時的決策脈絡，而不只看到 git diff。
>
> **格式約定**：每筆一個 `##` 區段，由新到舊（最新在上）。每筆至少包含：背景/觸發、變更內容、為什麼這樣做、考慮的替代方案與取捨、優缺點、驗證結果、關聯 commit。
>
> **未來規劃**：本「施工記錄」流程於 UI 建置完成後，封裝為 skill 或 MCP，讓每次開發自動產出記錄。目前先以本 md 手動維護。

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
