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

> 更新程式碼後需 `systemctl restart trading-web` 才會生效（uvicorn 未開 --reload）。
> 前置條件：專案 `.venv` 已建（`uv venv && uv pip install -r requirements.txt -r requirements-web.txt`）。
