# 待辦 / Roadmap

專案待辦的單一來源（cross-cutting）。決策脈絡見 [`engineering-log.md`](engineering-log.md)、UI 細節見 [`ui-development.md`](ui-development.md)。
狀態：⬜ 未開始 ／ 🔄 進行中 ／ ✅ 完成。

> 更新慣例：完成的項目標 ✅ 並保留一行（含 commit / 日期）；新待辦補到對應分區。

---

## A. 立即（本週）

- ⬜ **A1 record-fill 可歸策略**（讓持倉「監控」欄會打勾的關鍵）。現況手動補錄一律歸 `MANUAL`、結構性排除於 risk_exit；需支援指定 `--strategy-id`，使依策略訊號手動下的單可納入監控與策略別損益。詳見記憶 `record-fill-strategy-attribution`。→ 跨「資料正確性」與「UI 寫入」兩塊的前置。
- ⬜ **A2 systemd 常駐安裝**：`deploy/README.md` 的 sudo 安裝需在**自己的終端機**執行（harness 的 `!` 無法輸入 sudo 密碼）。裝完 `systemctl status trading-web` 應 active。
- ⬜ **A3 影子先行開跑**：**2026-06-15（週一）收盤後**起，每交易日跑 `scripts/shadow_daily.sh`，用儀表板逐日核對（runbook「上線 Gate」有清單）。

## B. go-live 收尾（gate，源自 2026-06-14 CEO review / HOLD SCOPE）

- ✅ git tag `v0.1.0-pre-golive` 回滾錨點（commit fc432d4）
- ✅ 清理殘留 `active-approval.json`（commit fc432d4）
- ✅ 每日影子報告 + `shadow_daily.sh`（commit a833141）
- ⬜ **B1 影子先行 ≥3 個交易日**並人工核對（= A3）
- ⬜ **B2 cron 失敗告警接 Discord**：`shadow_daily.sh` 的 `⚠ALERT` 發到 daily 頻道。channel_id 走 **gitignored 本地設定**、token 走 `~/.openclaw/.env`，**絕不入 git**（見記憶 `discord-alert-config`）。使用者指定此項最後處理。
- ⬜ **B3 開實單起步**：B1+B2 全綠後，建議路徑 C（先單策略 `trend_breakout`、限額小量），再放第二支。

詳見記憶 `ceo-review-golive-2026-06-14`。

## C. UI 分期

- ✅ 分期 1：唯讀 Web 儀表板（commit 2dd94cd）＋常駐 unit（0d7dc04）＋預設日期/移除底部（2ad8aed）
- ✅ service 層讀取側雛形（`services/dashboard.py`）
- ⬜ **C1 分期 2：service 層寫入側 + 寫入操作**：設定成交結果（含策略歸屬，依賴 A1）、拒絕訊號（`reject-signal`）。寫入必走既有 engine/projection 邏輯（不可繞過）。**go-live 影子驗證通過前不啟用。**
- ⬜ **C2 分期 4：CLI TUI（Textual）**：SSH/本機操作中心，與 Web 共用 service 層。
- ⬜ **C3 圖表**：權益曲線等視覺化。
- ⬜ **C4 neumorphism 風格**：套用指定設計稿（只動 `static/style.css` 與模板 class，不動資料層）。非現在。

詳見 [`ui-development.md`](ui-development.md) §1、§9。

## D. 資料正確性

- ⬜ **D1 record-fill 策略歸屬**（= A1，資料側）：影響停損涵蓋與損益歸因準確度。
- ⬜ **D2 除權息 / 公司行動追蹤**：計畫書 §三.3 自承的時間炸彈，會使均價、`highest_close`、停損基準失真。**任何持倉標的的除息日前必須處理。**

## E. 技術債

- ⬜ **E1 拆 `src/cli.py`**：1853 行 / 62 指令、churn 之冠（CEO review 第一順位債）。建議按領域（signal/trade/report/market）拆子模組；與 C1 的 service 層抽取可一併進行。

## F. 流程 / 工具

- 🔄 **F1 施工記錄維護**：每次開發變更記於 `engineering-log.md`。
- ⬜ **F2 施工記錄封裝為 skill / MCP**：待 UI 建置完成後，自動產出開發記錄（目前手動 md）。詳見記憶 `ui-requirements`。

---

### 關聯記憶
`ceo-review-golive-2026-06-14`、`multi-strategy-rollout-state`、`ui-requirements`、`record-fill-strategy-attribution`、`discord-alert-config`。
