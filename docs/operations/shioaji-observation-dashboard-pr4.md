# PR-4：唯讀盤中觀察 Web

狀態：OFFLINE_IMPLEMENTED / REALTIME_NOT_INTEGRATED（2026-10-08）

## 提供功能
- 依既有 FastAPI + Jinja 單站 `/trading/` 加一個 `/trading/intraday`，預設 feature flag OFF。JSON API 路徑為 `/api/intraday/status`、`/api/intraday/symbols`、`/api/intraday/{symbol}/snapshot?lot_type=BOARD|ODD`（經 root_path=/trading 映射）。
- `INTRADAY_DASHBOARD_ENABLED=1` 只允許讀本地 `data/intraday/dashboard.json`，**不會啟動 Collector、Shioaji 登入或交易委託**。
- 透過 `src/application/services/intraday_dashboard.py` 讀取 versioned snapshot。無檔案／損壞／過期／錯誤 lot／不可信 session／未驗證 heartbeat 都不得呈現可誤認為即時的價格。
- Web 每 30 秒重新讀本地快照，供內網研究觀察；訂閱整股 / 零股分開，五檔只顯示可見五層掛單，不等於已成交。
- 測試可透過 `python3 -m scripts.build_intraday_dashboard --ticks ... --books ... --health ... --output data/intraday/dashboard.json` 明確手動產生**離線**快照。腳本拒絕正規化失敗的 raw 資料，不會連永豐，也不會設定 systemd 或 cron。

## 未完成的即時整合（正式部署阻擋）
- 現有 PR-1 Collector 尚無經驗證的 SDK heartbeat／`trading_session_verified` 訊息，也沒有長時間穩定的 read-only snapshot publisher。現有 smoke 的 health 檔只會在結束時寫出 CLOSED，**不可能因為離線檔更新就宣稱正在提供經核實的即時行情**。
- 後續需將 heartbeat / quote session 驗證加入單一 Collector，在測試真實行情 SDK 版本相容與訂閱容量後，由獨立 Collector 原子更新 read-only snapshot；不能把瀏覽器／FastAPI 變成新的 SDK session。
- 依 D10 額外明確批准以前：**不得啟動真實 Shioaji 連線、日常 service / cron 或重啟真實 Web**。未完成 /healthz 和 /trading/ 實機確認前 PR-4 不可視作部署驗收完成。

## 隔離 CI
GitHub Actions 可在無券商憑證的環境執行 `tests/unit/test_intraday_dashboard.py`、`tests/unit/test_intraday_web_routes.py` 及 PR-1～PR-3 離線回歸；真實站台的 /healthz /trading/ 回歸需等使用者另行核准部署。
