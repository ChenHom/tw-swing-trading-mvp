"""P1-T2：交易結構指標（賺賠比/Expectancy(R)/平均持有期/換手率）。
用合成 fifo_matches/fills 列直接寫入 db 斷言（沿用 P1-T1 的 __new__ 隔離測試手法）。"""
import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import BacktestRunner
from src.contracts.models import ExitParams
from src.strategy.registry import StrategyDefinition


@pytest.fixture
def runner(tmp_path):
    db_file = tmp_path / "test_trade_structure.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    r.exit_definitions = {}
    yield r
    conn.close()


def _insert_fill(conn, fill_id, account_id, symbol, side, quantity, price, filled_at):
    conn.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side,
                            quantity, price, filled_at, created_at)
        VALUES (?, ?, 'run-x', 'order-x', ?, ?, ?, ?, ?, ?, ?)
        """,
        (fill_id, account_id, fill_id, symbol, side, quantity, price, filled_at, filled_at)
    )


def _insert_match(conn, match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
                   buy_price, sell_price, matched_at, realized_pnl, strategy_id):
    conn.execute(
        """
        INSERT INTO fifo_matches (match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
                                   buy_price, sell_price, matched_at, realized_pnl, created_at, strategy_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity, buy_price, sell_price,
         matched_at, realized_pnl, matched_at, strategy_id)
    )


def test_payoff_ratio_from_avg_profit_over_avg_loss(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1000, 100, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1000, 104, "2026-06-02T09:00:00")
    _insert_fill(conn, "b2", "acc-x", "2330", "BUY", 1000, 100, "2026-06-01T09:00:00")
    _insert_fill(conn, "s2", "acc-x", "2330", "SELL", 1000, 98, "2026-06-02T09:00:00")
    _insert_match(conn, "m1", "acc-x", "2330", "b1", "s1", 1000, 100, 104, "2026-06-02T09:00:00", 4000, "")
    _insert_match(conn, "m2", "acc-x", "2330", "b2", "s2", 1000, 100, 98, "2026-06-02T09:00:00", -2000, "")
    conn.commit()

    stats = runner._calculate_statistics("acc-x", 100000, [{"equity": 100000}])
    assert stats["payoff_ratio"] == pytest.approx(2.0)


def test_payoff_ratio_inf_when_no_losses(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1000, 100, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1000, 104, "2026-06-02T09:00:00")
    _insert_match(conn, "m1", "acc-x", "2330", "b1", "s1", 1000, 100, 104, "2026-06-02T09:00:00", 4000, "")
    conn.commit()

    stats = runner._calculate_statistics("acc-x", 100000, [{"equity": 100000}])
    assert stats["payoff_ratio"] == float("inf")


def test_expectancy_r_skips_trades_without_exit_params(runner):
    conn = runner.db_conn
    runner.exit_definitions = {
        "stratA": StrategyDefinition(
            strategy_id="stratA", strategy_version="1.0.0", params=None,
            exit_params=ExitParams(
                fixed_stop_loss_bps=200, trailing_stop_bps=300, time_stop_days=10, time_stop_min_return_bps=0
            ), params_hash="h", order_budget_twd=0
        )
    }
    # stratA：買 100 元、賣 102 元（1 股）→ 毛損益 2 元；停損 2% → 每股風險 20,000（×10000 尺度）
    # → R = (2×10000/1)/20,000 = 1.0（A3 後 expectancy 吃每股淨損益，fixture pnl 須與量價自洽）
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1, 1000000, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1, 1020000, "2026-06-05T09:00:00")
    _insert_match(conn, "m1", "acc-x", "2330", "b1", "s1", 1, 1000000, 1020000, "2026-06-05T09:00:00", 2, "stratA")
    # stratB：沒有對應 exit_definitions → 無可比 R 基準，排除
    _insert_fill(conn, "b2", "acc-x", "2317", "BUY", 1, 500000, "2026-06-01T09:00:00")
    _insert_fill(conn, "s2", "acc-x", "2317", "SELL", 1, 510000, "2026-06-03T09:00:00")
    _insert_match(conn, "m2", "acc-x", "2317", "b2", "s2", 1, 500000, 510000, "2026-06-03T09:00:00", 1, "stratB")
    conn.commit()

    stats = runner._calculate_statistics("acc-x", 100000, [{"equity": 100000}])
    assert stats["expectancy_r"] == pytest.approx(1.0)
    assert stats["expectancy_r_sample_size"] == 1


def test_expectancy_r_none_when_no_trade_has_defined_r(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1, 1000000, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1, 1020000, "2026-06-05T09:00:00")
    _insert_match(conn, "m1", "acc-x", "2330", "b1", "s1", 1, 1000000, 1020000, "2026-06-05T09:00:00", 20000, "")
    conn.commit()

    stats = runner._calculate_statistics("acc-x", 100000, [{"equity": 100000}])
    assert stats["expectancy_r"] is None
    assert stats["expectancy_r_sample_size"] == 0


def test_avg_holding_period_days_from_buy_fill_to_match_timestamp(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1, 100, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1, 104, "2026-06-05T09:00:00")  # 4 天
    _insert_match(conn, "m1", "acc-x", "2330", "b1", "s1", 1, 100, 104, "2026-06-05T09:00:00", 4, "")
    _insert_fill(conn, "b2", "acc-x", "2317", "BUY", 1, 50, "2026-06-01T09:00:00")
    _insert_fill(conn, "s2", "acc-x", "2317", "SELL", 1, 52, "2026-06-07T09:00:00")  # 6 天
    _insert_match(conn, "m2", "acc-x", "2317", "b2", "s2", 1, 50, 52, "2026-06-07T09:00:00", 2, "")
    conn.commit()

    stats = runner._calculate_statistics("acc-x", 100000, [{"equity": 100000}])
    assert stats["avg_holding_period_days"] == pytest.approx(5.0)


def test_turnover_rate_total_traded_value_over_average_equity(runner):
    conn = runner.db_conn
    # 總成交金額（買+賣）＝ (1*1,000,000 + 1*1,020,000) // 10000 = 202（元，÷10000 換算單位）
    _insert_fill(conn, "b1", "acc-x", "2330", "BUY", 1, 1000000, "2026-06-01T09:00:00")
    _insert_fill(conn, "s1", "acc-x", "2330", "SELL", 1, 1020000, "2026-06-05T09:00:00")
    conn.commit()

    equity_curve = [{"equity": 100}, {"equity": 100}]  # average_equity = 100
    stats = runner._calculate_statistics("acc-x", 100, equity_curve)

    expected_total_value = (1 * 1000000 // 10000) + (1 * 1020000 // 10000)
    assert stats["turnover_rate"] == pytest.approx(expected_total_value / 100)
