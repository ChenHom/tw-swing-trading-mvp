# 族群資金流開發紀錄（Sector Flow V1）

> 2026-10-02～10-03 在 `~/services/stock/tw-day-trading-lab` 開發，2026-10-03 搬到本 repo。
> 「lab 時期」各段是從 lab `docs/development-work.md` 原樣搬來的，裡面的指令和路徑是當時 lab 的寫法（例如 `PYTHONPATH=src python3 -m tw_day_trading_lab.cli`、`src/tw_day_trading_lab/sector_flow.py`）。本 repo 對應的指令與路徑見 `AGENTS.md`「族群資金流」一節。
> 之後的紀錄寫在文末「本 repo」各段，由舊到新。

# lab 時期（2026-10-02～10-03）

## 2026-10-02 Sector Flow V1

- 實作官方來源優先的族群資金流：TWSE T86 / MI_INDEX、TPEx 法人明細 /
  每日收盤、TDCC 持股分級；FinMind 只提供已快取的產業分類 metadata。
- `ingest sector-flow` 是唯一網路邊界；`report sector-flow` 可由 raw cache
  完全離線重播。沒有 Shioaji、下單、Telegram 或 GitHub publication side
  effect。
- 法人 / 外資 / 投信 / 自營商以 shares 精確加總；金額使用
  `net_shares_times_close`，報告禁止稱為精確資金流。
- 大戶使用 TDCC levels 12-15 的週 snapshot 差值；少於兩期時 fail closed
  為 `insufficient_data`。
- Code-complete 驗證與 2026-09-24～2026-10-01 實際 provider run 證據分開
  記錄。

### 2026-10-03 first data run evidence

- 官方 ingest 完成且 `failed=0`。有效交易日是 2026-09-24、09-29、09-30、
  10-01；09-25～09-28 四個 calendar dates 由兩市場一致回報無當日資料。
- 每個有效日四個來源均成功：TWSE T86 rows = 1,067 / 1,075 / 1,080 /
  1,074；TWSE closes = 1,079 / 1,084 / 1,087 / 1,085；TPEx institutional
  = 775 / 796 / 792 / 793；TPEx closes = 870 / 868 / 877 / 864。
- TDCC snapshot `2026-09-24` 有 50,269 normalized rows；因 repo 只有一期不
  計算 delta，輸出 `insufficient_data`。
- 報告價格 coverage 100%；FinMind taxonomy snapshot `2026-06-03` mapping
  7,445 / 7,452 rows（99.906%），所以整體 status 依契約為 `degraded`，未分類
  7 rows 沒有被偷偷丟棄。
- deterministic replay 兩次 checksum 一致。最終 artifacts：
  `reports/2026-09-24_2026-10-01-sector-flow.json`、
  `reports/2026-09-24_2026-10-01-sector-flow.md`。
- 最終 regression：418 tests passed；focused sector-flow 33 tests passed；
  `compileall` 與 `git diff --check` 通過。
- grill-me / code / data / security review 修正：既有 cache 重驗證、明確區分
  `ProviderNoData` 與 schema drift、TPEx 空 table、無成交價 placeholder、CLI
  handler 邊界、金額兩位小數，以及 TDCC 比例改名為各股票百分點變化加總。
  金額模式下流入 / 流出章節的歸類與排序也統一使用估算金額，不再混用股數
  符號。
  無未解 Critical / Important finding。
- 殘餘風險：taxonomy snapshot 比報告期間舊約四個月，且 7 rows 未分類；
  TDCC 尚不足兩期。報告可回答法人 / 外資族群淨買賣，但不能回答逐日大戶
  精確淨流入。

### 2026-10-03 implementation adjustment log

這一輪不是依 fixture 一次寫完；先完成 cache-first 主路徑，再以官方公開 API
實跑 2026-09-24～2026-10-01，依真實 payload 差異逐項收斂。調整紀錄如下：

| 發現 | 調整 | 落地 commit / 驗證 |
| --- | --- | --- |
| TWSE 無交易日可能回傳前一交易日資料，而不是空陣列 | 驗證 payload 內嵌日期；與 requested date 不同時分類為 `no_data`，禁止把舊資料算入當日 | `dfb2753`；09-25～09-28 正確排除 |
| TWSE / TPEx 無成交價分別可能是 `--` / `----` | 僅將官方無價 placeholder 視為不可估價列；其他非法數字仍 fail closed 為 schema error | `dfb2753`；有效日 price coverage 100% |
| TPEx `tables` 可能同時包含有效 table 與尾端空 `{}` | 只接受唯一具有 `fields` 與 `data` 的有效 table，不再假設陣列只有一個元素 | `dfb2753`；四個有效日 TPEx 來源皆成功 |
| CLI 新 handler 曾插入既有 `report close` 尾端 | 還原原 handler 邊界，sector-flow handler 保持獨立，補 CLI regression | `dfb2753`；完整 CLI suite 通過 |
| 已存在 cache 原本會直接跳過，無法偵測損壞或 schema 漂移 | skip 前先重讀並驗證 cache；無效 cache 重新抓取，並以 `ProviderNoData` / `ProviderSchemaError` 明確區分無資料與契約破壞 | `b1b1f2c`；重跑 ingest `failed=0` |
| TDCC 百分比欄位名稱容易被誤讀為單一比例 | 改名為 `sum_stock_percent_point_delta`，明示是各股票百分點變化的加總；不足兩期仍不產生數值 | `b1b1f2c`；本期輸出 `insufficient_data` |
| 浮點估算金額可能留下不穩定尾數 | 所有 TWD 估算值固定 round 至小數二位，確保離線 replay 可重現 | `b1b1f2c`；兩次 render checksum 一致 |
| 金額排名模式曾以「股數正負」篩選流入 / 流出，會使高價股族群被歸錯側 | `estimated_amount` 模式的篩選與排序統一使用估算金額；只有 fallback 才使用股數 | `734d551`；電子工業與半導體業正確列入金額淨流出 |

交付鏈為 `54ffc97`（官方來源 parser/cache）→ `2c727c3`（族群聚合）→
`6d0e059`（JSON / Markdown 報告）→ `d335dfb`（CLI）→ `5a7ffa6`
（契約與操作文件）→ `dfb2753` / `b1b1f2c` / `734d551`（真實資料與 review
修正）→ `4b011ce`（固定期間產物）。`master` 與 `origin/master` 最終皆為
`4b011ce34fb9bc289d432f581aa578b244f75f9d`。

最終產物 checksum：

- JSON：`4327908605fb031dd8ac289288cbc5525ec806da145076f27dd66b26bc4dda31`
- Markdown：`e2e812a23d581ed9b7063aae6c0a051a54a924ad5a9300f829ccaee2eb1ce418`

安全邊界維持不變：本次所有外部呼叫皆為 TWSE、TPEx、TDCC 公開資料的
read-only GET；沒有 Shioaji login / quote subscription / order / cancel，也沒有
Telegram 傳送或 GitHub report publication side effect。

## 2026-10-03 (3) Sector Flow 對抗審查與修正

### 審查

派一個不帶實作脈絡的 agent 對 Sector Flow V1 做對抗審查（只讀、不連網），結論由主對話逐條重現後才採信。

成立的部分：T86 5,337 列、TPEx 3,639 列的欄位對應皆與官方三大法人合計欄相符；獨立重算的股數完全一致、金額到分；單位、收盤價、placeholder、非交易日舊日期判定、TDCC 級距 12–15 都正確。

**嚴重問題：族群歸屬取決於 cache 列順序。** FinMind `TaiwanStockInfo` 有 858 檔普通股帶多個分類（例如 2330 同時是 半導體業 與 電子工業），`load_taxonomy` 只留最後一列。原交付版因此把 2330 / 2454 / 3711 歸進電子工業、半導體業裡沒有台積電；TWSE 與 TPEx 對同一族群用不同名稱，又讓一個族群拆成兩列。**原交付版的族群排名不可再引用。**

另外重現成立的：整天法人資料缺漏不會降級（`if not flows: continue` 在 incomplete 檢查之前）；金額模式下主要貢獻股仍以股數選排；期間 `covered_symbol_count` 是股票日不是檔數（塑膠工業 93 對約 25 檔）；四碼 TDR（91xx）未排除。

### 決策（使用者決定）

一檔股票有多個分類時**全部計入**，搭配三條防護：同義名稱合併、板別標籤與 catch-all 不當族群、報告明示族群重疊不可加總並標出大類。這比「只取細類」更貼近資料：多分類中有同義名、板別、真實的跨產業（建材營造＋紡織纖維、汽車工業＋電機機械），取細類對後者沒有定義。

### 實作

- `sector_flow.py`：`TaxonomyEntry.categories`、`CATEGORY_SYNONYMS`（8 組，見 `docs/data-contracts.md`）、`BOARD_LABELS`、`其他` 僅在唯一分類時保留、`BROAD_CATEGORIES = {電子工業, 化學生技醫療}` → `is_broad`；日、期間、大戶三處都展開到全部分類；主要貢獻股依排名方式選排；期間檔數改為不重複股票；新增 `category_overlap`、`dates_without_data`。
- 交易日判定：任一來源 `ok` 即交易日，其餘來源非 `ok` 即降級並寫出日期；全部 `missing` / `no_data` 的日期列入 `dates_without_data`、不降級（不建假日曆）。驗收時補上一條：**`schema_error` 一律降級**，原規格會把「四個來源都損壞」誤當成無資料日。
- `sector_flow_sources.py`：`COMMON_STOCK_RE` 排除 `91xx`。2026-06-03 snapshot 的 11 檔存託憑證全是 91xx，91xx 也沒有其他分類。
- `sector_flow_report.py`：開頭加重疊說明、大類標「（大類）」、列出無資料日期；degraded 報告不再印「無額外警告」。

每一項都先寫測試並確認在修正前失敗。期間檔數的測試由主對話另外拿 HEAD 版原始碼重跑，確認舊程式得到 `4 != 2`。全套 430 tests 在系統 python（shioaji 1.3.2）與 `.venv`（1.7.5）皆通過。

### 重產與獨立對帳

離線重產 2026-09-24～10-01 報告，另寫腳本以官方 parser + 獨立的分類正規化重算全部 38 個族群，股數與金額零差異，報告中不再出現已合併或排除的名稱。

| 族群 | 原交付版 | 修正後 |
|---|---|---|
| 半導體業 | -122.32 億 | **-318.07 億**（含台積電、聯發科、日月光） |
| 電子工業（大類） | -167.11 億 | -158.57 億 |
| 電子零組件業 | -2.73 億（淨流出） | **+134.49 億（流入第 1）** |

族群數由 45 降為 38（同義合併、TDR 與板別移除）。現行 checksum：JSON `4813eff16e65ddae0b119ffdcff75dccf89b6cac1c7ce5084e925b45e6453ea4`、Markdown `f58fb740ac44663dd0985549cbb8e96dfd384a4b09e9b5921adb467663a7a0ba`。

### 未修（審查成立但不在本次範圍）

已於 2026-10-03 (4) 修完，僅剩最後一條（全部 `missing` 的日期無法區分休市與未抓取）。

## 2026-10-03 (4) Sector Flow 審查剩餘項目依嚴重度修完

使用者決定族群歸屬維持「全部計入」，並要求把審查剩下的問題依嚴重度修完。每項都先寫測試並確認修正前失敗。

1. **價格覆蓋率規則不一致**：排名用「每日最低覆蓋率 ≥ 90%」、狀態卻用整體覆蓋率，一天 82.7%、整體 95.7% 時報告 `ok` 但排名默默改用股數。改成同一條 `prices_ok` 規則，低於門檻即降級並列出日期。
2. **TDCC 大戶段比錯東西**：只計入期間內有法人資料的上市櫃代號、只計入兩期都存在的代號（新上市不再整筆算成變化），排除數量輸出於 `excluded_symbols`；最新一期早於 `start_date` 7 天以上，或兩期相隔超過 14 天 → `insufficient_data`、不給數字。
3. **TDCC 壞 cache**：ingest 會重新解析既有 cache，壞掉或日期不符就用剛抓到的 payload 覆寫（`repaired_cache: true`）；報告端壞檔不再被默默略過，改為 `schema_error` 並降級。驗收時主對話補一條：**只有要比較的那兩期壞掉才算錯**，更舊的壞檔不影響，否則一個修不了的舊檔會讓大戶段永遠出不來。
4. **截斷偵測**：T86 `total`、TPEx 表格 `totalCount` 與實際列數不符即 `ProviderSchemaError`（已確認所有真實 cache 兩者相等）。
5. **壞 cache 不再崩潰**：parser 拒絕非物件 payload；捕捉 `ValueError`（涵蓋壞 JSON、壞 UTF-8 與 `ProviderSchemaError`）。
6. **CLI exit code**：`report` 在 `blocked` 時寫完檔再 exit 1；`ingest` 有失敗來源時 exit 1；`degraded` 仍 exit 0（現行報告因 0.1% 分類缺口本來就是 degraded）。
7. **Markdown**：金額排名下加註「淨股數與淨金額可能正負相反」，每日排行補最大流出表。

TPEx 表格日期與請求日不符維持 `schema_error`（無法離線驗證 TPEx 休市行為，fail closed）。

驗證：全套 450 tests 通過（系統 python 與 `.venv`）。重產報告 JSON 與修正前 byte-identical（股數、金額全未變動），Markdown 新 checksum `75e8ac1859d904bbab06fe5acf6199ec4ef8eb8164c16157c69b2f3fae06445f`。

剩餘：全部 `missing` 的日期仍無法區分休市與整天沒抓到（ingest 未寫 no-data 標記）。→ 2026-10-03 (6) 由使用者決定一律視為休市。

## 2026-10-03 (5) Sector Flow 族群細看

使用者問「族群內要細看」與「能看到排名前幾名的股」，並決定族群歸屬維持全部計入、細看大類時依子類分組。

- `report sector-flow` 新增 `--category NAME`（可重複，名稱經 `CATEGORY_SYNONYMS` 正規化）與 `--top N`（預設 10）。找不到成員的分類直接報錯並列出可用名稱，不寫任何檔案。
- JSON 新增 `category_detail`（只在帶 `--category` 時出現）：族群合計（與 `period_summary` 該列完全相同）、成員數；大類另有子類小計與 `subcategory_overlap_count`；流入／流出前 N 名個股含外資／投信／自營商拆分、估算金額、`share_of_side_pct`（分母為族群內全部同方向成員，不只前 N 名）與逐日進出。
- Markdown 新增「族群細看」區塊；觀察日超過 10 天時省略逐日表，改指向 JSON。

驗收時主對話修一處：子類分組原本只排除自己，`化學生技醫療`（另一個大類）因 6431 同時帶三個分類而出現在電子工業的子類小計；改為所有大類都不算子類，並讓重疊計數與分組共用同一條規則（先前兩處規則不一致，測試抓到）。

驗證：全套 470 tests 通過（系統 python 與 `.venv`）；不帶 `--category` 重產的報告與 commit 版 byte-identical。真實資料跑 `--category 電子工業 --category 半導體業 --top 10`，另寫腳本由官方 parser 獨立重算：兩族群成員數（460 / 209）、流入與流出前 10 名順序、`share_of_side_pct`、電子工業→半導體業子類小計（98 檔、NT$ -23,328,481,286.55）全部一致。

本期（2026-09-24～10-01）半導體業流出前三：聯發科 -261.8 億（佔流出 30.1%）、台積電 -61.5 億、京元電子 -57.2 億；流入前三：景碩 +139.6 億、穩懋 +92.4 億、華邦電 +81.3 億。數字為淨股數 × 收盤價的估算，不是實際成交金額。

## 2026-10-03 (6) 四個來源皆無資料的日期視為休市

使用者決定：四個來源（TWSE／TPEx 法人、TWSE／TPEx 收盤）全部是 `missing` 或 `no_data` 的日期，一律認定為休市。行為本來就是如此（列入 `dates_without_data`、不降級），本次只把 Markdown 標示由「無資料日期（休市或未抓取，無法區分）」改為「休市日（四個來源皆無資料，視為休市）」，並在 `docs/data-contracts.md` 與 AGENTS.md 寫明這是規則。JSON 欄位名稱維持 `dates_without_data`（描述證據，不改契約）。

已知風險與緩解：抓取失敗不會被誤判為休市，因為 `ingest sector-flow` 有失敗來源時 exit 1；只有「某日完全沒跑 ingest」會被當成休市。

驗證：全套 470 tests 通過；重產報告 JSON 不變，Markdown 只差這一行，新 checksum `53f813c43049ef132402656abefffccc67a1532134ca0aafeaa7963997e6c506`。

## 2026-10-03 (7) ingest 請求間隔與歷史回補

- `UrllibJsonHttpClient` 新增 `min_interval_seconds`（預設 3 秒），任兩次網路請求至少間隔這麼久；cache 命中不呼叫 client，所以不受影響。理由：TWSE 會封鎖短時間大量請求的 IP，而這台機器上 `quantitative-trading-decision-system` 的排程也打 TWSE。
- 回補 2026-07-01～10-02：TWSE T86 / MI_INDEX 補齊 65 個交易日。**TPEx 大多回 HTTP 520**（Cloudflare 類回應），9 月只有少數日期成功；回補後單發一個請求仍為 520。判斷為大量請求觸發的暫時封鎖。不偽裝 User-Agent 繞過，待冷卻後以更長間隔重試。
- 影響：TPEx 缺漏的日期會依既有規則讓報告 `degraded`，不會被當成休市（同日 TWSE 有資料）。每日排程必須容忍 TPEx 暫時失敗並在之後自動補抓。

## 2026-10-03 (8) Sector Flow V1 搬到波段 MVP

動機：使用者要在波段站台 `https://192.168.50.109/trading/` 加「族群資金」頁籤並排平日 22:00 cron 抓資料，producer 必須和站台同 repo。

- 搬到 `~/services/stock/tw-day-trading`（MVP commit `3d6032e`）：
  - `sector_flow_sources.py` -> `src/market_data/sector_flow_sources.py`
  - `sector_flow.py` / `sector_flow_report.py` -> `src/application/reporting/`
  - 4 個測試 -> `tests/unit/`，fixtures `fixtures/sector-flow/` -> `tests/fixtures/sector-flow/`
  - CLI：`ingest sector-flow` -> `python3 -m app market sync-sector-flow`；`report sector-flow` -> `python3 -m app report sector-flow`
  - design / plan 兩份 doc 搬入 MVP `docs/superpowers/`，本 repo `data-contracts.md` 的 Sector Flow 合約附在 design doc 文末。
- 快取：`data/raw/{twse,tpex,tdcc}` 與 `data/raw/finmind/TaiwanStockInfo` 以 `cp -a` 複製到 MVP；本 repo 刪除 `twse/tpex/tdcc`，保留 `TaiwanStockInfo`（candidate_builder 仍用）。
- 本 repo 已移除：3 個模組、4 個測試、fixtures、2 份 doc、`reports/2026-09-24_2026-10-01-sector-flow.{json,md}`、`cli.py` 的 import / cmd / parser / `_positive_int`。上文歷史條目保留不改。
- 等價驗證：在 MVP 以 `.venv/bin/python -m app report sector-flow --start-date 2026-09-24 --end-date 2026-10-01 --cache-dir data/raw` 重產，與本 repo 搬遷前的報告 sha256 完全相同：
  - JSON `4813eff16e65ddae0b119ffdcff75dccf89b6cac1c7ce5084e925b45e6453ea4`
  - Markdown `53f813c43049ef132402656abefffccc67a1532134ca0aafeaa7963997e6c506`
- 殘留風險：TPEx 2026-07-01..10-02 回補曾被 HTTP 520 擋下，搬過去的快取可能不完整，報告會因此 `degraded`；MVP 站台頁籤與 22:00 cron 尚未建立（搬遷時刻意不做）。


# 本 repo

## 2026-10-03 網頁「族群資金」頁籤、dashboard 與平日 22:00 cron

**做了什麼**

- **頁籤**：站台「資金總覽」右邊新增「族群資金」頁籤，版面依使用者逐項確認的草稿實作：
  - 可切換近 20／30／60／90 個交易日。
  - 摘要卡：「今日轉為流入／流出」的數字點下去，會跳出原生 `<dialog>` 列出名單。
  - 流向轉變表、族群 × 日期熱圖、族群排行。
  - 族群細看：每日淨額柱、累計線，疊加類股指數和加權指數（區間首日為 0% 的漲跌幅）。大類有子類小計。
  - 流入／流出前 5 名個股，代號連到 Yahoo 股市，做法和交易紀錄頁籤的代號一樣。
  - 回到頂端的浮動鈕，右緣對齊 main 內容邊線。
- **資料**：
  - `report sector-flow-dashboard` 產生 `data/sector_flow/dashboard.json`，`GET /api/sector-flow` 原樣回傳，頁籤第一次打開時才抓。
  - 格式見 `docs/superpowers/specs/2026-10-03-sector-flow-tab-contract.md`。
- **排程**：`scripts/sync_sector_flow.sh` 依序跑三步，平日 22:00 由使用者 crontab 執行：
  1. 更新產業分類快照（`sync-sector-taxonomy`）。
  2. 抓最近 7 天的資料（`sync-sector-flow`）。
  3. 重算 dashboard。
- **產業分類**：新增 `market sync-sector-taxonomy`，抓 FinMind TaiwanStockInfo，存成有日期的快照；內容和最新快照相同就不寫。

**為什麼這樣做**

- **每個視窗各跑一次報表，不分段加總**：
  - 第一版把長區間切成 ≤31 天分段再加總。用真實資料核對時，發現占比會有小偏差（例如 48.6% 對 48.2%），子類檔數也會少算 1。原因是缺價股票會被某一段整段排除，但在其他段仍被加總。
  - 改法：`build_sector_flow_report` 加上 `max_days` 參數（預設仍是 31），dashboard 傳 `None`。
  - 31 天上限原本是為了限制連網抓取的範圍；`report sector-flow` 指令和 `sync-sector-flow` 抓取的行為都不變。
- **預先算好 JSON 給網頁讀，而不是每次請求現算**：一次要算 90 天的報表約需 13 秒；預先算好後，網頁切換區間是即時的。

**驗證**

- **正確性**：20／30／60／90 日四個視窗，都和同日期區間直接跑 `report sector-flow` 逐項比對：個股排行（代號、金額、占比、外資、投信）、子類小計、族群合計全部一致。
- **補抓**：2026-05-15..10-02，請求間隔 5 秒，共 5 段，0 失敗；TWSE／TPEx 四個資料集都是 97 個交易日，日期完全對齊。先前的 HTTP 520 沒有再出現。
- **產業分類**：更新到 2026-10-03 後（4,329 筆），分類覆蓋率 100%，「未分類」族群消失（38 → 37 個），dashboard 狀態由 `degraded` 變為 `ok`。
- **頁面**：在暫時的伺服器上實測：
  - 四個視窗都能切換，每個視窗的所有族群細看都沒有 NaN。
  - dialog、Yahoo 連結都正常；只在打開頁籤時才抓資料。
  - 手機寬度沒有橫向捲軸。
- **cron**：使用者手動跑一次，兩步都 exit 0。這一次也抓到 TDCC 10-02 的週快照，TDCC 現在有兩期。
- **測試**：完整測試全部通過。

**事故**

- **經過**：網頁 subagent 收掉自己在 8899 埠的測試伺服器時，用了 `kill $(pgrep -f "uvicorn src.web.server:app")`，連正式的 `trading-web.service` 也被一起關掉。systemd 把 SIGTERM 當成正常結束，沒有自動重啟，站台 502 約 20 分鐘，由使用者 `sudo systemctl restart` 恢復。
- **教訓**：測試伺服器只用自己的 PID，或用含唯一埠號、且不會比對到自己 shell 的條件來關，例如 `pgrep -f -- "--port 889[7]"`。收完要再確認正式站台的 `/healthz`。

**殘留風險與下一步**

- 頁籤沒有顯示 TDCC 大戶代理指標；TDCC 已有兩期，可以加。
- FinMind 若出現新的分類名稱，要檢查 `CATEGORY_SYNONYMS`，否則會多出一個重複的族群。
- `static_v` 只在服務啟動時依 `style.css` 的 mtime 計算一次；只改靜態檔而不重啟時，使用者可能要手動重新整理。

## 2026-10-04 子類排名與「大戶持股（週）」

**做了什麼**

- **子類排名**：大類（電子工業、化學生技醫療）細看的「子類小計」可以點，點了以後下方的流入／流出前 5 名換成該子類，上方顯示麵包屑「電子工業 › 半導體業」，點「電子工業」或再點同一列回到大類。切換區間時保留選取；新區間沒有這個子類時回到大類。
- **大戶持股（週）**：獨立一張表，放在族群排行和族群細看之間。
  - 各族群欄位：大戶估算金額（Σ 大戶股數變化 × 收盤價）、增加檔數、減少檔數。
  - 點一列，下方顯示該族群大戶增加／減少前 5 名個股：代號、名稱、估算金額、大戶張數變化、持股比例變化（pp）。
  - 這是週資料，不受 20／30／60／90 日切換影響。
- **資料端**：
  - `build_category_detail` 的每個子類多了 `top_inflows` / `top_outflows`。
  - `build_large_holder_proxy` / `build_sector_flow_report` 新增 `include_stocks` / `large_holder_stocks` 旗標，預設關閉；dashboard 打開這個旗標，取得個股明細。
  - 合約新增 `subs[].in/out` 與 `large_holder`。

**為什麼這樣做**

- **子類用「在大類裡的成員」，不跳到同名頂層族群**：FinMind 給上櫃股的分類幾乎沒有「電子工業」標籤，所以近 20 日「電子工業 › 半導體業」有 99 檔、幾乎都是上市股，而頂層「半導體業」有 212 檔、約一半是上櫃。跳過去的話，數字會和剛點的那一列對不上。
- **大戶主指標用估算金額加增減檔數，不用張數**：跨股票加總張數會被低價股主導。例如 09-24 → 10-02 這一週，貿易百貨增加 42 萬張，金額只有 +18 億。
- **大戶週資料另外成一張表**：TDCC 公開資料只給最新一週、無法回補歷史，和日資料的熱圖、區間不同頻率，所以分開放。

**驗證**

- **報表輸出不變**：不帶 `--category` 時，`report sector-flow` 重產 09-24..10-01，JSON／Markdown 的 sha256 仍是 `4813eff1` / `53f813c4`。
- **子類排名**：4 個區間 × 2 個大類，共 72 組子類。拿大類的完整個股清單、依產業分類篩出成員，自行排序比對，0 差異。
- **大戶個股**：直接讀 TDCC 原始檔，加總分級 12～15 的股數，再乘上 10-02 收盤價，獨立重算 60 筆前 5 名個股，0 差異。
- **頁面**（暫時伺服器加瀏覽器）：
  - subagent 點過所有子類列（72 次）和所有大戶列（37 列），沒有 NaN、沒有 console 錯誤，手機 390 寬度沒有橫向捲軸。
  - 我另外攔截 API，測了沒有真實資料可測的幾種情況，都顯示正確：
    - 大戶資料只有一期。
    - 兩期相隔超過兩週。
    - TDCC 快照無法讀取。
    - 舊資料沒有 `large_holder`（整區隱藏）。
    - 有股票缺收盤價（顯示「N 檔無收盤價」）。
- **測試**：完整測試 481 個通過。

**殘留風險與下一步**

- 大戶目前只有 09-24 → 10-02 一期變化，週快照由 cron 每週累積；累積 4 週以上後可考慮加週走勢。
- 增資、減資、合併會讓大戶股數跳動，頁面上已註明，但沒有自動排除。
- `static_v` 只在服務啟動時計算，這次改的 JS／CSS 要等服務重啟後才會換新的快取版本號；在那之前，瀏覽器可能要強制重新整理才會看到新版。

## 2026-10-04：大戶持股移到獨立頁籤

使用者要求把「大戶持股（週）」從「族群資金」頁籤移到新的頁籤。

- 新頁籤 `#tab-holder`（桌機「大戶持股」、手機「大戶」）排在「族群資金」右邊。資料仍是 `dashboard.json` 的 `large_holder`，不加新 API；兩個頁籤先打開哪個就由哪個抓，只抓一次。
- 樣式從 `#tab-sector` 改成 `:is(#tab-sector, #tab-holder)`，選擇器權重不變。
- 說明文字拿掉「不受上方區間切換影響」，因為新頁籤沒有區間按鈕。

**驗證**

- 暫時伺服器加 Chromium：
  - 直接開 `#tab-holder` 時，37 列都在，預設選半導體業。點列、按 Enter 都會換前 5 名。
  - 切到「族群資金」後排行正常，全程只抓一次 API。
  - 手機 390 寬度沒有橫向捲軸。
  - 攔截 API 測了 404 和只有一期兩種情況，都在大戶頁籤顯示訊息。
  - 除了刻意製造的 404，沒有其他 console 錯誤。
- 完整測試 481 個通過，`git diff --check` 乾淨。測試伺服器用 `--port 889[5]` 停掉後，正式站台 healthz 200。
