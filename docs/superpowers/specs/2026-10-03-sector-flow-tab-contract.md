# 族群資金頁籤：dashboard JSON 合約

> 2026-10-03 定稿。producer（`src/application/reporting/sector_flow_dashboard.py`）與 web（`src/web/static/js/sector_flow.js`）共用這份格式，要改時兩邊必須一起改。

- **檔案**：`data/sector_flow/dashboard.json`。`data/` 已在 .gitignore。
- **產生**：`python3 -m app report sector-flow-dashboard`，完全離線，用暫存檔加 rename 原子寫入。每個平日 22:00 由 `scripts/sync_sector_flow.sh` 更新。
- **讀取**：`GET {base}/api/sector-flow` 原樣回傳這個檔；檔案不存在時回 404 JSON `{"error": ...}`。「族群資金」或「大戶持股」頁籤第一次被打開時才會去抓；兩個頁籤共用這份資料，只抓一次。

## 格式

```jsonc
{
  "schema_version": 1,
  "end_date": "2026-10-03",            // 要求的結束日，cron 當天
  "dates": ["2026-05-26", "..."],      // 最近 ≤90 個「有資料的交易日」，由舊到新
  "taiex": [48475.74, null, "..."],    // 與 dates 對齊的加權指數收盤；缺值為 null
  "windows": [20, 30, 60, 90],
  "status": "ok" | "degraded" | "blocked",
  "warnings": ["..."],                 // 報表 warnings，最多 20 條
  "taxonomy_snapshot_date": "2026-10-03",
  "holidays": ["..."],                 // 最近 10 個 dates_without_data（四個來源皆無資料，視為休市）
  "rows": [{
    "name": "半導體業",
    "broad": false,                    // 大類（電子工業 / 化學生技醫療）
    "idx": "半導體類指數" | null,       // 對應的 TWSE 類股指數名稱
    "idxv": [1532.13, "..."] | null,   // 與 dates 對齊的類股指數收盤；沒有對應指數時為 null
    "daily": [123456789, "..."],       // 與 dates 對齊的每日法人淨額估算（元，整數）
    "f": ["..."], "t": ["..."], "dl": ["..."]   // 外資 / 投信 / 自營商每日淨額估算（元，整數）
  }],
  "stocks": {                          // key 是視窗天數的字串
    "20": {
      "半導體業": {
        "members": 209,
        "in":  [{"sym": "2454", "name": "聯發科", "amt": 54460000000, "share": 20.7, "f": 123000, "t": 4000, "dl": -500}],
        "out": ["... 同上，amt 為負"],
        "subs": [{"name": "半導體業", "n": 99, "amt": -2332848128,   // 只有大類有，其他為 []
                  "in": ["... 同上個股格式"], "out": ["..."]}]           // 該子類「在這個大類裡」的成員排名
      }
    },
    "30": {}, "60": {}, "90": {}
  },
  "large_holder": {                    // 「大戶持股」頁籤用。TDCC 大戶（分級 12–15，單一帳戶 400 張以上），週資料，固定近 20 日，不隨族群資金的視窗變
    "status": "ok" | "insufficient_data" | "schema_error",
    "reason": null | "fewer_than_two_eligible_snapshots" | "no_snapshot_in_window" | "...",
    "prior": "2026-09-24", "latest": "2026-10-02",   // 最新兩期週快照（status 依這兩期判定）
    "snapshots": 6,                    // 快取中 ≤ end_date 的週快照數
    "window": 20, "start": "2026-09-03",               // 視窗交易日數與起日（dates[-20]）
    "weeks": ["2026-09-04", "...", "2026-10-02"],      // 視窗內每期週快照，各與前一期比較，由舊到新
    "rows": [{                         // 依 amt 由大到小
      "name": "半導體業", "broad": false,
      "wk": [1200000000, null, "..."], // 與 weeks 對齊的當週 Σ(大戶股數變化 × 當週收盤價)，元；與前一期相隔超過兩週等無法比較時為 null
      "amt": 43730000000,              // 20 日累計 = wk 非 null 值加總
      "up": 96, "down": 92,            // 20 日內大戶股數淨增加／淨減少的檔數
      "missing": 0,                    // 有缺收盤價的週、不列入前 5 名的檔數
      "in":  [{"sym": "6488", "name": "環球晶", "amt": 5100000000, "shares": 1200000, "pp": 0.85}],  // 20 日估算金額增加前 5（shares、pp 也是 20 日加總）
      "out": ["... 同上，amt 為負"]                                                                  // 20 日估算金額減少前 5
    }]
  }
}
```

## 規則

- **視窗天數**：視窗 N 的實際天數是 `min(N, len(dates))`。`stocks[N]` 是對這段日期**單獨跑一次** `build_sector_flow_report(..., max_days=None)` 的 `category_detail`（取前 5 名），所以和同日期區間的 `report sector-flow` 完全一致。
  - 不要改成「分段算再加總」：缺價股票只會被部分計入，占比和子類檔數都會偏掉。
- **單位**：`f` / `t` / `dl` 在 `rows` 裡是**元**，在 `stocks` 裡是**股**，web 顯示時除以 1000 換成張。
- **占比**：`share` 是該股占同一邊合計的百分比，取一位小數。
- **缺價**：視窗若因價格覆蓋率不足改用 `net_shares` 排名，`amt` 為 null，web 顯示「—」。
- **金額與族群**：金額都是估算值（法人淨股數 × 收盤價），不可稱為精確資金流。族群會重疊，不可跨族群加總。
- **類股指數對應**：依序嘗試以下三種名稱，比對對象是 MI_INDEX 標題含「價格指數(臺灣證券交易所)」的那張表。類股指數只含上市股票，族群金額含上櫃。
  1. `name + "類指數"`
  2. 去掉結尾「工業」再加 `"類指數"`
  3. 去掉結尾「業」再加 `"類指數"`
- **子類排名**：`subs[].in/out` 是該子類在這個大類裡的成員（例如近 20 日「電子工業 › 半導體業」99 檔，幾乎都是上市股），**不是**同名頂層族群（頂層「半導體業」近 20 日有 212 檔，約一半是上櫃）。FinMind 給上櫃股的分類幾乎沒有「電子工業」標籤，所以兩者成分股不同。「（僅大類）」也有自己的排名。
- **大戶**：`shares` 單位是股（顯示除以 1000 為張），`pp` 是大戶占集保比例的百分點變化。每週只計入「前後兩期都有、且在 `dates[0]`..該週法人資料中出現」的股票。增資、減資、合併也會讓大戶股數跳動。
  - 每週的數字來自 `build_sector_flow_report(start=dates[0], end=該週, large_holder_stocks=True, taxonomy_date=end_date)`：收盤價用該週（含）以前最後一個交易日，產業分類固定用 `end_date` 那份，所以族群不會因為週次不同而換成員。
- **status**：取主報表（`dates[0]`..`end_date`）的狀態。`degraded` 的原因寫在 `warnings`，web 顯示在頁尾「資料警告」。
