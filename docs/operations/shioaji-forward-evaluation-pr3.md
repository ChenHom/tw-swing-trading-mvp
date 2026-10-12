# PR-3：盤中執行品質前向對照 — 離線研究框架

狀態：OFFLINE_IMPLEMENTED / FORWARD_EVIDENCE_PENDING（更新 2026-10-12；已同步合併後的 master）

## 2026-10-12：與 PR-1／PR-2 合併版本整合

- PR-1 [#2](https://github.com/ChenHom/tw-swing-trading-mvp/pull/2) 和 PR-2 [#3](https://github.com/ChenHom/tw-swing-trading-mvp/pull/3) 已合併至 `master`（`aa6277be`）。
- PR-3 已以**雙父節點非強制 Merge commit** 同步上述 `master`，並改 PR base 為 `master`。保留原有研究邏輯／測試，以及 P0/P1 的 Collector、安全、資料分流修正；GitHub 比較僅有 7 個 PR-3 檔案。
- GitHub Actions PR-3 workflow 已擴大為 PR1 + PR2（含 P0/P1）+ PR3 全部離線回歸，未連 SDK、未載憑證。
- 新增 CLI 端到端驗證：即使輸入為格式正確的 `MarketBook`、`ObservationEvent` 和候選機會，若 `data_health=UNKNOWN`，報告必須保持 `EVIDENCE_PENDING`、`SESSION_NOT_VERIFIED`、`fills_assumed=false`，不可將模擬行情誤寫成可信前向成果。
- **2026-10-12 的 30 秒 81/81 raw quality PASS** 與 **60 秒 SDK 1.3.2 157 筆 capture PASS** 只證明資料收集／落盤／安全停止；**不能直接當作 60 個交易日、100 個獨立機會、20 日 OOS 的 PR-3 證據**。兩次 Collector 的連續傳輸與全市場完整性仍為 `UNVERIFIED`。
- PR-3 維持 Draft / 研究證據待收集；合併 PR-3 程式碼與正式啟用前向策略是兩個獨立決策。PR-4 Dashboard 仍相依 PR-3，暫不連動修改。

## 交付
- 研究核心：`src/application/research/intraday_evaluation.py`
- CLI：`scripts/intraday_forward_eval.py`
- 輸入 JSON：`manifest`（預先凍結，包含 frozen_at / experiment_id / cost 假設）、`opportunities`（事前已知的獨立候選及 plan digest）、`books`（PR-2 MarketBook.as_dict）、`observations`（PR-2 ObservationEvent.as_dict）、`data_health`（UNKNOWN/HEALTHY/DEGRADED/FAILED）。
- 命令：`python3 -m scripts.intraday_forward_eval --input <offline-study-input.json> --output-dir artifacts/reports/intraday-evaluation`
- 每次產生 `<experiment_id>/comparison.json`、`comparison.md`、`manifest.json`、`cohort.csv`、`exclusions.jsonl`、`lineage.json`。同 experiment_id 禁止默默改寫不同輸入內容。

## 評估與證據
- Baseline：當時已知的進場計畫決策時間之後，最早取得的新鮮 best ask；Challenger：已收到的確認事件之後，最早可觀察的新鮮 best ask。皆僅是假設性可觀察報價、不是實際 fill/排隊成交。
- 同股票 / 市場 / 整股或零股 / plan digest / 規則版本 / SDK session 才配對；來自未來、延遲、過期與斷線樣本不確定時標明資料不足。
- 不確認而錯過的候選一律留在 cohort 分母；研究輸出含 MISSED、INVALID、INSUFFICIENT_DATA，不能只挑有利樣本。
- 有效報價資料至少 60 個交易日、100 個**獨立候選進場機會**，按最後 20 個交易日保留樣本外；不足時只回傳 EVIDENCE_PENDING。達標也只回傳 ELIGIBLE_FOR_MANUAL_REVIEW，不代表正式資金策略已通過。
- 研究框架沒有完整回報 PF、淨 expectancy、實際滑價、成交失敗率、MDD 或 5 年正式策略回測；這些要等完整真實資料與正式研究方法，不能事後捏造結果。
- 此階段 JSON cohort 的交易日合法性需在正式前向來源管線使用 XTAI 行事曆確認；沒有由券商／交易所原始資料背書者不可解讀為真實行情樣本。

## 離線驗證
- CI [PR-3](https://github.com/ChenHom/tw-swing-trading-mvp/pull/4)：63 項 PR-1～PR-3 測試通過。嚴禁載 CA、下單、修改 account DB、解禁 REJECTED 策略。
- 沒有連線到真實永豐行情。真實行情 smoke 與每日收集要依 D10 各自明確授權。
