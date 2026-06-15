"""list_backtest_results / read_backtest_result 直測。"""
from datetime import date

from src.application.reporting.backtest_report import write_backtest_result
from src.application.services import dashboard as dash


def _seed(base_dir, run_id, strategy_id="trend_breakout", final_equity=312345, total_pnl_bps=411):
    result = {
        "run_id": run_id,
        "account_id": f"backtest:{run_id}",
        "equity_curve": [
            {"date": date(2025, 1, 2), "cash": 300000, "position_value": 0, "equity": 300000},
            {"date": date(2025, 1, 3), "cash": 280000, "position_value": 32345, "equity": final_equity},
        ],
        "statistics": {
            "initial_cash": 300000,
            "final_equity": final_equity,
            "total_pnl": final_equity - 300000,
            "total_pnl_bps": total_pnl_bps,
            "max_drawdown": 0.0234,
            "win_rate": 0.6,
            "profit_factor": 1.8,
            "avg_profit": 1000.0,
            "avg_loss": -500.0,
            "trade_count": 5,
        },
    }
    return write_backtest_result(
        result, strategy_id=strategy_id, start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3), initial_cash=300000, base_dir=str(base_dir),
    )


def test_list_empty_dir_returns_empty(tmp_path):
    assert dash.list_backtest_results(base_dir=str(tmp_path / "nope")) == []


def test_read_missing_returns_none(tmp_path):
    assert dash.read_backtest_result("does_not_exist.json", base_dir=str(tmp_path)) is None


def test_list_and_read_roundtrip(tmp_path):
    base = tmp_path / "backtest"
    _seed(base, "bt-aaa11111", final_equity=312345, total_pnl_bps=411)
    _seed(base, "bt-bbb22222", final_equity=320000, total_pnl_bps=667)

    items = dash.list_backtest_results(base_dir=str(base))
    assert len(items) == 2
    # 新到舊
    assert items[0]["run_id"] == "bt-bbb22222"
    assert items[0]["final_equity"] == 320000
    assert items[0]["total_pnl_bps"] == 667
    assert items[0]["name"] == "trend_breakout_bt-bbb22222.json"
    assert items[1]["run_id"] == "bt-aaa11111"

    result = dash.read_backtest_result(items[0]["name"], base_dir=str(base))
    assert result["run_id"] == "bt-bbb22222"
    assert result["equity_curve"][1]["equity"] == 320000


def test_read_rejects_path_traversal(tmp_path):
    base = tmp_path / "backtest"
    _seed(base, "bt-aaa11111")
    assert dash.read_backtest_result("../../../etc/passwd", base_dir=str(base)) is None


def test_read_rejects_non_json_suffix(tmp_path):
    base = tmp_path / "backtest"
    base.mkdir()
    (base / "trend_breakout_bt-aaa11111.txt").write_text("not json", encoding="utf-8")
    assert dash.read_backtest_result("trend_breakout_bt-aaa11111.txt", base_dir=str(base)) is None
