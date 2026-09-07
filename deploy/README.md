# 部署：唯讀 Web 儀表板常駐 (systemd)

`trading-web.service` 讓儀表板開機自啟、崩潰自動重啟，以 `hom` 身分用專案 `.venv` 執行，
綁 `127.0.0.1:8800`，僅由 nginx 子路徑 `/trading` 對外。

## 安裝（需 sudo，在你自己的終端機執行）

```bash
# 1. 先停掉手動 nohup 起的實例，避免 8800 衝突
pkill -f "uvicorn src.web.server" 2>/dev/null

# 2. 安裝並啟用
sudo cp /home/hom/services/stock/tw-day-trading/deploy/trading-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-web

# 3. 確認
systemctl status trading-web --no-pager
curl -s -o /dev/null -w "本地: %{http_code}\n" http://127.0.0.1:8800/
```

手機（區網）：`http://192.168.50.109/trading/`

## 日常管理

```bash
sudo systemctl restart trading-web      # 改 code 後重啟
sudo systemctl stop trading-web
systemctl status trading-web
journalctl -u trading-web -f            # 看即時日誌
```

### Web 變更的完整部署檢查

修改 `src/web/` 或 Web 使用的 Python service/read model 後，以下三步視為同一個不可拆開的部署動作：

```bash
sudo systemctl restart trading-web.service
curl -fsS http://127.0.0.1:8800/healthz
curl -fsS -o /dev/null -w "首頁: %{http_code}\n" http://127.0.0.1:8800/
```

兩個 endpoint 都成功、首頁顯示 `200` 才算完成。`systemctl status` 顯示 active 只代表 process 存活，不代表首頁可渲染。

> **已知故障模式（2026-09-07）**：更新模板與 dashboard read model 後若未重啟，Jinja 可能讀到磁碟上的新模板，但 uvicorn 仍使用記憶體內的舊 Python module。結果是模板期待新欄位、舊 dict 未提供，首頁直接回 `500 Internal Server Error`。因此 Web 變更不能只 checkout/pull；一定要 restart + 首頁 readback。
>
> 更新程式碼後需 `systemctl restart trading-web` 才會生效（uvicorn 未開 --reload）。
> 前置條件：專案 `.venv` 已建（`uv venv && uv pip install -r requirements.txt -r requirements-web.txt`）。
