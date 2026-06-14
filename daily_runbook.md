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

---

## 上線 Gate（go-live 前必過，源自 2026-06-14 CEO review / HOLD SCOPE）

> 背景：多策略系統已建好、103+ 測試綠、檢核清單（TSE≥60、雙策略授權、migration）機械上已完成。
> **但閉環從未在真資料上跑出一筆策略 BUY**（`position_high_watermarks` 曾為 0 筆即鐵證）。
> 開實單前，先用影子先行把閉環驗一遍、把停損的觀測與告警補上。

| # | 項目 | 指令 / 驗收 | 狀態 |
|---|---|---|---|
| 0 | 回滾錨點 | `git tag` 已釘 `v0.1.0-pre-golive`；任何壞日 `git checkout v0.1.0-pre-golive` | ✅ 已完成 |
| 1 | 殘留授權清理 | 已移除指向退役 trend_pullback 的 legacy `active-approval.json` | ✅ 已完成 |
| 2 | **影子先行 ≥3 個交易日** | 連跑 `scripts/shadow_daily.sh`，逐日核對報告 §3〜§8（見下） | ⬜ 待執行 |
| 3 | cron 失敗告警 | `shadow_daily.sh` 已在 run-daily 非 0 時輸出 `⚠ ALERT`；接上你的告警管道（Telegram/mail） | ⬜ 待接線 |
| 4 | 開實單起步 | 全綠後建議先單策略小額（路徑 C），再放第二支 | ⬜ |

**影子先行每日跑法（cron 可掛）：**

```bash
# 收盤後執行：run-daily（paper）+ 產生每日報告 + 失敗告警
scripts/shadow_daily.sh simulation-main            # 日期預設今天
scripts/shadow_daily.sh simulation-main 2026-06-12 # 指定日期
```

**crontab 範例（影子先行，自 2026-06-15 週一起，交易日收盤後自動跑）：**

> README §5 已記錄通用 cron 機制（包裝腳本 / 直接 crontab）；以下為**影子先行專用**的排程，
> 與正式 `run_daily_sim.sh` 並存。非交易日（週末/國定假日）由 run-daily 內的交易日曆自動空轉，
> 報告會記下當日無資料，不影響帳務。

```cron
# crontab -e 後貼入（請用絕對路徑）
CRON_TZ=Asia/Taipei
# 影子先行：週一~週五 14:00（收盤後 13:30 資料沉澱）跑 paper run-daily + 每日報告
0 14 * * 1-5 /home/hom/services/stock/tw-day-trading/scripts/shadow_daily.sh simulation-main >> /home/hom/services/stock/tw-day-trading/logs/shadow_cron.log 2>&1
```

> * `shadow_daily.sh` 已在 run-daily 非 0 時於 stdout/log 輸出 `⚠ ALERT`；要真正收到通知，需把該鉤子接上 Telegram/mail（go-live gate #3）。
> * 最新報告路徑隨時可由 `cat artifacts/reports/daily/LATEST.txt` 取得。

**每日報告落檔位置（A2 產物）：**

| 檔案 | 內容 |
|---|---|
| `artifacts/reports/daily/<account>_<date>.txt` | 當日報告本體 |
| `artifacts/reports/daily/LATEST.txt` | 最新一份報告的絕對路徑（單行，cron / 人工快速定位） |
| `artifacts/reports/daily/INDEX.tsv` | 歷史索引：`date / account / status / path` |

> 此目錄為執行時生成物，已列入 `.gitignore`，不入版控。
> 也可單獨重產報告（不重跑模擬）：`python3 scripts/daily_report.py --date <YYYY-MM-DD>`。

**影子先行每日人工核對清單（對著報告看）：**

1. **§1 RUN 狀態** 全 `COMPLETED`，無 `last_error`。
2. **§3 RISK_EXIT 監控中部位**：一旦有策略 BUY，監控數應 > 0，且**每檔都有移動停利水位**（若顯示「移動停利失效」＝ §2.2 水位 upsert 沒觸發，停損從第一天就壞）。
3. **§4 今日成交 / §5 明日訊號**：BUY/SELL 標的與理由合理、無異常重複。
4. **§6 執行事件**：`NETTING_SUPPRESSED` / `APPROVAL_*` 是否符合預期（退役 trend_pullback 的舊 `APPROVAL_INVALID` 屬已知無害）。
5. **§8 對帳** 顯示 `✅ 通過`。

詳細的初始化、訊號查詢、重置與 cron 排程說明見 [README.md](README.md) 第 4、5 節。
go-live review 全文與兩個 🔴 見記憶 `ceo-review-golive-2026-06-14`。
