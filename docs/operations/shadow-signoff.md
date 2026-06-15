# 影子先行驗收簽核表（Gate #2）

**目標**：連續 ≥3 個交易日的影子先行（paper-trading）完整驗證，逐日核對報告 §1–§8，確認系統穩定與資料正確性。

> **開始日期**：2026-06-15（週一）  
> **截止日期**：3 個交易日內完成（若遇國定假日順延）

---

## 核對清單

| 日期 | 交易日 | RUN 狀態 | 監控部位 | 移動停利水位 | 標的合理性 | 對帳通過 | 授權無誤 | 簽核日期 |
|------|--------|---------|---------|------------|-----------|---------|---------|----------|
| 2026-06-15 | ✅ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| 2026-06-16 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| 2026-06-17 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |

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

## 3 日集滿後

- **集滿 3 列**且**全部欄位 ✅**：**Gate #2 簽核通過**。
- 推進 Gate #3（cron 失敗告警接 Discord，見 `daily_runbook.md` §Gate）。
- 推進 Gate #4（開實單起步，先單策略小額）。

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
