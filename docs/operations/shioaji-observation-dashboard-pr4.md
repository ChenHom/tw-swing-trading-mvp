# PR-4：唯讀盤中觀察 Web

狀態：OFFLINE_IMPLEMENTED / REALTIME_NOT_INTEGRATED（2026-10-12 更新，已同步 PR-3 最新版本）

## 2026-10-12：PR-3 整合與 Dashboard 安全修正

- 原 PR-4 從舊 PR-3 建立，已用**非強制雙父節點 merge** 納入最新 PR-3 `ab308723`（含 `master` PR-1/2 P0/P1 修正）。保留 PR-4 歷史及 PR-3 新增的研究 CLI 端到端測試。PR-4 仍以 PR-3 為 base；僅有 **12 個 Dashboard 專屬檔案差異**。
- Web `OBSERVING` 現在必須同時符合：`state=HEALTHY`、真實且新鮮的 `last_heartbeat_at`、`transport_verification_status=EVENT_VERIFIED`、`connection_verified=true`、`gap_unresolved=false`、`trading_session_verified=true`、`data_quality_status=VERIFIED`、`market_completeness_status=VERIFIED` 以及有效 Session ID。只要缺一項，價格與五檔都隱藏。
- Tick／BidAsk 必須與相同股票／交易市場／BOARD 或 ODD／Collector Session 對應，且各自時間未過期；交叉五檔不呈現。
- **現在的 Shioaji 1.3.2 Smoke 只有 `simulation=true`、`collection_status=RAW_CAPTURED`，`transport_verification_status=UNVERIFIED`、`data_quality_status=UNVERIFIED`、`market_completeness_status=UNVERIFIED`，不能產生 `OBSERVING` 價格。** 即使手動以最新時間生成快照也不能越過驗證條件。
- GitHub Actions 已擴充為 Dashboard/API、所有 PR-1～3／P0/P1 離線回歸及原有 Web route 測試。`tests/unit/test_web_server.py` 自 2026-06 已有全模組 **skip**（TestClient runtime hang），CI 的 skipped 不等於 passed；另一組新 `test_intraday_web_routes.py` 使用真正的 TestClient 驗證頁面／JSON API，並另設 Raw JSONL → 離線 Publisher → 讀取器端到端測試。
- **沒有開啟 `INTRADAY_DASHBOARD_ENABLED`**、沒有服務更新／重啟／新排程／Broker 登入／交易操作，PR-4 仍 Draft。

## 2026-10-12 實際操作：先從已收到的 81 筆行情做離線預覽（無券商登入）

目前尚未授權持續行情蒐集或正式 Web 部署；**先只在** `~/services/stock/tw-day-trading-smoke` **隔離 worktree 執行**。不要進入、修改或重啟正式 `tw-day-trading` 主機服務：

```bash
cd ~/services/stock/tw-day-trading-smoke
git status --short
# 若上行有輸出，先處理本地未提交變更，不要硬切換。
git fetch origin feat/shioaji-observation-dashboard-pr4
git switch --detach origin/feat/shioaji-observation-dashboard-pr4

mkdir -p "$HOME/shioaji-2327-preview"
python3 -m scripts.build_intraday_dashboard \
  --ticks "$HOME/shioaji-2327-smoke/shioaji/ticks/2026-10-12/BOARD/2327.jsonl" \
  --ticks "$HOME/shioaji-2327-smoke/shioaji/ticks/2026-10-12/ODD/2327.jsonl" \
  --books "$HOME/shioaji-2327-smoke/shioaji/bidask/2026-10-12/BOARD/2327.jsonl" \
  --books "$HOME/shioaji-2327-smoke/shioaji/bidask/2026-10-12/ODD/2327.jsonl" \
  --health "$HOME/shioaji-2327-smoke/shioaji/collector_health.json" \
  --output "$HOME/shioaji-2327-preview/dashboard.json"
```

預期 `mode=OFFLINE_PUBLICATION_ONLY`、`symbols=2`。上述 snapshot 的 Collector 在原 Smoke 結束後是 `CLOSED` 且 Transport `UNVERIFIED`，所以**不會**顯示任何「即時可信價」；但可用獨立的歷史模式檢視 **BOARD 與 ODD 各自的最後 Tick 和五檔價格**，並始終顯示原本事件時間。

不啟動真正交易 Web，也可以先驗證資料：

```bash
python3 - <<'PY'
import os
from pathlib import Path
from src.application.services.intraday_dashboard import load_snapshot
p = Path.home() / "shioaji-2327-preview/dashboard.json"
result = load_snapshot(p, enabled=True, include_history=True)
print(result["status"], result["display_mode"], [(x["symbol"], x["lot_type"],
       bool(x["reference_tick"]), bool(x["reference_book"]),
       bool(x["tick"])) for x in result["symbols"]])
assert result["status"] == "CLOSED"
assert result["display_mode"] == "HISTORICAL_REFERENCE"
assert len(result["symbols"]) == 2
assert all(x["tick"] is None and x["book"] is None for x in result["symbols"])
PY
```

若想用**獨立的本機測試埠**看畫面（**不碰正式 Web service**），在同一個 smoke worktree 執行：

```bash
INTRADAY_DASHBOARD_ENABLED=1 \
INTRADAY_SHOW_HISTORICAL=1 \
INTRADAY_SNAPSHOT_PATH="$HOME/shioaji-2327-preview/dashboard.json" \
TRADING_WEB_ROOT_PATH="" \
python3 -m uvicorn src.web.server:app --host 127.0.0.1 --port 8765
```

在該台主機開啟 `http://127.0.0.1:8765/intraday`；如果程式在遠端主機上，需自行透過 SSH port-forward 後用本機瀏覽器開啟。最後 Ctrl-C 停止此**獨立前景測試程序**，不要使用或修改正式 `trading-web.service`。

## 有明確人工授權時才測試：既有 60 秒 Shioaji 模擬行情 + 同 Session Publisher

此選用擴充**不建立第二個 Shioaji Session**，只接現有 `ShioajiTickCollector` 的 `sink` 與 `book_sink`；收集仍限定 `simulation=True`、1..60 秒、交易日 08:30–13:40 Asia/Taipei、單一標的、原本雙閘門及容量保護。沿用已成功的 smoke 命令，額外加：

```text
--snapshot-output "$HOME/shioaji-2327-preview/current-dashboard.json" \
--snapshot-interval-seconds 5
```

Snapshot 由 CLI 控制執行緒每約 5 秒原子輸出，Callback/Worker 不做快照檔案 IO；最後一定輸出 Collector 停止後的健康狀態。故**短時間模擬測試結束後，歷史值可檢視，但不會停留在 `OBSERVING` 假裝持續更新**。此測試尚未在真實 SDK 執行；目前僅提供離線 CI 證據。

## 狀態契約與風險

- `status=OBSERVING` + `display_mode=LIVE_VERIFIED`：仍要求真正驗證的 transport、session、heartbeat、data_quality 與 market completeness，現有 Shioaji SDK smoke 不具備這些證據。
- `status=CLOSED`、`STALE` 或 `UNVERIFIED` + `display_mode=HISTORICAL_REFERENCE`：僅在明確開啟 `INTRADAY_SHOW_HISTORICAL=1` 且 snapshot 欄位合法時，用 `reference_tick`、`reference_book` 顯示歷史或未驗證參考行情；`tick`、`book` **仍然是 null**。
- `INTRADAY_DASHBOARD_ENABLED` 預設 OFF，`INTRADAY_SHOW_HISTORICAL` 預設 OFF，Snapshot Publisher 也預設 OFF。
- 無已驗證連續 heartbeat／獨立交易所完整性憑證時，不應為了能看見價而將健康欄位**手動設成 VERIFIED**。PR-3 研究樣本仍為 `EVIDENCE_PENDING`。
- 短時間 Smoke 快照 Publisher **不是**正式日常蒐集器，也不會自動部署／排程。

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
