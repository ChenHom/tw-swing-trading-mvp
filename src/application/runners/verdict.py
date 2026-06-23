"""S1 裁決狀態機（P1-T7，Phase 1 範圍）。

完整五級狀態 INVALID/REJECTED/RESEARCH_PASS/SHADOW_PASS/CAPITAL_APPROVED 是 S1 的定義；
本模組（reuse BacktestRunner 的研究輸出）只能產出前三級——SHADOW_PASS 需影子執行證據
（Phase 4 才建）、CAPITAL_APPROVED 需核准內容欄（Phase 5 才建），目前都無資料來源，
不可在此假裝可裁決。
"""
import sqlite3
from datetime import date
from typing import Optional

INVALID = "INVALID"
REJECTED = "REJECTED"
RESEARCH_PASS = "RESEARCH_PASS"
SHADOW_PASS = "SHADOW_PASS"
CAPITAL_APPROVED = "CAPITAL_APPROVED"

# 「窗涵蓋 2022 完整空頭」＝窗須完整覆蓋整個 2022 年，非僅與其有交集。
BEAR_WINDOW_2022_START = date(2022, 1, 1)
BEAR_WINDOW_2022_END = date(2022, 12, 31)


def get_regime_gate_thresholds(conn: sqlite3.Connection, strategy_id: str, strategy_version: str) -> Optional[dict]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT regime_definition_version, max_regime_drawdown, min_expectancy_ci_lower,
               max_bear_underperformance, min_effective_sample_size, max_profit_concentration
        FROM regime_gate_thresholds
        WHERE strategy_id = ? AND strategy_version = ?
        """,
        (strategy_id, strategy_version),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def set_regime_gate_thresholds(
    conn: sqlite3.Connection, strategy_id: str, strategy_version: str, regime_definition_version: str,
    max_regime_drawdown: float, min_expectancy_ci_lower: float, max_bear_underperformance: float,
    min_effective_sample_size: int, max_profit_concentration: float,
) -> None:
    """門檻須在看到任何 backtest 結果前寫入；同策略版本只能寫一次（避免看結果後回頭調寬）。"""
    conn.execute(
        """
        INSERT INTO regime_gate_thresholds (
            strategy_id, strategy_version, regime_definition_version, max_regime_drawdown,
            min_expectancy_ci_lower, max_bear_underperformance, min_effective_sample_size,
            max_profit_concentration, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(strategy_id, strategy_version) DO NOTHING
        """,
        (strategy_id, strategy_version, regime_definition_version, max_regime_drawdown,
         min_expectancy_ci_lower, max_bear_underperformance, min_effective_sample_size,
         max_profit_concentration),
    )
    conn.commit()


def evaluate_verdict(
    backtest_result: dict, start_date: date, end_date: date, is_diagnostic_universe: bool,
    regime_gate: Optional[dict],
) -> dict:
    """回 {"verdict", "diagnostic_result", "reasons"}。diagnostic 路徑只能淘汰明顯爛策略、
    不能晉級——與下方正式 RESEARCH_PASS 路徑互斥，不存在「RESEARCH_PASS-diagnostic」混合狀態。"""
    if is_diagnostic_universe:
        sharpe = backtest_result["statistics"]["sharpe_ratio"]
        diagnostic_result = (
            "明顯爛（Sharpe<=0），可淘汰" if sharpe <= 0
            else "未達淘汰門檻；正式裁決仍須改用 PIT universe"
        )
        return {
            "verdict": INVALID, "diagnostic_result": diagnostic_result,
            "reasons": ["今日 universe 非 PIT，僅可 diagnostic，不得進入正式生命週期"],
        }

    covers_2022_bear_window = start_date <= BEAR_WINDOW_2022_START and end_date >= BEAR_WINDOW_2022_END
    if not covers_2022_bear_window:
        return {"verdict": INVALID, "diagnostic_result": None, "reasons": ["窗未硬性涵蓋 2022 完整空頭"]}

    if regime_gate is None:
        return {
            "verdict": INVALID, "diagnostic_result": None,
            "reasons": ["regime gate 門檻未寫入具體數值（看結果前無 gate，不可裁決）"],
        }

    stats = backtest_result["statistics"]
    robustness = backtest_result["robustness"]

    failures = []
    if stats["close_to_close_maxdd"] > regime_gate["max_regime_drawdown"]:
        failures.append("close_to_close_maxdd 超過 max_regime_drawdown")
    ci_lower = robustness["expectancy_bootstrap_ci_lower"]
    if ci_lower is None or ci_lower < regime_gate["min_expectancy_ci_lower"]:
        failures.append("expectancy_bootstrap_ci_lower 未達 min_expectancy_ci_lower")
    if robustness["effective_sample_size"] < regime_gate["min_effective_sample_size"]:
        failures.append("effective_sample_size 未達 min_effective_sample_size")
    hhi = robustness["profit_herfindahl_concentration"]
    if hhi is not None and hhi > regime_gate["max_profit_concentration"]:
        failures.append("profit_herfindahl_concentration 超過 max_profit_concentration")
    # max_bear_underperformance：須先有 regime 分段（S3，Phase 3+ 才建）才能算「空頭子窗」
    # 落後 benchmark 多少；regime 偵測尚未建立，此門檻先寫入但不參與 Phase 1 裁決。

    if failures:
        return {"verdict": REJECTED, "diagnostic_result": None, "reasons": failures}
    return {"verdict": RESEARCH_PASS, "diagnostic_result": None, "reasons": []}
