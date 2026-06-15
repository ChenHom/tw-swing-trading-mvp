"""回測結果落檔（write_backtest_result）直測。"""
import json
from datetime import date

from src.application.reporting.backtest_report import write_backtest_result


def _result(**stats_overrides):
    stats = {
        "initial_cash": 300000,
        "final_equity": 312345,
        "total_pnl": 12345,
        "total_pnl_bps": 411,
        "max_drawdown": 0.0234,
        "win_rate": 0.6,
        "profit_factor": 1.8,
        "avg_profit": 1000.0,
        "avg_loss": -500.0,
        "trade_count": 5,
    }
    stats.update(stats_overrides)
    return {
        "run_id": "bt-abc12345",
        "account_id": "backtest:bt-abc12345",
        "equity_curve": [
            {"date": date(2025, 1, 2), "cash": 300000, "position_value": 0, "equity": 300000},
            {"date": date(2025, 1, 3), "cash": 280000, "position_value": 32345, "equity": 312345},
        ],
        "statistics": stats,
    }


def test_write_backtest_result_creates_files(tmp_path):
    out = tmp_path / "backtest"
    path = write_backtest_result(
        _result(),
        strategy_id="trend_breakout",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3),
        initial_cash=300000,
        base_dir=str(out),
    )

    assert path.exists()
    assert path.name == "trend_breakout_bt-abc12345.json"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "bt-abc12345"
    assert payload["strategy_id"] == "trend_breakout"
    assert payload["start_date"] == "2025-01-01"
    assert payload["end_date"] == "2025-01-03"
    assert payload["initial_cash"] == 300000
    assert payload["equity_curve"][0]["date"] == "2025-01-02"
    assert payload["equity_curve"][1]["equity"] == 312345
    assert payload["statistics"]["final_equity"] == 312345

    latest = (out / "LATEST.txt").read_text(encoding="utf-8").strip()
    assert latest == str(path)

    index_line = (out / "INDEX.tsv").read_text(encoding="utf-8").strip()
    parts = index_line.split("\t")
    assert parts[1] == "trend_breakout"
    assert parts[2] == "bt-abc12345"
    assert parts[3] == "2025-01-01"
    assert parts[4] == "2025-01-03"
    assert parts[5] == "312345"
    assert parts[6] == "411"
    assert parts[8] == str(path)


def test_profit_factor_inf_becomes_null(tmp_path):
    out = tmp_path / "backtest"
    path = write_backtest_result(
        _result(profit_factor=float("inf")),
        strategy_id="trend_breakout",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3),
        initial_cash=300000,
        base_dir=str(out),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["statistics"]["profit_factor"] is None


def test_index_appends_multiple_runs(tmp_path):
    out = tmp_path / "backtest"
    for run_id in ("bt-aaa11111", "bt-bbb22222"):
        write_backtest_result(
            {**_result(), "run_id": run_id, "account_id": f"backtest:{run_id}"},
            strategy_id="trend_breakout",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 3),
            initial_cash=300000,
            base_dir=str(out),
        )
    lines = (out / "INDEX.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split("\t")[2] == "bt-aaa11111"
    assert lines[1].split("\t")[2] == "bt-bbb22222"
