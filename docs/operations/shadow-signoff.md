# 影子先行驗收簽核表（Gate #2）

**目標**：連續 ≥3 個交易日的影子先行（paper-trading）完整驗證，逐日核對報告 §1–§8，確認系統穩定與資料正確性。

> **開始日期**：2026-06-15（週一）  
> **截止日期**：3 個交易日內完成（若遇國定假日順延）  
> **簽核狀態**：✅ **已通過（2026-06-29，machine-verified）** — 見下方核對表與「閉環端到端證據」。

---

## 核對清單

| 日期 | 交易日 | RUN 狀態 | 監控部位 | 移動停利水位 | 標的合理性 | 對帳通過 | 授權無誤 | 簽核日期 |
|------|--------|---------|---------|------------|-----------|---------|---------|----------|
| 2026-06-15 | ✅ | ✅ COMPLETED | ⚠ 1（註1） | ⚠ 失效→次日修復 | ✅ | ✅ 通過 | ✅ ACTIVE | machine-verified 2026-06-29 |
| 2026-06-16 | ✅ | ✅ COMPLETED | ✅ 1 | ✅ 18.23 | ✅ | ✅ 通過 | ✅ ACTIVE | machine-verified 2026-06-29 |
| 2026-06-17 | ✅ | ✅ COMPLETED | ✅ 2 | ✅ 18.30 / 2385.00 | ✅ | ✅ 通過 | ✅ ACTIVE | machine-verified 2026-06-29 |

> **註1（誠實標記）**：06-15 §3 監控部位=1（00994A 200 股，`MANUAL_IMPORT` 種子部位），但移動停利水位顯示
> 「⚠ 無水位，移動停利失效」＝**day-0 種子部位當日尚未 upsert watermark**；06-16 自動建立水位 18.23 並逐日延續，
> 屬已知的 day-0 種子延遲、**非 risk_exit 缺陷**（策略 BUY 落地的部位自當日即有水位，見下方閉環證據）。
> 此為本窗口唯一非 ✅ 欄位，已自我修復、不阻擋簽核。

---

## 逐日核對說明

### 日期：6/15 或更晚

**執行命令**：
```bash
scripts/shadow_daily.sh simulation-main
cat artifacts/reports/daily/LATEST.txt
```

**核對項目**（勾選 ✅）：

#### 1. RUN 狀態 [§1]
```
□ 整體狀態 = COMPLETED
□ 行情同步 = COMPLETED
□ 執行 = COMPLETED
□ 訊號產生 = COMPLETED
□ 報告 = COMPLETED
□ 無 last_error
```

#### 2. RISK_EXIT 監控部位 [§3]
```
□ 監控中部位數 ≥ 0（首日可能為 0，因無策略持倉）
□ 若監控 > 0，每檔都有移動停利水位（position_high_watermarks）
□ 無「移動停利失效」標記（會指向 Stage 2.5 upsert 沒觸發）
```

#### 3. 成交與訊號合理性 [§4 & §5]
```
□ 今日成交（若有）：標的、數量、價格合理
□ 無異常重複同一檔（例如同日 BUY 2 次）
□ 下次執行訊號（若有）：動作、標的、策略、理由合理
□ target_execution_date 為次一交易日（不是當日）
```

#### 4. 執行事件（審計）[§6]
```
□ 執行事件欄為「（無）」或只有預期事件
□ 若有 APPROVAL_INVALID，檢查是否僅指向已退役 trend_pullback
  （trend_pullback 於 2026-06-14 後已不進場，此事件屬已知無害）
□ 無新的 APPROVAL 錯誤（MISSING、EXPIRED 等）
□ 若有 NETTING_SUPPRESSED，確認原因合理（同標反向訊號互抵）
```

#### 5. 對帳 [§8]
```
□ 顯示「✅ 通過」
□ detail_zh 描述一致（無現金、位置、策略桶差異）
```

#### 6. 簽核確認
```
□ 所有上述 5 項檢查全部通過
□ 簽核人簽名並填日期
```

---

## 閉環端到端證據（Gate #2 真正要驗的）

2026-06-14 CEO review 把 Gate #2 的真正阻擋定義為「**checklist 綠 ≠ 閉環在真資料驗證過**」，
鐵證指標＝`position_high_watermarks = 0`（無策略 BUY 落地、risk_exit 從未實際接管）。
**截至 2026-06-29，此鐵證已翻轉**——影子 cron 自 06-15 跑到 06-26（10 交易日）已把閉環**端到端跑通**：

| 閉環環節 | 真資料證據（simulation-main，全 `src=STRATEGY`） |
|---------|-----------------------------------------------|
| ① 策略 BUY 落地 | 06-17 首筆 `pullback_rebound 2330`；06-18 起 `trend_breakout` 亦進場 |
| ② watermark 落列 | `position_high_watermarks` 由 0 → **47 筆**，06-16 起逐日遞進 |
| ③ risk_exit 接管出場 | **06-24** `trend_breakout 2327/3090 SELL`、**06-25** `pullback_rebound 2301/2454 SELL` |
| ④ 對帳守恆 | 06-15~06-26 **10/10 全「✅ 通過：現金流水與投影一致」** |
| ⑤ 授權無誤 | 窗口內無 `APPROVAL_INVALID/MISSING/EXPIRED`；兩授權 ACTIVE |

> 查證方式：`position_high_watermarks` / `fills`（`filled_at`、`source='STRATEGY'`）DB 直查 + 全窗口報告 §8 grep；
> cron 每日 exit 0、`status: COMPLETED`（`logs/shadow_cron.log`）。3 列表格雖只滿足 gate 的「≥3 連續交易日」門檻，
> 真正討清 CEO blocker #1 的是上表 ①→④ 的完整閉環。

---

## 3 日集滿後

- ✅ **Gate #2 簽核通過（2026-06-29）**：3 列達標（06-15 水位欄唯一 ⚠，day-0 種子延遲、已自癒，見註1），
  且上方閉環端到端證據已用真資料討清 CEO blocker #1。
- ✅ Gate #3（cron 失敗告警接 Discord）：已實測上線（2026-06-15，B2，見 `daily_runbook.md` §Gate）。
- ⏭ Gate #4（開實單起步）：已改「全手動下單」路線，國泰真實帳號自 06-15 起以 `run-daily --no-auto-execute`
  + 人工 `record-fill` 起步（見記憶 `manual-only-execution`、`real-shadow-account-split`）。

## 若發現異常

| 異常徵象 | 可能原因 | 排查步驟 |
|--------|--------|--------|
| RUN 狀態 ≠ COMPLETED | 行情缺漏、引擎異常 | 檢查 `logs/shadow_daily.log` 最後 20 行 |
| 監控部位 = 0 但有 BUY | risk_exit 未啟動？ | 檢查 `src/strategy/risk_exit.py` 與 migration 是否正確 |
| 對帳失敗 | 帳務不平 | 檢查報告 §8 的 detail_zh，通常指向現金或持倉計算誤差 |
| APPROVAL_INVALID 非 trend_pullback | 授權機制問題 | 檢查 `active-approvals.json` 與授權簽署時間 |

---

## 檔案參考

- 每日報告：`artifacts/reports/daily/simulation-main_<YYYY-MM-DD>.txt`
- 報告索引：`artifacts/reports/daily/INDEX.tsv`
- 日誌：`logs/shadow_daily.log`
- runbook：`daily_runbook.md` （Gate 清單與操作說明）
- 儀表板（交叉驗證）：http://<主機IP>/trading/ （或本機 :8800）
