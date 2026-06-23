"""P1-T7：S1 裁決狀態機（Phase 1 範圍：INVALID/REJECTED/RESEARCH_PASS）。"""
from datetime import date

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.verdict import (
    evaluate_verdict, get_regime_gate_thresholds, set_regime_gate_thresholds,
    INVALID, REJECTED, RESEARCH_PASS,
)

COVERS_2022 = (date(2021, 6, 1), date(2023, 6, 1))
MISSES_2022 = (date(2023, 1, 1), date(2023, 12, 31))

PASSING_GATE = {
    "regime_definition_version": "v1", "max_regime_drawdown": 0.5, "min_expectancy_ci_lower": -1.0,
    "max_bear_underperformance": 0.5, "min_effective_sample_size": 5, "max_profit_concentration": 0.9,
}


def _result(sharpe=1.0, maxdd=0.1, ci_lower=0.5, eff_n=10, hhi=0.3):
    return {
        "statistics": {"sharpe_ratio": sharpe, "close_to_close_maxdd": maxdd},
        "robustness": {
            "expectancy_bootstrap_ci_lower": ci_lower,
            "effective_sample_size": eff_n,
            "profit_herfindahl_concentration": hhi,
        },
    }


def test_diagnostic_universe_is_invalid_with_diagnostic_result_when_sharpe_negative():
    verdict = evaluate_verdict(_result(sharpe=-0.5), *COVERS_2022, is_diagnostic_universe=True, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == INVALID
    assert "淘汰" in verdict["diagnostic_result"]


def test_diagnostic_universe_is_invalid_but_not_eliminated_when_sharpe_positive():
    verdict = evaluate_verdict(_result(sharpe=1.5), *COVERS_2022, is_diagnostic_universe=True, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == INVALID
    assert "PIT" in verdict["diagnostic_result"]


def test_diagnostic_universe_never_promotes_regardless_of_gate():
    # diagnostic 路徑與正式 gate 互斥：即使 regime_gate=None 也不應走到「無 gate」分支的理由
    verdict = evaluate_verdict(_result(), *COVERS_2022, is_diagnostic_universe=True, regime_gate=None)
    assert verdict["verdict"] == INVALID
    assert verdict["diagnostic_result"] is not None


def test_window_missing_2022_is_invalid():
    verdict = evaluate_verdict(_result(), *MISSES_2022, is_diagnostic_universe=False, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == INVALID
    assert verdict["diagnostic_result"] is None
    assert any("2022" in r for r in verdict["reasons"])


def test_no_regime_gate_defined_is_invalid():
    verdict = evaluate_verdict(_result(), *COVERS_2022, is_diagnostic_universe=False, regime_gate=None)
    assert verdict["verdict"] == INVALID
    assert any("gate" in r for r in verdict["reasons"])


def test_regime_gate_violation_is_rejected():
    # maxdd=0.9 > max_regime_drawdown=0.5 → 違反
    verdict = evaluate_verdict(_result(maxdd=0.9), *COVERS_2022, is_diagnostic_universe=False, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == REJECTED
    assert any("max_regime_drawdown" in r for r in verdict["reasons"])


def test_regime_gate_violation_on_effective_sample_size():
    verdict = evaluate_verdict(_result(eff_n=2), *COVERS_2022, is_diagnostic_universe=False, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == REJECTED
    assert any("effective_sample_size" in r for r in verdict["reasons"])


def test_regime_gate_pass_yields_research_pass():
    verdict = evaluate_verdict(_result(), *COVERS_2022, is_diagnostic_universe=False, regime_gate=PASSING_GATE)
    assert verdict["verdict"] == RESEARCH_PASS
    assert verdict["reasons"] == []


def test_set_and_get_regime_gate_thresholds_roundtrip(tmp_path):
    db_file = tmp_path / "test_verdict.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))

    set_regime_gate_thresholds(conn, "stratA", "1.0.0", "v1", 0.3, 0.1, 0.2, 20, 0.5)
    gate = get_regime_gate_thresholds(conn, "stratA", "1.0.0")
    assert gate["regime_definition_version"] == "v1"
    assert gate["max_regime_drawdown"] == pytest.approx(0.3)
    assert gate["min_effective_sample_size"] == 20

    assert get_regime_gate_thresholds(conn, "stratB", "1.0.0") is None
    conn.close()


def test_regime_gate_thresholds_cannot_be_overwritten_after_first_write(tmp_path):
    db_file = tmp_path / "test_verdict_immutable.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))

    set_regime_gate_thresholds(conn, "stratA", "1.0.0", "v1", 0.3, 0.1, 0.2, 20, 0.5)
    set_regime_gate_thresholds(conn, "stratA", "1.0.0", "v2", 0.9, 0.9, 0.9, 1, 0.9)  # 看結果後想調寬，應被拒

    gate = get_regime_gate_thresholds(conn, "stratA", "1.0.0")
    assert gate["regime_definition_version"] == "v1"  # 第一次寫入的門檻不可被事後覆寫
    conn.close()
