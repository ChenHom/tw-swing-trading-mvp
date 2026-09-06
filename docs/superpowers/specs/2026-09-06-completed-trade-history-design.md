# 已完成交易歷史查詢設計

日期：2026-09-06
狀態：已完成互動設計，待使用者審閱本文

## 目標

在現有 Web 儀表板的「資金總覽」Tab 中，於「歷史權益曲線」下方新增唯讀的「歷史交易紀錄」。使用者可依賣出／平倉日查看每次 SELL 的完整交易損益，並展開檢視該 SELL 對應的 FIFO 買進批次。

本功能回答的是「某天實際平倉了哪些交易、每筆扣除可得費稅後賺賠多少」，不是顯示尚未平倉部位，也不重新推導策略訊號。

## 範圍與不變項

- 保留現有 `body > main > div.tab-nav`，不新增 Tab、不改順序、不改名稱。
- 保留既有資金總覽、資產配置及歷史權益曲線。
- 唯一新增的 UI 是權益曲線下方的一張交易紀錄卡。
- 只讀既有 `fills` 與 `fifo_matches`；不新增資料表、不做 migration、不修改任何帳務事實。
- 一次 SELL 顯示一列，以 `sell_fill_id` 作為交易列身分。
- 交易日期取 `fifo_matches.matched_at` 的台北日曆日期，等同該 SELL 的平倉日。
- 尚未賣出的持倉不顯示。
- 帳號與策略邊界完全依既有事實欄位隔離。

## 操作流程

1. 使用者進入既有「資金總覽」Tab。
2. 交易紀錄預設載入所選帳號最近一個有 FIFO 平倉事實的日期。
3. 使用者可由日期下拉選單選擇平倉日，或按「上個平倉日／下個平倉日」跳轉；不需要逐日跳過無交易日期。
4. 畫面顯示當日完成交易筆數、毛損益、交易成本及淨損益摘要。
5. 每次 SELL 各列一筆。點整列或右側指示器後，在該列下方展開 FIFO 配對明細；再點一次收合。
6. 同時間只展開一筆。必要資訊不依賴 hover，鍵盤與觸控也能操作。

交易日期使用獨立 GET query parameter `trade_date=YYYY-MM-DD`，不改變頁首既有 `view_date` 的語意。交易日期導覽連結保留目前的 `account` 與 `view_date`。當使用者切換帳號時，交易日期回到該帳號最近的平倉日，避免把上一帳號的日期誤套到新帳號。

## UI 呈現

### 每日摘要

- 完成交易：當日不同 `sell_fill_id` 數量。
- 毛損益：當日所有交易列 `gross_pnl` 合計。
- 交易成本：可計算時為 `gross_pnl - net_pnl`，UI 以負數費用呈現。
- 淨損益：當日所有交易列 `net_pnl` 合計。

### 主表每列

- 賣出時間。
- 股票代號與中文名稱。
- 策略中文名稱。
- 賣出股數。
- FIFO 數量加權買入均價。
- 賣出成交價。
- 持有期間：從該 SELL 配對到的最早買進日至平倉日之日曆天數；同日買賣為 0 日。各批次的實際期間留在展開明細。
- 淨損益。
- 淨報酬率：`net_pnl / FIFO 買進名目金額`。
- ＋／−展開指示器。

價格只在顯示邊界由 Price x 10000 轉為小數；TWD 損益持續使用整數。排序固定為賣出時間、`sell_fill_id`，確保同一資料每次呈現順序一致。

### 展開明細

每個 FIFO match 顯示買進日、配對股數、買進價、該批次持有日曆天數，以及完整的 `buy_fill_id → sell_fill_id`。ID 使用等寬字與自動換行，只放在展開區，不占用主表空間。下方列出毛損益、費用與稅、淨損益。

指示器以 CSS 畫兩條線：收合為＋；展開時垂直線旋轉並淡出，只留下−，約 180ms。明細同步淡入。`prefers-reduced-motion: reduce` 時取消動畫但保留功能。

桌機顯示表格；窄螢幕沿用專案既有水平捲動模式，不改 Tab 導航。白底主要資料使用深色文字，灰色只用於欄名及次要說明。

## 資料服務設計

新增獨立唯讀 service `src/application/services/completed_trades.py`，避免繼續擴大 `dashboard.py` 的私有 SQL。公開四個小型查詢入口：

- `list_close_dates(conn, account_id) -> list[str]`
- `read_completed_trades(conn, account_id, close_date) -> list[dict]`
- `build_trade_day_summary(trades) -> dict`
- `build_completed_trade_history(conn, account_id, requested_date=None) -> dict`，組合日期、前後導覽、交易列與摘要，供 dashboard 使用。

`read_completed_trades` 以 `(account_id, sell_fill_id)` 聚合 `fifo_matches`，並 join BUY／SELL `fills` 取得成交時間與來源。每個聚合列包含主表欄位及依買進時間排序的 `lots[]`。

核心計算：

- `quantity = SUM(match.quantity)`
- `weighted_buy_price_x10000 = SUM(quantity * buy_price) / SUM(quantity)`
- `gross_pnl = SUM(realized_pnl)`
- 所有 match 的 `net_realized_pnl` 均非 NULL 時，`net_pnl = SUM(net_realized_pnl)`
- `cost_twd = gross_pnl - net_pnl`（正整數，模板顯示為扣除項 `−cost_twd`）
- `buy_notional_x10000 = SUM(quantity * buy_price)`
- `return_pct = net_pnl * 10000 / buy_notional_x10000 * 100`

運算使用整數分子，僅在輸出顯示比例時轉換，避免用浮點數重建帳務金額。

## 舊資料的未知淨損益

正式 DB 目前存在 `net_realized_pnl IS NULL` 的舊 FIFO match，這代表當時的完整費稅無法可靠回算。

若某個 SELL 聚合列中任一 match 的淨損益為 NULL：

- 該列仍顯示股數、買賣價與毛損益。
- `cost`、`net_pnl`、`return_pct` 顯示 `—`。
- 顯示「舊資料無法回算完整費稅」說明。
- 不以毛損益冒充淨損益，也不自行套用現行費率估算歷史值。

若選定日期任一交易列的淨損益未知：

- 每日毛損益仍可合計。
- 每日交易成本與淨損益顯示 `—`。
- 摘要標示「部分交易淨損益不可計算」。

## 無資料與錯誤處理

- 帳號沒有任何平倉事實：顯示「尚無已完成交易」，日期控制停用。
- 合法日期但當日無平倉：顯示空狀態，不自動跳到其他日期。
- `src/web/server.py` 將 `trade_date` 宣告為 `date | None`；非法日期格式由 FastAPI 回 HTTP 422，不回傳其他日期資料。
- 被竄改成其他帳號才有的日期：只依目前帳號查詢並回空，不洩漏另一帳號資料。
- 股票名稱查無時仍顯示代號；策略名稱沿用既有 fallback。

## 測試目標與案例

測試的核心不是再次驗證 FIFO 引擎如何產生事實，而是驗證讀取層沒有錯誤分組、錯誤歸日、跨邊界混入或把未知數字偽裝成已知。

### 聚合身分

- 兩筆 BUY（60@100、40@110）由一次 SELL（100@120）吃完：主表一列、明細兩列、加權買價 104、毛損益 1,600。
- 同一標的、策略、日期分兩次 SELL：必須顯示兩列，防止錯用 `(date, symbol, strategy)` 合併。
- BUY 100、SELL 40：顯示一筆 40 股完成交易；剩餘 60 股不進入歷史完成交易。

### 日期與隔離

- 買進日查不到完成交易，僅在 SELL 的 `matched_at` 日期出現。
- 同日同標的但不同帳號、不同策略的資料不得互相混入。
- 只有 BUY、沒有 FIFO match 的部位不得出現。
- 同日 00:00:00 與 23:59:59 的 SELL 均歸同一天，隔日資料不得混入。
- 空帳號、合法無交易日、非法日期與被竄改日期分別有明確結果。
- 切換 `trade_date` 保留 `account` 與 `view_date`；切換帳號重設為新帳號最近平倉日。

### 損益誠實性

- 已知淨損益時，列與摘要符合 `gross - cost = net`。
- 多 FIFO match 的費用只由既有 `net_realized_pnl` 相加，不重複扣除。
- 任一 match 的 `net_realized_pnl` 為 NULL 時，整個 SELL 的 cost/net/return 均為 NULL。
- 當日任一交易為未知淨損益時，每日 cost/net 不產生看似完整的數值。
- 報酬率分母使用配對買進名目金額；淨損益未知時報酬率亦未知。

### Web 與互動

- 新卡位於權益曲線後，既有 Tab 的數量、名稱、順序不變。
- 預設收合；點整列展開；再點收合；點第二列時第一列收合。
- ＋／−動畫狀態正確，鍵盤 Enter／Space 可操作。
- `prefers-reduced-motion` 下無動畫但仍可展開。
- 窄螢幕可水平捲動，必要資料不依賴 hover。
- 空值顯示 `—` 與原因文字，不輸出 `None`、`null` 或假造數字。

### 唯讀與實際資料核對

- 查詢及渲染前後 SQLite `total_changes` 不變。
- `fills`、`fifo_matches`、`cash_ledger` 的列數與內容不變。
- 使用臨時 DB 做端到端 fixture，涵蓋多 BUY、部分 SELL、多次 SELL、跨帳號、跨策略與 NULL 淨損益。
- 對正式 DB 的 `simulation-main / 2026-08-27` 做唯讀 readback：3 筆 SELL、毛損益 +1,015、已知成本 -404、淨損益 +611。

## 預計修改範圍

- 新增 `src/application/services/completed_trades.py`。
- 擴充 `src/web/server.py` 接收及驗證 `trade_date`。
- 擴充 `src/web/templates/dashboard.html`，僅在權益曲線後新增卡片。
- 擴充 `src/web/static/style.css` 實作表格、展開指示器、動畫與窄螢幕捲動。
- 新增 service 單元測試。
- 擴充 Web 渲染測試。
- 加入瀏覽器互動驗證。
- 更新 `docs/development/ui-development.md` 與 `docs/development/todo.md`。

不修改交易引擎、FIFO 寫入邏輯、資料庫 schema、每日執行流程、既有 Tab 導航或券商相關功能。

## 驗收條件

1. 使用者能在資金總覽的權益曲線下方，選擇任一既有平倉日。
2. 每個 SELL 僅一列，多個 FIFO 買進批次可展開稽核。
3. 帳號、策略、日期及部分平倉邊界無資料污染。
4. 已知淨損益與現有事實逐元一致；未知淨損益誠實顯示 `—`。
5. 現有 Tab 與帳務寫入路徑零變更。
6. 桌機、手機、滑鼠、鍵盤皆可使用，必要資料不依賴 hover。
