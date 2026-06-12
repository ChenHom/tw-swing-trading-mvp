# 每日例行操作 Runbook

> 關鍵原則只有一條：`simulation run-daily` **固定收盤後（13:45 之後）跑、一天一次**，其餘皆為檢視或例外處理。
> 若在盤中誤跑，當日日 K 會以盤中快照寫入；收盤後執行 `python3 -m app market sync` 重抓即可覆蓋修正（upsert）。

## 每日例行指令

| 時段 | 指令 | 用途 |
|---|---|---|
| 盤前 | `python3 -m app signal list` | 檢視今天將被執行的訊號（昨日收盤產生的 BUY/SELL bundle），最後人工把關 |
| 盤前（可選） | `python3 -m app trade reject-signal --signal-id <ID>` | 否決不想執行的個別訊號（被拒訊號今天不會成交） |
| 盤前（可選） | `python3 -m app approval list` | 確認各策略授權有效未過期（MISSING/INVALID 的策略 BUY 會被擋；SELL 不受影響） |
| 盤中 | （無） | 日 K 波段架構，盤中不跑任何指令；券商手動下單請收盤後補錄 |
| 盤後 13:45+ | `python3 -m app simulation run-daily` | **核心指令**：同步日 K（含 TSE 指數）→ 執行今日到期 bundle（含 risk_exit 停損）→ 更新移動停利水位 → 產生明日訊號（risk_exit → trend_breakout → pullback_rebound）→ 報告。冪等，失敗可原地重跑 |
| 盤後（手動單補錄） | `python3 -m app trade record-fill ...`（長期持有加 `--long-term`） | 補錄手動成交事實，自動歸入 MANUAL bucket、不受 risk_exit 管理 |
| 盤後（檢視） | `python3 -m app report pnl --by-strategy` | 策略別損益歸因（各策略與 MANUAL 持倉表現） |
| 盤後（檢視） | `python3 -m app signal list` | 檢視剛產生、明日將執行的訊號 |
| 每週（可選） | `python3 -m app portfolio reconcile` | 帳務對帳：現金流水、FIFO 持倉、策略 bucket 三方一致性檢查 |

## 例外處理

| 情境 | 指令 | 說明 |
|---|---|---|
| 當日流程出錯重跑 | `python3 -m app simulation run-daily` | 冪等設計：已完成的子階段自動跳過，不會重複成交或扣款 |
| 行情缺漏（WAITING） | 等資料源補齊後重跑 run-daily | 嚴禁跳過缺漏日；系統會停在 `WAITING_MARKET_DATA` |
| 盤中誤跑 run-daily | 收盤後 `python3 -m app market sync` | 覆蓋修正當日的部分日 K（執行階段不受影響，開盤價正確） |
| 歷史行情回補 | `python3 -m app market backfill --calendar-days 120` | 收盤後執行；個別商品上市前無資料會自動跳過，不阻斷整日同步 |

## 一次性前置（新策略上線前）

```bash
# 1. 回補行情：大盤 60MA 濾網需要 ≥60 個交易日的 TSE 指數資料
python3 -m app market backfill --calendar-days 120

# 2. 每個進場策略各簽發並啟用一份授權（valid-from 不指定時為當下時刻，
#    當天 preflight 會顯示 NOT_YET_VALID，建議明確指定當日 00:00）
python3 -m app approval create \
  --strategy config/strategies/<strategy_id>.yaml \
  --valid-from <YYYY-MM-DD>T00:00:00+08:00 \
  --expires-at <YYYY-MM-DD>T23:59:59+08:00 \
  --output artifacts/approvals/approval-<strategy_id>-<date>.json
python3 -m app approval activate artifacts/approvals/approval-<strategy_id>-<date>.json

# 3. 確認狀態為 ACTIVE
python3 -m app approval list
```

詳細的初始化、訊號查詢、重置與 cron 排程說明見 [README.md](README.md) 第 4、5 節。
