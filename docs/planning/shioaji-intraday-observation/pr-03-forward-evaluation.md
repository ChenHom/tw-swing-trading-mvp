# PR-3 — 盤中條件的前向對照與執行品質研究

狀態：**SPEC_CONFIRMED / IMPLEMENTATION_PENDING**（D4 / D7 / D10 已確認；只允許使用經授權收集的前向資料，不恢復 REJECTED 策略）
目的：**檢驗 Tick / BidAsk 是否帶來可驗證的執行品質增益**。做出負面結果是有效研究交付，不代表工程失敗。

## 問題與研究假設

目前真實帳號進場清單為空，兩個舊進場策略已被 REJECTED；不可以把盤中觀察當成正式策略修復。先測「指定隔日候選或觀察計畫後，盤中確認條件對滑價、錯過機會及假突破的影響」，再討論更高層的策略淨期望值。

在看新資料前，把假設與預設門檻寫入版本化 experiment manifest，至少比較：

- BASELINE：原本在訊號產生時已定義的時點/條件，依同樣可觀測即時資料估算可執行價與成本；遵循既有策略授權（禁用即無正式 BUY）。
- CHALLENGER：只在事前指定的 Tick / BidAsk observation window 滿足時，變更觀察或虛擬進場時點；沒有滿足要記錄 MISSED / NO_TRADE。
- SHADOW ONLY：兩組為研究中的假設交易，不呼叫 broker API、不改 app.db / fills；未來若要正式放行必須走既有研究裁決與人工批准。

若基準進場時點無法從現有 plan 還原，結果標 INCOMPARABLE，不可為了追求收益自行選有利價格。

## 資料來源 / PIT 邊界

使用 PR-1/2 的 raw Tick、BidAsk、health、之前已形成的 candidate bundle、support/resistance plan、成本與零股成交規則。每個研究樣本記錄 as_of、signal_generated_at、entry_decision_at、price_observed_at、quote_received_at、filter_version、reference_session、account_mode、fills_assumed=false、data_quality。

延遲行情、重連缺口、未提供 BidAsk、只有日 K，應分為 BLOCKED / INSUFFICIENT_DATA，而不是填補為獲利成交。五檔是可見掛單快照，**不是完整隊列、不能保證可依顯示價與量成交**；需報上下界或 conservative 假設，勿寫實際成交。零股需使用零股的成交/報價時點與可適用成本，不以整股五檔推估。

歷史無完整五檔，則從正式啟用收集當日起做前向測試；2018–2026 的日 K 只能作波段背景與基準，不能假裝驗證五檔策略。樣本不足時只能標 EVIDENCE_PENDING。

## 比較指標（事前固定）

- 主要：基準與觀察組之進場可執行價差／估計滑價（bps，含費率情境）、未成交/錯失比率、符合條件後的延遲、資料不可用率。
- 次要：指定後續窗口（例如收盤與隔日）的不利價格移動、MFE/MAE、假突破率、後續持有期淨報酬估算；以歷史基準明訂持有/出場，不能隨結果挑窗口。
- 研究裁決：交易筆數、有效樣本、信賴區間 / bootstrap、分年度 / 市場狀態 / 流動性分組、過度濾除、極端交易貢獻。保留 INVALID/EXCLUDED/MISSED 的完整稽核軌跡。
- 報告同時附 baseline、challenger、delta、樣本大小、排除理由、品質缺口、版本/輸入雜湊，否則視為不可裁決。
- 零股與整股分層分析，樣本不足時不加總為有意義結論。

## 已確認：前向研究門檻（D7；2026-10-08）

- **至少 60 個交易日**的前向行情資料，**至少 100 筆獨立且事前定義的候選進場機會**。
- 最後至少 **20 個交易日**按時間順序封存為樣本外；這 20 日可包含於前述 60 日，但不得參與規則調參。
- 「獨立機會」以已知 signal / watch-plan 為單位；同一事件的多筆 Tick 或五檔不重複計數，baseline / challenger 必須配對。
- 未達門檻或關鍵資料缺漏，裁決為 `EVIDENCE_PENDING`；即使達標也只是執行品質初步研究，不得使 REJECTED 策略恢復 BUY。
- 指標、成本、入場規則、排除條件與樣本切分在研究觀察前鎖定；既有波段策略至少五年資料與 100 筆交易等驗證要求保持獨立。

## 建議變更與成果

新增 src/research/intraday/ 下的 experiment manifest / sampler / replay evaluation / report builder（不要污染目前研究 DB），輸出 artifacts/reports/intraday-evaluation/{experiment_id}/ 下的 manifest.json、cohort.csv/jsonl、comparison.json、comparison.md、exclusions.jsonl、lineage.json。
從既有 docs/strategies / Research Ledger 紀錄假設、試驗次數與參數；基準策略已退役，研究結果不得直接改 config/trading.yaml 的 account_overrides。

## 測試 / 驗收

1. 合成 fixture 覆蓋 baseline / challenger 同樣資料、不同 entry window 的對照，無未來資訊、無相同標的重複計次。
2. 將「沒進場」納入分母及 opportunity cost；剔除後不能只看剩下交易的勝率。
3. 禁用策略仍可做影子研究，但不產生正式新 BUY、虛構 broker fills。
4. 缺 Tick / BidAsk、gap、假日、無效支撐、時間先後矛盾、無價可成交，全部有顯式分類。
5. 研究 manifest / raw hash / code version 不變時 replay 輸出相同，日期與交易時段一致。
6. 工程完成條件是嚴格、可反駁的研究報告可重現；經濟上可否上線須另行裁決：沒有可靠正面證據就停在觀察階段。

## 明確不做

不對歷史五檔缺口做擬合補造、不直接自動 tune threshold、不因樣本內勝率增加解禁 REJECTED 策略、不聲稱基於五檔可確定實際成交、未經驗證不修改風控/買進授權。


## 已確認：實測與研究邊界（D10；2026-10-08）

- 研究框架可先使用 fake SDK / 既有可合法使用的 replay fixtures 開發，**不得因研究需要自行啟用真實行情**。
- 首次真實唯讀行情 smoke 與每日前向資料收集各需獨立明確使用者核准；未核准則停在可測試的離線研究框架。
- D7 的 60 交易日、100 獨立候選機會、最後 20 日時序樣本外，是取得初步結果的必要門檻；尚未蒐集完成時必須標 `EVIDENCE_PENDING`，不能宣稱研究已經證明策略有效。
