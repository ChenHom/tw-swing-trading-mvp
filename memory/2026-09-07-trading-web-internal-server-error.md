# DEBUG REPORT — trading-web 首頁 500

- **Symptom:** 開啟 `/trading/` 回傳 `500 Internal Server Error`；`trading-web.service` 本身仍為 active/running。
- **Root cause:** 常駐 uvicorn process 自 2026-08-17 啟動後未重啟，但 2026-09-06 已更新首頁模板與 dashboard read model。Jinja 會讀到磁碟上的新模板，舊 Python process 卻仍使用記憶體內的舊 `build_dashboard()`，因此模板讀取 `d.completed_trade_history` 時收到缺少該鍵的舊 dict，拋出 `jinja2.exceptions.UndefinedError`。
- **Evidence:** journal traceback 指向 `src/web/templates/dashboard.html` 的 `d.completed_trade_history`；service 啟動時間早於 commit `c94e819`。目前原始碼的 `build_dashboard()` 已包含該鍵。
- **Fix:** 不需修改原始碼；執行 `sudo systemctl restart trading-web.service`，使 Python process 與模板載入同一版本。
- **Regression test:** `tests/unit/test_completed_trade_web.py` 與 `tests/unit/test_completed_trades_service.py`。
- **Verification:** 修復前相關測試 `19 passed in 1.42s`；有 sudo 權限的使用者重啟後確認頁面恢復正常。
- **Related:** `deploy/README.md` 已明載改 code 後必須重啟，因 uvicorn 未開 `--reload`。
- **Status:** DONE — 根因已排除；部署規則已同步寫入 `AGENTS.md`、`deploy/README.md` 與 engineering log，要求後續 Web 變更完成 restart + readback。
