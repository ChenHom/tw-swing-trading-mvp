# 施工記錄 (Engineering Log)

> 本檔記錄每一次有意義的開發變更：**動哪裡、為什麼要動、會怎麼動、為什麼這樣動、考慮了什麼、優缺點、結果**。
> 目的是讓未來的自己（或協作者/AI）能還原當時的決策脈絡，而不只看到 git diff。
>
> **格式約定**：每筆一個 `##` 區段，由新到舊（最新在上）。每筆至少包含：背景/觸發、變更內容、為什麼這樣做、考慮的替代方案與取捨、優缺點、驗證結果、關聯 commit。
>
> **未來規劃**：本「施工記錄」流程於 UI 建置完成後，封裝為 skill 或 MCP，讓每次開發自動產出記錄。目前先以本 md 手動維護。

---

## 2026-06-23 ｜ 誠實回測實驗室 + trend_rider「順勢交易者」Challenger（Phase 0/1/2 + R）

**背景／觸發**：使用者把目標釐清為「**建策略 → 回測 → 驗證是否真的賺錢 → 持續優化**」。盤點發現一個致命現況：Phase 0/1/2 的回測機器（305 tests）**全是合成單元測試、從未在真實資料上跑過**，`research.db` 根本不存在。對「會不會賺錢」這個問題，系統連 step 0 都沒踏出。

**Phase 0/1/2（先前已寫、本次首度真實驗證）**：雙價/CA 帳本 + FinMind/TWSE provider + PIT universe 骨架（Phase 0）；風險/結構指標、四條 benchmark、報酬分層、DSR/bootstrap/Herfindahl/有效N gate、成本占比、分年表、五級裁決狀態機（Phase 1）；Strategy Thesis、Research Ledger、家族級 lockbox、參數高原掃描（Phase 2）。

**Phase R（讓回測在真實資料上跑起來）**：
- `backtest run` 加 `--db`（指向 `research.db`，預設仍 `app.db` 不破壞 live）。
- 跑 `market backfill-history` 回補真實歷史 → research.db（**44,959 bars, 2018-2026, 25 檔, 含 2022 完整空頭**；196 現金股利+9 股票股利）。
- 端到端真跑暴出 **3 個合成單測沒抓到的整合 bug**：① fingerprint `set(dict)` TypeError（index_symbols 是 dict，改取 code）② TSE 加權指數 FinMind data_id 要用 `TAIEX`（加 alias，落帳仍存 `TSE`）③ approval 時效閘擋掉歷史回放（backtest mode 跳過 valid_from/expires_at，保留 digest/issuer/revocation/mode）。
- **另發現**：backtest 在共用 db 上**非冪等**（signal `bundle_id` 非 run-scoped，重跑碰撞）→ 暫以「research.db 當資料母版、copy-per-run」繞過。

**三支策略真實數字（2018-2026, 8.5年, diagnostic universe → 仍 INVALID 但數字真實）**：
| 指標 | trend_breakout | pullback_rebound | trend_rider |
|---|---|---|---|
| 總報酬 / Sharpe | +32.9% / 0.49 | +44.2% / 0.82 | **+121.9% / 1.20** |
| maxDD / 交易數 / PF | 14.6% / 388 / 1.54 | 9.4% / 410 / 1.89 | 10.5% / **113** / **3.92** |
| 成本占毛利 | 34.3% | 29.2% | **5.6%** |
| COVID / 2022 / 2024 | −0.5%/−3.6%/−2.4% | −1.8%/−3.3%/+6.4% | −2.8%/−1.2%/**+6.9%** |

**誠實解讀**：① 兩支現役策略的真 edge 是**防守**（COVID 急殺 0050 −26%、2022 緩跌 −25%，策略都只虧 ~3%，靠 index 60MA 濾網 + 停損，非運氣）。② 結構缺陷在「持續上升趨勢」被放到最大——2024 AI 大年個股 +30~114%，現役策略幾乎零捕獲。③ 故新增 **trend_rider「順勢交易者」**（reuse risk_exit 引擎、零引擎改動，純靠 exit config「讓贏家跑」：寬移動停利 −25%、長均線跌破、停用時間停損；保留 index 60MA 濾網作崩盤防守）。

**trend_rider 結果與紀律警告**：成本剩 5.6%（交易少）、崩盤防守保住、Sharpe 1.20——這些是**不受標的池偏差影響的結構性勝利**。但 **+121.9% 是全研究最受後見之明污染的數字**（「讓贏家跑」套在「今天才挑、已知 2330/2454 漲 10~14x 的籃子」上必然超好看；HHI 最高、去最佳 5 筆只留 22%）。**報酬 edge 未證實**，需真 PIT universe 才能公平裁決。

**為什麼這樣動 / 紀律**：trend_rider 參數依標準趨勢跟蹤慣例**設死、不為補抓已看過的 2024 調參**（過擬合＝違反 S4 一次性）；Strategy Thesis 看結果前寫死；新 strategy_id、**不動現役兩支**（Champion/Challenger 分離）。

**UI**：儀表板「持倉部位」加「最後收盤」欄；「策略別損益」改顯示策略中文名 + ⓘ tooltip（reuse `strategy_names` 對照）。

**驗證**：全套件 **311 passed**。三支各產出 `artifacts/reports/backtest/` 報告、數字自洽、裁決＝INVALID+diagnostic_result（今日 universe 非 PIT）。

**關聯 commit**：`277c096`（Phase 0 資料地基）、`08b09a5`（Phase 1+2 量尺與治理）、`4cdd1d3`（Phase R + trend_rider）、`443ce86`（UI 最後收盤 + 策略中文名）。

**遺留 / 下一段（R-T4b，見 plan `2455-cosmic-fountain.md`）**：Track 1 — trend_rider 影子上線驗證（per-account 策略範圍，simulation-main 跑、國泰不污染，累積零偏差 forward 證據）；Track 2 — PIT 流動性排名 universe（消除後見之明：方案A 指數成分史不可行/付費，方案B 流動性排名可行，**下市股歷史可查 → survivorship 可消除**）；其餘問題：backtest 非冪等、成本歸因、fingerprint 解除 diagnostic 寫死、backtest per-date universe。

---

## 2026-06-15（夜）｜ 掛 cron（A3）+ E1 拆 src/cli.py（2053→cli 套件）

**背景／觸發**：go-live 關鍵路徑已轉為使用者操作 + 時間（B1 三交易日），趁等影子空窗清 CEO review #1 技術債（cli.py churn 之冠）。使用者另指示：C1 Web 寫入**不加**（守唯讀 gate）、cron 交我掛。

**A3 掛 cron**：crontab 原有 `10 15 * * 1-5 run_daily_sim.sh`（只跑 run-daily、不產報告/告警）。`shadow_daily.sh` 是其超集，故**替換**該行為 `shadow_daily.sh simulation-main`（保留 15:10 時點＝13:30 收盤資料沉澱；避免同帳戶一天雙跑），其餘 14 條他案排程原封不動。明天 6/16 15:10 自動跑出閉環首筆監控 BUY（00994A）並產報告。

**E1 拆分**：`src/cli.py`(2053行) → `src/cli/` 套件：`common.py`（共用 helper + STOCK_NAMES + 再匯出 load_active_manifests/strategy_registry）、11 域模組（market/strategy/approval/account/backtest/simulation/signal/trade/portfolio/report/corporate_action）、`main.py`（argparse+dispatch）、`__init__.py`（re-export）。最大 trade.py 409 行。

**為什麼這樣動 / 關鍵設計**：
- 領域 handler 內共用名稱一律 `common.X()` **模組限定呼叫**——因測試 `monkeypatch.setattr("src.cli.common.get_settings", …)` 改的是 common 模組屬性，唯模組限定才在 call-time 讀到 patch（`from common import get_settings` 會在 import 時綁死、patch 不到）。
- `__init__.py` re-export 所有 `cmd_*` + `resolve_account_id`/`sign_manifest`，使測試 `from src.cli import cmd_xxx` 與 `app.py` 的 `from src.cli import main` 零改動。
- 實作以一次性 AST 抽取腳本（按 def 行範圍切、對白名單識別子做 `common.` 限定、函式體外的 import header 不動）產生，腳本用後即刪；漏抓的 trade 域常數 `_MONITOR_NOTES` 手動補回。
- 測試僅改 3 個 patch 字串（→ `src.cli.common.*`），直接 import 行不改。

**取捨**：每個域模組沿用完整 import header（部分未用 import），換取零 NameError 風險與簡單；可日後 lint 修剪。淨行數略增（2548）屬重複 header，邏輯不變。

**驗證**：全套件 **154 passed**（含 test_cli_commands 24 全過）；CLI 冒煙 `--help`/`corporate-action check`/`report pnl --by-strategy`/`signal list` 輸出與拆前一致（signal list 非 tty 要 --account 屬正確防呆）；`app.py` 入口可跑。crontab `crontab -l` 確認替換正確、他條未動。

**遺留**：import header 修剪（lint）、其餘 backlog（D2 FinMind、C 系列、B3）。C1 仍受 go-live gate。

---

## 2026-06-15（傍晚）｜ D2 對帳/單位 bug 修正 + 公司行動可視性（顧明天里程碑）

**背景／觸發**：今天影子跑產生 `BUY 00994A [pullback_rebound] target=6/16` → **明天影子跑會執行它，產生閉環首筆策略 BUY、首個 risk_exit 監控部位**（CEO review 等待的里程碑），且首個監控標的恰是會配息 ETF（00994A），D2 立即相關。規劃期間使用者追問單位，連帶查出昨日 D2 三個潛伏 bug。

**修了什麼（Part 0，最關鍵）**：
- **單位錯配**（使用者抓出）：系統 `quantity`=股、`price`/`highest_close`=元×10000、但 **`cash_ledger`/`cash_balances`=整數元**。現金股利入帳原寫 `Σ(股×cash_per_share)` 直塞 cash 欄 → 大 10000 倍。改為 `… ÷ 10000`。記憶 `unit-conventions` 已存此慣例（00400A=9000股×13.67元=123,030元為驗證基準）。
- **reconcile 破壞**：現金股利沒更新 `cash_balances`（破第一關 `SUM(ledger)==balance`）→ 補同步 upsert；股票股利新增 lot 沒寫對應 `fills`（破 `SUM(fills)==SUM(lots)` 總量與策略桶兩層）→ 補合成 `fills`（source=CORP_ACTION、raw insert 不動現金）。
- **測試造假**：舊 `test_cash_dividend_adjustment` 用錯單位斷言（1500 萬而非 1500 元），fixture 對帳不乾淨只弱檢查 → 重寫 fixture 以 `apply_fill_transaction` 建對帳乾淨倉、以整數元斷言、加 **RECONCILE_OK 強斷言**（現金/股票股利各一）。

**新增可視性（Part 1–3）**：`corporate-action check` 盤點持倉與除息登錄狀態（持倉中無登錄 → ⚠提醒自查）；daily_report 新增 §9「公司行動／除權息提醒」（近窗 ±7 日、未套用標 ⚠）；dashboard `_corporate_actions` + 模板「公司行動」表（近一個月、已/未套用 badge）。皆對舊 DB（未 migration）容錯回空。

**Part 4 文件**：`daily_runbook.md` 補「go-live 啟用步驟（使用者操作）」——掛 cron、接 Discord（建 bot/token 走 env/channel_id 走 gitignored 本地檔/實測）、B1 簽核。

**為什麼這樣動**：除權息只調 price/watermark 不調 fills，會破壞系統最核心的「fills 為事實源、lots/cash 為投影」不變式。修法選「補齊投影面缺的列」（cash_balances 同步、合成 fill）而非「放寬 reconcile」，維持不變式即真相。可視性讓明天首筆監控 BUY 與潛在除息風險在報表/儀表板看得到。

**驗證**：全套件 **154 passed**；live DB 以 `init_db`（CREATE IF NOT EXISTS，冪等）補上兩張新表；端到端 temp DB：00994A 5000股配 1.5 元 → reconcile 配息前後皆 OK、現金 +7500 元（非 7500 萬）、均價 17.30→15.80。`corporate-action check` 對 live 9 筆持倉正確列出並提醒。

**遺留**：D2 FinMind 自動抓取、減資、配股碎股殘值→零股款；B1 連跑 3 日（起於今日）；B3 待 gate 齊全。

---

## 2026-06-15（下午）｜ A3/B2/D2 實作與測試（影子先行、Discord 告警、除權息）

**背景／觸發**：go-live 卡在 CEO review 的兩個 🔴（影子先行未跑、cron 告警未接線），外加資料正確性時間炸彈（除權息在 6-8 月旺季會引爆）。計畫書已備、決策已定，需實作落地。

**怎麼動**：
1. **A3 影子先行上線**：`scripts/shadow_daily.sh` 驗證可用（paper-trading，FakeBroker），手動跑一次後掛 cron（`daily_runbook.md` 範例照貼）。無需改變腳本本身（已於 a833141 完成），只需 cron 接線（用戶自行 `crontab -e`）。
2. **B2 cron 失敗告警接 Discord（Bot API）**：新增 `src/notification/discord_alert.py`（httpx 版、極端穩定、例外不外拋），改 `shadow_daily.sh` 在 `⚠ALERT` 後調用子進程發送（尾接 `|| true`），新增 `config/alert.local.yaml.example`，`.gitignore` 加 `config/alert.local.yaml`（gitignored）。token 走 `~/.openclaw/.env`（dotenv），channel_id 走本地設定，單元測試 8 項全綠（configured-from-env/yaml、send 成功回 True、未設定回 False、HTTP 非 2xx 回 False、逾時與例外不外拋）。
3. **D2 除權息 MVP（人工標記+調整）**：新增 `corporate_actions` + `position_cost_adjustments` 事實表（append-only），`projection.apply_corporate_action` 方法（冪等、保對帳平衡），支援現金股利（price/watermark-=cash、現金入帳）與股票股利（price/watermark/=1+ratio、qty*=1+ratio、新增配股 lot）。CLI `corporate-action record/apply/list` 三指令。單元測試 4 項全綠（現金股利、股票股利、冪等、reconcile 仍通過）。

**為什麼這樣動**：A3 腳本已備，只需掛 cron 讓其自動化（gate 時鐘今天就開始走）。B2 用 Bot API 對應記憶 `discord-alert-config` 所述慣例（token env、channel_id gitignored），httpx 相依已有（requirements-web.txt），不新增依賴。D2 採 MVP 人工標記+調整而非自動抓取（FinMind），因為第一筆監控 BUY 時間不確定（影子才在跑），應急方案要快；自動化可後續迭代。公司行動調整公式確保price/watermark/qty一致性、現金分錄對衝股價下修、冪等性防重複套用。

**考慮的替代方案與取捨**：
- **A3**：是否該 shadow_daily.sh 改進？否決，腳本已夠好，cron 接線是 infra 層用戶決定，不該代勞。
- **B2 密鑰存放**：token env（過期刷新靈活）vs bot token 本地檔（簡單但安全隱患）。選 env 沿用既有 dotenv 慣例；channel_id 本地檔因不必重換、gitignored。
- **D2 手動 vs 自動**：自動需 API 對接（FinMind / 證交所），測試成本高、回測重現性問題。手動簡單、用戶掌控，且除息事件每月就幾個，不是高頻操作。若有兩筆監控 BUY 卡在除息日前，再升級自動也來得及。
- **D2 調整策略**：「改 price」vs「記錄係數後查詢時套用」。選前者因現有 risk_exit 直讀 price 欄、watermark 主鍵難加係數列，改 schema 成本高。改 price 的代價是歷史 fills 事實不變（正確）、衍生的持倉成本與風控基準皆調（完整）。

**驗證**：全套件 151 passed（新增 12 test：8 Discord + 4 公司行動）；A3 手動跑成功（exit 0、報告產出、`daily_runbook.md` gate 清單與 `docs/shadow-signoff.md` 簽核表備妥）；B2 測試覆蓋 configured、send、timeout、例外、未配置；D2 測試覆蓋現金股利、股票股利、冪等、reconcile。`git diff` 含 notification 模組、CLI 指令、schema/migration、測試、文檔。

**遺留與後續**（本次不含）：B1 需連跑 3 日核對（起於今日，預期 6/17 完成）；D2 自動抓取與減資完整支援；daily_report.py 「明日將執行訊號」是否同步改「下次執行」（當前維持現狀，避免擴大範圍）；C1 寫入操作。

---

## 2026-06-15（上午）｜ 儀表板日期語意 + 「下次執行」解耦 + 審計在地化（UI UX）

**背景／觸發**：使用者 6/15（週一交易日）手機開唯讀儀表板，三個 UX 問題：(1) 看不到「今天開盤可執行的計畫」——資料其實在（週五收盤產生、target 6/15 的訊號），但預設日期落在最近 run 日 6/12、面板又叫「明日將執行訊號」，讀不出來；(2) 想讓日期顯示今天、面板改名「下次執行」；(3) 底部「審計」看不懂——頂部「對帳」只有通過/失敗 badge，底部「執行事件（審計）」是 `execution_events`，`event_type` 英文代碼、`detail` 一串 sha256/bundle id。

**怎麼動**（全 presentation 層，read-only，未動 schema／未接寫入路由）：
- **日期預設改今天**（`server.py`，原為 `latest_run_date`）。釐清：`view_date` 只驅動 `run_status`／`fills_today`／`events` 三塊；cash/positions/pnl/monitored/reconcile 為即時狀態。模板加 hint 說明、傳 `today` 供「檢視非今天」提示。
- **「下次執行」與日期解耦**：新增 `dashboard._next_execution_signals(conn)`，查 `signal_date == MAX(signal_date)`（最近一批），不接 view_date；`build_dashboard` 鍵 `next_signals`→`next_execution`；模板改名「下次執行」+ 副標。
- **對帳在地化**：新增 `_reconcile_summary()` 把 reconcile dict 轉 `{ok, code, detail_zh}`，失敗含具體差異數字；卡片顯示 badge + 明細 + 一行說明。
- **事件在地化**：`EVENT_TYPE_LABELS` 對照（4 個授權閘門代碼 + legacy），`_events` 每列加 `event_label`；模板「中文（原碼小字 tag）」呈現，`detail` 留技術明細。
- CSS 加 `.hint`/`.caption` 小字；測試更新（預設今天、解耦、對帳明細、事件 label，全套件 139 passed）。

**為什麼這樣動**：關鍵根因是「明日將執行」面板被 `signal_date == view_date` 綁死——單純把預設改今天反而讓它變空（6/15 尚未產生訊號）。解耦改查最新批次才同時滿足「顯示今天」與「看得到下次執行計畫」。在地化只在 presentation 層轉文案，`projection.reconcile()`／`engine` 契約不變。

**考慮的替代方案與取捨**：
- 「下次執行」曾考慮以 `target_execution_date >= 今天` 查；否決：資料相依、且退役策略的過期 target 會混入。改用「最新 signal_date 批次」語義單純、空批次顯示「無」即真實狀態（如 06-12 收盤未產生訊號）。
- 日期預設今天的代價：交易日盤前 run_status/fills/events 三塊為空，但這是真實狀態，且即時面板與下次執行仍有內容，不再是先前「整頁空白」問題（解耦後計畫面板恆有資料）。此為對先前 commit 2ad8aed（預設最近 run）的有意調整，經使用者確認。
- daily_report.py 文字報告維持「明日將執行訊號」字樣（範圍外，避免擴大）。

**驗證**：全套件 139 passed；playwright 手機尺寸截圖確認今天視圖（日期=今天、面板正確為空、對帳/下次執行帶說明）與 06-12 視圖（事件「授權無效（過期/模式不符）」中文化 + 原碼 tag）。`git diff` 僅含 read-only 檔案。

**關聯**：todo C（UI 分期）、UI 需求（手機可看、即時可讀）；計畫 `system-reminder-message-sent-at-sun-dreamy-pebble`。

---

## 2026-06-15 ｜ 寫入側 service 骨架 + trade 域抽取（C1 前置／E1 第一刀）

**背景／觸發**：UI 分期 C1「寫入操作」依安全鐵律（ui-development §2/§9）必須走「與 CLI 相同、已驗證的 engine/projection 邏輯」，UI 不得直接 `INSERT/UPDATE` 事實表。但這些寫入邏輯原本內嵌在 `cli.py` 的三個 handler（`cmd_trade_record_fill` / `cmd_trade_reject_signal` / `cmd_trade_un_reject_signal`），混雜 argparse／DB／print／`sys.exit`，無法被未來 Web POST 直接重用，也是 E1（cli.py 1853 行技術債）的一部分。

**怎麼動**：
- 新增 `src/application/services/trade_write.py`，比照讀取側 `dashboard.py` 風格：module-level 函式、接 `conn`、無副作用式輸出（不 print／exit／argparse）。封裝 `record_fill` / `reject_signal` / `un_reject_signal`。成功回結構化 dict；使用者層級驗證錯誤拋 `TradeWriteError(code, message)`。
- `record_fill` 走既有 `PortfolioProjection.apply_fill_transaction`（沿用 FIFO/cash/PnL，不繞過）；未知 strategy_id 於寫入前拋 `UNKNOWN_STRATEGY`（不寫入任何 fill）；監控資格回 `monitor_status` 枚舉（`long_term_excluded` / `manual_excluded` / `monitored` / `not_monitored` / `indeterminate`），exit 集合由呼叫端傳入（保持純函式輸入）。
- `cli.py` 三個 handler 改為薄消費者：解析 args → 算 `exit_strategy_ids`（長期/MANUAL 免查；其餘 try/except 載入失敗退 None）→ 呼叫 service → 依回傳 dict／`monitor_status` 映射**現有中文輸出字串（逐字保留）** → `except TradeWriteError` print+`exit(1)`、`finally: conn.close()`。
- 新增 `tests/unit/test_trade_write_service.py`（14 直測）。

**為什麼這樣動**：寫入路徑的 invariant（FIFO 隔離、冪等、授權閘門、原子提交）全在 projection／既有 SQL，service 只搬「資料進出」這層、不重寫邏輯，故風險最小。presentation（中文提示文案、`買入/賣出` 標籤、monitor_status→文字）留在 CLI，service 只回狀態枚舉，未來 Web 自行渲染——一份 service、兩個前端。

**考慮的替代方案與取捨**：
- 曾考慮 service 直接回中文提示字串：否決，會把 presentation 綁死於 CLI，Web 無法重用語意；改回枚舉。
- 曾考慮 service 內部自行 `load_exit_managed_definitions`：否決，會讓監控判定耦合 settings／YAML IO，且破壞既有測試「長期持有不查 exit 定義」的斷言；改為 exit 集合由呼叫端決定、service 純函式判定（`None`→indeterminate）。
- 本計畫**不接任何 Web POST 路由**（C1 啟用受 go-live 影子驗證 gate），`git diff src/web/server.py` 為空，只備骨架。
- E1 僅抽 trade 域三操作，其餘域（signal/report/market/approval/portfolio）列為後續，避免一次大改。

**邊界提醒**：`apply_fill_transaction` 的 `ValueError`（SELL_WITHOUT_POSITION / LONG_TERM_PROTECTED）由 service 往外傳、CLI 渲染為「錄入成交資料失敗」；reject/un-reject 由 service 自行 commit（沿用現行行為）。connection 生命週期由呼叫端 own（service 不 close，比照 dashboard）。

**驗證**：全套件 136 passed（122 + 14 新 service 直測）；既有 CLI 輸出斷言不變；`git diff src/web/server.py` 空；`app.py trade record-fill -h` 參數不變。

**關聯**：todo C1（service 骨架已備、Web 路由待 go-live）／E1（第一刀）、UI 分期 C1 前置、計畫 `drifting-spinning-cerf`。

---

## 2026-06-14 ｜ record-fill 可歸策略（A1／資料正確性）

**背景／觸發**：手動補錄成交 `trade record-fill` 一律寫死 `strategy_id='MANUAL'`，而 MANUAL 結構性排除於 risk_exit 監控與策略別損益。但使用者實際流程是「看策略訊號→自行到券商手動下單→事後補錄」，這類成交意圖上屬某策略，全歸 MANUAL 會導致 (a) 不受停損監控、(b) 策略別損益失真，亦使儀表板「監控」欄恆為 0（待修項 [[record-fill-strategy-attribution]]）。

**怎麼動**：
- `cli.py` `record-fill` 新增 `--strategy-id`（預設仍 `MANUAL`，向後相容）。
- 驗證：strategy_id 須屬 `registry.PARAMS_MODELS` 或 `MANUAL`，否則印錯誤訊息（列出可用策略）並 `exit(1)`，不寫入任何 fill。
- 成功輸出新增「策略歸屬」行並標示監控資格：長期持有／MANUAL → 結構性排除；具 exit 區塊策略 → 已納入監控；無 exit 區塊策略 → 不受監控。監控資格查詢以 `load_exit_managed_definitions` try/except 包覆（防 settings mock 或 YAML 缺檔時噴錯）。
- 新增 5 個 CLI 測試（預設 MANUAL、歸策略受監控、無 exit 區塊、長期+策略排除、未知策略拒絕）。

**為什麼這樣動**：下游機制（per-(strategy_id,symbol) FIFO 隔離、策略別 PnL、`update_high_watermarks`、`RiskExitEngine`）原本就以 strategy bucket 運作，唯一缺口是 record-fill 寫死 MANUAL。故最小且正確的修法只是「讓 record-fill 能指定 bucket」，不動 projection／engine。歸入具 exit 的策略後，當日起 daily run 的 `update_high_watermarks` 會自動納入該部位、risk_exit 即監控之；建倉首日無 watermark 時 risk_exit 以 `max(買入均價, 當日收盤)` 保守初始化，行為正確。

**考慮的替代方案與取捨**：
- 曾考慮加 `--from-signal` 由來源訊號帶出歸屬（更貼近使用者流程），但需處理 symbol/side 一致性與更多狀態，耦合 signals 表；屬 C1 Web 寫入 UI（go-live 影子驗證前不啟用）一併設計較妥，本次不納入，先把資料側 `--strategy-id` 做穩。
- 驗證來源用 `PARAMS_MODELS`（registry 真實常數、免檔案 IO），而非 `load_strategy_definition`（需讀 YAML）。
- record-fill 仍為 Manifest 授權的例外路徑：補錄的是「外部券商已發生的事實」，BUY 不查授權閘門、SELL 本就不受授權閘門——歸入策略不改變此性質。

**邊界提醒**：SELL 的 FIFO 扣減是 per-bucket，補錄 SELL 須與當初 BUY 歸同一 strategy_id，否則 `SELL_WITHOUT_POSITION`（錯誤訊息已含 strategy_id）。長期持有即使指定策略仍排除於監控（is_long_term 優先）。

**驗證**：全套件 120 passed；`app.py trade record-fill -h` 顯示新參數；新測試涵蓋四種監控提示與未知策略拒絕（不寫入 fill）。

**審查後修正（同日，對抗性多視角審查確認 3 項真實問題）**：
1. *CLI 監控提示誤導（low）*：`load_exit_managed_definitions` 的 broad `except` 失敗時退回空 dict，會對實際具 exit 區塊的策略誤印「無 exit 區塊，不受監控」。改為失敗時退 `None` 並印不確定語氣（「exit 設定載入失敗，無法判定…」）；fill 已 commit，故仍不可拋出（會誤報錄入失敗）。新增測試 `test_record_fill_exit_config_load_failure_is_indeterminate`。
2. *儀表板監控判定不一致（medium，先前既存、被本次新語意凸顯）*：`dashboard._positions` 原為 `monitored = 非MANUAL and 非長期`，未檢查 exit 區塊，與 `RiskExitEngine`／新 CLI 提示／`daily_report` 三方矛盾（無 exit 區塊的策略會被誤標監控）。改為接受 `exit_strategy_ids`，`monitored = 非MANUAL and 非長期 and sid∈exit_strategy_ids`；`server.py` 新增 `_exit_strategy_ids()`（載入失敗回 None→不 500）注入 `build_dashboard`。新增測試 `test_dashboard_monitored_requires_exit_block`。目前三策略皆有 exit 區塊，故為潛伏不一致，但與本任務「監控欄正確」目標直接相關，故一併修正。
3. *文件語意過強（nit）*：`ui-development.md §6` 補上「須具 exit 區塊」限定，與引擎/CLI 對齊。

**修正後驗證**：全套件 122 passed（+2 新測試）。

**關聯**：todo A1／D1、[[record-fill-strategy-attribution]]、UI 分期 C1 前置。

---

## 2026-06-14 ｜ 儀表板預設日期改最近 run 日 + 移除底部字串

**背景／觸發**：使用者反映明日訊號、執行事件、risk_exit 監控、持倉監控欄皆空。診斷 live DB 後確認**非 bug**：(1) 預設日期是今天（週日 06-14 無 run）→ 全空；(2) 8 筆訊號全屬已退役 `trend_pullback`，分布於 06-10/06-11，06-12 無訊號、僅 1 筆 APPROVAL_INVALID 事件；(3) 9 筆持倉全 MANUAL → risk_exit 監控結構性為 0。

**怎麼動**：
- `dashboard.latest_run_date()`：取最近有 daily_run 的日期；`server.index` 無 `view_date` 時預設落於此（避免假日/未跑日全空）。
- 移除 `base.html` 底部「唯讀儀表板 · 區網單人使用」字串與對應 `.foot` CSS。
- 新增測試 `test_dashboard_defaults_to_latest_run_date`。

**為什麼這樣動**：不捏造資料；以「預設落在最近有資料的日期」改善空白觀感，其餘空段為 go-live 前的真實狀態（策略尚未實際交易、持倉全 MANUAL）。MANUAL 不受監控連動 [[record-fill-strategy-attribution]] 待修項——修好後手動成交可歸策略、納入 risk_exit。

**驗證**：115 passed；live 預設日期落 2026-06-12，APPROVAL_INVALID 事件顯示、底部字串移除；歷史訊號可由日期選 06-11 檢視（4 筆 trend_pullback）。

---

## 2026-06-14 ｜ Web 儀表板常駐 (systemd) + 區網上線

**背景／觸發**：phase 1 儀表板已用 nohup 跑起、手機經 `http://192.168.50.109/trading/`（nginx→127.0.0.1:8800）可見，但 nohup 重開機不會自啟。長期工具需常駐。

**怎麼動**：新增 `deploy/trading-web.service`（systemd system unit，User=hom、用 `.venv` 執行 uvicorn、綁 127.0.0.1:8800、root_path=/trading、Restart=on-failure、開機自啟）+ `deploy/README.md` 安裝/管理說明。

**為什麼這樣動**：
- system unit（非 user unit）→ 不需登入即於開機啟動，符合區網常駐主機。安裝需一次 sudo。
- 綁 127.0.0.1：8800 對外被防火牆擋，僅 nginx 反代，攻擊面最小。
- 直接 ExecStart uvicorn（不經 shell）較易被 systemd 追蹤；`web_ui.sh` 保留供手動執行。

**考慮點／取捨**：未開 `--reload`（正式服務不需熱載，改 code 後 `systemctl restart`）。最小安全強化（NoNewPrivileges/PrivateTmp）；未加 ProtectSystem 以免擋到 sqlite 檔存取。

**驗證**：安裝由使用者以 sudo 執行（harness 的 `!` 無法輸入 sudo 密碼）；本機 `curl 127.0.0.1:8800/` 200、手機 `/trading/` 已確認可見。

---

## 2026-06-14 ｜ 唯讀 Web 儀表板 第一版（FastAPI + Jinja，分期 1）

**背景／觸發**：UI 分期第一步 —— 唯讀儀表板，服務手機/區網查看與每日影子核對。使用者確認：子路徑 `ip/trading`、預設畫面、不認證、預設帳戶 `simulation-main`、頁面載入即時性。

**怎麼動**：
- `src/application/services/dashboard.py`：**service 層讀取側雛形**（純讀 projection/SQLite，回傳結構化 dict）。提供 `build_dashboard` / `list_accounts` / `list_reports` / `read_report`。
- `src/web/server.py`：FastAPI app，`root_path` 由 `TRADING_WEB_ROOT_PATH`（預設 `/trading`）控制；路由 `/`（儀表板,可切帳戶/日期）、`/reports`、`/reports/{name}`(防目錄穿越)、`/healthz`。
- 模板 `src/web/templates/*`（base/dashboard/reports）+ `static/style.css`（基本款、行動友善、單欄優先）。連結以 Jinja 全域 `base`=root_path 前綴，子路徑與本機直連皆正確。
- `scripts/web_ui.sh`：uvicorn 啟動（綁 127.0.0.1:8800）。`docs/nginx-trading.conf.sample`：子路徑反代範例（proxy_pass 結尾斜線剝前綴 + X-Forwarded-Prefix）。
- `tests/unit/test_web_server.py`：TestClient 冒煙（healthz、儀表板渲染、報告清單、404、目錄穿越阻擋）。

**為什麼這樣動**：
- 讀取邏輯先抽 service 層（不直接在路由查 DB），為日後寫入操作與 CLI TUI 共用鋪路；寫入暫不做（go-live 安全）。
- 唯讀、不認證（信任區網、不對外）；DB 每請求開關。
- 風格先基本款，neumorphism 之後再換（不影響資料層）。

**過程中抓到的問題**：
- 新版 Starlette `TemplateResponse` 簽名改為 `(request, name, context)`；舊式 `(name, {"request":...})` 會把 context dict 當成 template name → `unhashable type: dict`。已改新簽名。
- 測試種子帳戶 balance 與 cash_ledger 不一致會被 reconcile 正確擋下（印證 reconcile 有效）；改種一致帳戶。

**優缺點**：優 — 零寫入風險、service 層起步、手機/區網可用、報告可線上瀏覽。缺 — 尚無圖表（權益曲線待後續）、無自動刷新（需手動重整,符合一天一更新的資料節奏）。

**驗證**：全套件 **114 passed**；對 live `data/app.db` TestClient 渲染 2026-06-12，現金 5,987、MANUAL 持倉、各區段皆正確輸出；`/healthz` ok、`/reports` 200。

---

## 2026-06-14 ｜ 建立 uv venv + 修正依賴宣告缺漏（pytz）

**背景／觸發**：UI/Web 需引入相依套件，使用者決定改用 venv 並偏好 uv。需先確認建 venv 對現行指令的影響。

**影響檢查（重要）**：`scripts/run_daily_sim.sh` 與 `shadow_daily.sh` 已寫「有 `.venv/bin/python` 則優先用、否則 python3」。故一旦建 `.venv`，每日流程會**自動改用 venv**，venv 必須裝齊全部依賴（含 shioaji），否則日跑會壞。今天為週末非交易日，是安全切換+驗證窗口。

**怎麼動**：
- 新增 `requirements-web.txt`（fastapi/uvicorn/jinja2/python-multipart/httpx），與核心 `requirements.txt` 分離。
- `uv venv --python python3`（解析到 CPython 3.11.13）；`uv pip install -r requirements.txt -r requirements-web.txt`。
- venv 跑全測試**揪出隱性依賴**：`pytz` 被 `src/market_data/provider.py` 執行期使用，卻**從未宣告於 requirements**（先前靠系統環境碰巧有）。補進 `requirements.txt`。

**考慮點／取捨**：
- venv 解析到 **Python 3.11.13 + shioaji 1.5.3**（系統原為 3.10 + shioaji 1.3.2），因 requirements 用 `>=` 未鎖版。風險為「新版/新 Python 破壞既有行為」→ 以全測試驗證收斂。
- 依賴分兩檔（核心 vs web）：優點是只跑交易核心者可略過 web stack；缺點是多一個檔。
- 未鎖版（沿用專案既有 `>=` 風格）：優點維護簡單；缺點重建可能再漂版，日後可用 `uv pip compile` 產 lock。

**優缺點**：優 — 環境隔離、uv 安裝快、順手揪出 pytz 宣告缺漏（潛在 Shioaji 同步炸點）。缺 — Python/套件版本較系統新，需持續以測試把關。

**驗證**：`.venv` 下 `pytest` **109 passed**；`-m app portfolio reconcile` 通過。`.venv` 已在 `.gitignore`。

---

## 2026-06-14 ｜ Web/CLI UI 架構與技術選型（討論定案中）

**背景／觸發**：使用者要為系統做長期使用的 UI（單人、區網主機、手機經內網查看、SSH/本機操作），且需要寫入操作（設定手動成交結果、拒絕訊號等）。主機已有 nginx 並接了其他服務。未來想套用 neumorphism 風格（https://github.com/joshhu/uitest 02-neumorphism），但非現在。

**決策 1 — UI 寫入 ⇒ 必須抽 service 層**
- 為什麼：UI 寫入若直接改 DB，會繞過 FIFO 隔離、冪等、授權閘門等 invariant。寫入必須走與 CLI 相同的、已驗證的邏輯。
- 怎麼做：從 1853 行 `src/cli.py` 抽出 `src/application/services/` 純函式層（輸入參數 → 操作 projection/engine → 回傳結果），CLI / Web / 未來 TUI 共用。
- 取捨：一次還掉 CEO review 列的第一順位技術債；但工程量較大，需測試保護。`src/application/reporting/daily_report.py` 已是純讀層雛形。

**決策 2 — Web 技術：FastAPI + Jinja + HTMX（反轉先前的 Streamlit 推薦）**
- 初始傾向 Streamlit（開發最快）。**反轉原因**：使用者明確表示未來要套特定 neumorphism HTML/CSS 風格。
- 比較：
  - Streamlit：開發最快，但深度自訂 CSS/版面困難，難還原指定設計稿；接 nginx 需 websocket proxy 設定。
  - FastAPI + Jinja + HTMX：對 markup/CSS 完全掌控（neumorphism HTML 可直接模板化）；標準 ASGI，nginx 反向代理最單純；單人填表單 HTMX 原生支援。工程量略大。
- 結論：未來客製化需求 + 已有 nginx ⇒ 選 FastAPI 系。優：長期可控、風格自由、部署標準。缺：比 Streamlit 多前期工。

**決策 3 — 分期：先唯讀、後寫入（go-live 安全）**
- 為什麼：go-live 影子驗證未過前，任何能改帳務的新入口都是新風險面。
- 順序：(1) 唯讀 Web 儀表板（純讀 projection，零風險，立即服務手機/區網查看與每日影子核對）→ (2) 抽 service 層 → (3) 補寫入操作 + CLI TUI。
- 取捨：唯讀畫面之後沿用、不白做；最快見效又不破壞 go-live 安全。

**決策 4 — 部署走既有 nginx**；**風格**先用基本款，neumorphism 列入後續（非現在）。

**狀態**：技術方向討論中，待使用者確認分期與技術棧後動工。關聯記憶 `ui-requirements`、`record-fill-strategy-attribution`。

---

## 2026-06-14 ｜ 修正 portfolio reconcile 誤報失敗（commit 0c991dd）

**背景／觸發**：使用者要求跑 `portfolio reconcile`，輸出「Reconciliation failed! Errors found: - status」並 exit 1。直接以 Python 呼叫 `projection.reconcile()` 確認回傳為 `{"status": "RECONCILE_OK"}` ⇒ 帳務實際一致，是 **CLI 誤報**。

**根因**：`projection.reconcile()` 成功時回傳 `{"status":"RECONCILE_OK"}`（非空 dict），但 `cmd_portfolio_reconcile` 仍用舊契約 `if not errors:` 判斷。非空 dict 恆為真 ⇒ 永遠走 failed 分支，並 `for err in errors` 印出 dict 的 key `status`。

**怎麼動**：`src/cli.py` 判斷改為 `not result or (isinstance(result, dict) and result.get("status")=="RECONCILE_OK")`；新增回歸測試 `tests/unit/test_cli_commands.py::test_cmd_portfolio_reconcile_reports_success_on_ok`（OK 須印 successful、不得出現 failed）。

**為什麼這樣動**：最小變更修正契約不一致，不改動 `reconcile()` 既有回傳（其他呼叫端如 `daily_report.py` 已用對的契約）。

**考慮點／取捨**：本可改 `reconcile()` 回傳空 dict 表成功，但那會牽動所有呼叫端、風險較大；改 CLI 判斷影響面最小。

**優缺點**：優 — 一行修好、有回歸測試、低風險。缺 — 同類「dict 真值誤判」可能散落他處（未全面掃，列為待觀察）。

**為什麼重要**：runbook 叫操作者每週跑 reconcile，卻永遠回報失敗 ⇒ go-live 危害（操作者無法信任對帳輸出）。

**驗證**：live `data/app.db` reconcile → "Reconciliation successful"；全套件 109 passed。

---

## 2026-06-14 ｜ 每日影子報告產生器 + 影子先行流程（commit a833141）

**背景／觸發**：CEO review（HOLD SCOPE）指出兩個 go-live 阻擋項 —（1）多策略閉環從未在真資料跑過一筆策略 BUY（`position_high_watermarks=0` 為證）；（2）cron run FAILED 無告警。且 `run-daily` 的 Stage 4「Reporting」原為空殼（只翻狀態、不產報告）。

**怎麼動**：
- 新增 `src/application/reporting/daily_report.py`：純讀 DB/projection 的文字報告產生器，涵蓋 RUN 狀態、**RISK_EXIT 監控部位數+移動停利水位**（補 §7 觀測缺口）、成交、明日訊號、執行事件審計、策略別損益、對帳。
- `scripts/daily_report.py`：CLI 入口，落檔並記錄路徑（`<acct>_<date>.txt` / `LATEST.txt` / `INDEX.tsv`），stdout 末行印 `REPORT_PATH=`。
- `scripts/shadow_daily.sh`：cron 可掛 = run-daily(paper) + 報告 + run 非 0 時輸出 `⚠ ALERT`。
- 5 個單元測試；`.gitignore` 忽略生成物；runbook 補「上線 Gate」與核對清單。

**為什麼這樣動**：
- 報告產生器**刻意獨立於 cli.py god-file**（純讀、無副作用、可測），作為未來 service/讀取層的雛形。
- 用獨立 script + shell wrapper 而非塞進 cli.py，避免加重 churn 之冠的 god-file。
- 系統本質是 paper-trading（FakeBroker、Hard Boundary 永不串實盤），故 `run-daily` 本身即「影子」，無需另建 dry-run 機制。

**考慮點／取捨**：曾考慮把報告直接寫進 runner Stage 4（每次 run-daily 自動產），但那較侵入、需改 runner 並補測試；先以獨立 script 解耦，日後可再由 Stage 4 呼叫。

**優缺點**：優 — 零風險、即時補上觀測、報告 §6 已在 live 揪出退役 trend_pullback 的舊 APPROVAL_INVALID（印證價值）。缺 — 報告非由 run-daily 自動觸發，需 shell wrapper 串接。

**驗證**：108 passed；對 live `data/app.db` 實測報告產出正常。

---

## 2026-06-14 ｜ go-live 回滾錨點 + 殘留授權清理（commit fc432d4）

**背景／觸發**：CEO review 列上線前置 —（1）高/不可逆決策卻無 git tag 回滾錨點；（2）磁碟殘留 legacy `active-approval.json`（指向已退役的 trend_pullback）。

**怎麼動**：`git rm artifacts/approvals/active-approval.json` 並 commit；打 annotated tag `v0.1.0-pre-golive`。

**為什麼這樣動**：先驗證 `src/approval/store.py:29-39` — active map（`active-approvals.json`）存在時 legacy 單檔**永不被讀**，故刪除對 runtime 無害，僅清掉誤導除錯者的殘留。tag 釘在清理後的乾淨狀態，作為任何壞日的回滾點。

**優缺點**：優 — 零風險、回滾有依據。缺 — 無。

**驗證**：working tree clean；tag 可見。

---

## 2026-06-15 ｜ B2 go-live 啟用步驟完成（Discord 告警實測）

**背景／觸發**：B2（`src/notification/discord_alert.py` + `shadow_daily.sh`）程式面已就位，但 go-live 啟用步驟（`daily_runbook.md` §go-live 2）尚未執行：缺 `config/alert.local.yaml`、未實測。

**怎麼動**：
1. 修 `DiscordAlertConfig`：原先只用 `os.getenv("DISCORD_BOT_TOKEN")`，但 `~/.openclaw/.env` 不在 `src/config.py` 的 `load_dotenv()`（讀 cwd `.env`）範圍內，token 永遠讀不到。改為在環境變數未設時，用 `dotenv_values()`（不污染 `os.environ`）讀取 `~/.openclaw/.env` 的 `DISCORD_BOT_TOKEN`。
2. `cp config/alert.local.yaml.example config/alert.local.yaml`，填入 channel_id（gitignored，不入版控）。
3. 實測 `.venv/bin/python -m src.notification.discord_alert "go-live 告警測試"` → exit 0，Discord 頻道收到紅色 embed 告警。

**為什麼這樣動**：`dotenv_values()` 回傳 dict 而非寫入 `os.environ`，避免影響既有測試（尤其 `test_discord_config_from_local_file` 原本假設無 token，若用 `load_dotenv()` 會讀到真實機器上的 `~/.openclaw/.env` 而誤判為已配置）。

**優缺點**：優 — go-live gate #3 全鏈路（程式 + 設定 + 實測）皆完成。缺 — 無。

**驗證**：`pytest tests/unit/test_discord_alert.py` 9 項全綠（新增 1 項覆蓋 openclaw env fallback）；Discord 實際收到測試告警。

---

## 2026-06-24 ｜ 全域 signal bundle 跨帳號污染修補（no-add + exit leakage）

**背景／觸發**：simulation-main（影子）3090 日電貿出現「均價 321.86、今日收盤 302 小虧卻觸發移動停利」。追根（全程 DB 查證）發現**不是 risk_exit 或 no-add 邏輯錯，而是架構錯位**。

**根因（三層因果鏈）**：
1. `signal_bundles` **無 `account_id` 欄 → 訊號 bundle 全域共用**；兩條 cron（國泰 15:10 先、simulation-main 15:12 後）以 `bundle_id` 為 PK 走 idempotent `_save_bundle`（已存在即跳過不重產）。⇒ **誰先跑就把誰的持倉視角烤進共用 bundle**，另一帳號盲目重用。
2. **進場(no-add)**：06-22 國泰先跑、其 trend_breakout 名下無 3090（碰過的是 MANUAL、06-16 已賣光）→ no-add 對它不該觸發 → 3090 創 20 日新高 → 吐 BUY 進共用 bundle；sim idempotent 重用 → 自己持 4 股卻照單 → 06-23 allocator 算出 `is_new_position=False` 仍走 `increase_long` 加買 59 股 → 新 59 股繼承舊 lot 峰值 335.50、隔日即被移動停利停在虧損。
3. **出場(exit leakage，同根因更危險)**：exit bundle_id `bundle-{date}-{strategy}-exit` 也 account-agnostic，且只在「有出場訊號」時建。兩帳號同日同策略不同持倉時，先跑者的 exit bundle 被後跑者 idempotent 重用 → 後者**漏掉自己該觸發的停損**（或被迫執行不屬於自己的 SELL）。目前沒爆只因國泰幾乎無 strategy 持倉＝靠運氣。

**正解原則**：進場訊號＝市場事實（突破發生了）→ 全域共用 OK，per-account 在 allocator 把關；出場訊號＝帳號專屬事實（成本/高水位/持有天數）→ 本就不該全域共用，必須 per-account。

**怎麼動（兩部分）**：
1. **no-add 移到 allocator（commit 1）**：[allocator.py](../../src/trading/allocator.py) `MultiStrategyAllocator.plan` 算出 `is_new_position` 後若 `not is_new_position` → `ALREADY_HOLDING` 擋下 BUY + `continue`。位置不變式的權威執行點＝動錢當下對本帳號活的持倉；allocator 本就有 per-account `local_positions`、本就算出 `is_new_position`，只差沒擋。唯一 chokepoint（回測 + 兩 live 帳號全走 `engine → allocator.plan`），一處全蓋；SELL 在 BUY 段前處理不受影響（S5）；順帶治掉 stale-peak。`increase_long` 自然成 dead code（留 ternary + `ponytail:` 註記，未來加碼策略才開 per-strategy `allow_add`）。
2. **exit bundle account-scope（commit 2）**：`signal_bundles` 加 nullable `account_id`（NULL=全域 entry；set=私有 exit；沿用 [db.py](../../src/portfolio/db.py) 既有 idempotent `PRAGMA→ALTER` 慣例）；exit bundle_id → `bundle-{date}-{strategy}-{account}-exit`（消跨帳號 idempotent 碰撞 → 修漏停損）；`_save_bundle(...,account_id=None)` 落地、Stage 3a 出場傳 account、3b 進場傳 None；`_find_bundles_for_execution(date, account_id)` → `WHERE target_execution_date=? AND (account_id IS NULL OR account_id=?)`（修執行到別人的 SELL），更新 3 呼叫點。回測走 run-scoped finder、天生隔離、不動。

**為什麼這樣動**：entry 全域共用是對的（突破是市場事實，避免重產），錯只在「位置相關閘放在共用層」；把 no-add 下放到 per-account 的 allocator、把 exit 上移成 per-account 私有 bundle，各歸其位。`account_id` 用 nullable 欄而非改 bundle_id 解析（國泰/strategy 名含 `-`/`_`，suffix 解析脆弱）；NULL=全域確保存量/待執行的舊 bundle 仍照常執行（使用者要求「讓 sim 那 63 股 06-24 移動停利跑完」不受影響，已用 app.db 副本驗證）。

**優缺點**：優 — 根治兩類跨帳號污染、SELL 安全補上、stale-peak 連帶消失、migration 加性冪等對 live 零破壞。缺 — entry/exit 全域 vs 私有的非對稱稍增認知成本（已於程式註解與本 log 說明）。

**驗證**：`pytest tests` **314 passed**（+ allocator no-add ×2、finder per-account scope ×1、risk_exit exit-id 斷言收緊）；對 `data/app.db` 副本跑 `init_db` 二次 → account_id 加成功、既有 bundle 全 NULL、資料筆數不變、冪等；副本上 `_find_bundles_for_execution('simulation-main', 06-24)` 確認待執行 3090 exit 仍會被 sim 執行（63 股照賣）。sim 那 63 股依使用者指示不攔、讓 06-24 照常執行。

---

## 2026-06-24 ｜ R-T4b Track 2 完成：首批 PIT 公平裁決（專案首個非 INVALID）

**背景／觸發**：固定 21 檔 universe 是今日手挑＝後見之明，所有回測只能 diagnostic→INVALID。Track 2 用「每月再平衡、依當下已知成交額 top-N」的 PIT 流動性 universe 取代，首次能對策略下正式裁決，公平回答「+122% 是真 edge 還是後見之明」。

**怎麼動（B1→B4 全鏈路在 research.db，gitignored）**：
1. **B1 擴大回補**（睡眠期間並行完成）：research.db 從 ~45k bars/25 檔 → **929,831 bars / 587 檔 / 2018~2026，amount 已填**。
2. **B2 建 PIT policy**：`market build-universe --policy-version liquidity-top150-v1 --top-n 150 --lookback 20`→ 102 次月再平衡、15,150 列、**451 檔**不同標的；PIT 驗證（成分隨時間變、known_at<=R、首 rebalance 前回 0 檔無外洩）。
3. **B4b 註冊 regime_gate**：`scripts/register_regime_gates.py` 忠實轉錄三支 thesis **看結果前已寫死**的門檻（write-once）。
4. **B4a 三支 PIT 重跑**（copy-per-run，--universe-policy liquidity-top150-v1，2018~2026，初始 30 萬）。

**結果（決勝 gate＝逐筆期望值 bootstrap 5% CI 下界 ≥ 0）**：
| 策略 | diagnostic（後見之明） | **PIT 總報酬** | maxDD | 有效樣本 | **期望值 CI 下界** | **裁決** |
|---|---|---|---|---|---|---|
| trend_breakout | +32.9% | **+8.75%** | 27.67% | 366 | **+1.35** ✓ | **RESEARCH_PASS** |
| pullback_rebound | +44.2% | +0.47% | 16.07% | 285 | −56.75 ✗ | REJECTED |
| trend_rider | **+121.9%** | +12.51% | 15.91% | 122 | −196.85 ✗ | REJECTED |

**為什麼這結果重要（report ≠ edge）**：
- **大反轉**：diagnostic 下最不起眼的 trend_breakout（曾判「edge≈5 筆運氣」）是**唯一**逐筆 edge 撐過公平、無倖存者偏誤、廣標的池的策略（366 有效筆、期望值下界正、HHI 0.023 極分散）。
- diagnostic 下的明星 trend_rider（+121.9%、Sharpe 1.20）PIT 崩到 +12.51% 且 **REJECTED**——「讓贏家跑」的寬停損在 PIT 會夾帶下跌到底的輸家、期望值 CI 下界 −196.85（最差）。**+122% 幾乎全是後見之明污染，治理如預測抓到了**。
- 注意 trend_rider PIT 總報酬（+12.51%）> trend_breakout（+8.75%）卻被否決：gate 看的是**逐筆期望值穩健性**（CI 下界），非帳面報酬。trend_rider 的正報酬來自少數集中交易、可能輕易翻負＝不穩健。

**trend_breakout RESEARCH_PASS 的誠實註腳（過 gate 但經濟邊際）**：+8.75%/8.5 年（CAGR ~1%）；**成本吃毛利 68.7%**；去最佳 5 筆 PnL 轉負（−2,115）；年化報酬 CI 跨零（−4.2%~+8.5%）；輸 0050 buy-hold（+34.6%）、被等權 universe（+118.9%）輾壓。RESEARCH_PASS＝「逐筆 edge 統計穩健、可進影子驗證」，**非**「會賺大錢」。

**優缺點**：優 — 專案首批正式裁決、北極星「驗證是否真賺錢」首次有公平答案；治理價值實證（擋掉兩支後見之明明星、放行唯一穩健者）。缺 — 殘留 survivorship（roster 單一快照漏部分早期下市股，已揭露）；regime/bear gate 因 regime 偵測未建仍未評估。

**驗證**：三支報告於 `artifacts/reports/backtest/`（gitignored）；裁決＝五級狀態之一、非 INVALID；`register_regime_gates.py` 可重現門檻。`pytest tests` 321 passed（PIT 程式碼）。
