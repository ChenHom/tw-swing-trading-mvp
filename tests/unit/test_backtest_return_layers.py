"""P1-T4：報酬分層（①訊號 ②③策略/全系統組合 ④modeled_executable_return）。
用合成 fifo_matches + cash_ledger 列直接寫入 db 斷言（沿用 __new__ 隔離測試手法）。"""
import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import BacktestRunner


@pytest.fixture
def runner(tmp_path):
    db_file = tmp_path / "test_return_layers.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    yield r
    conn.close()


def _insert_match(conn, match_id, account_id, buy_price, sell_price):
    conn.execute(
        """
        INSERT INTO fifo_matches (match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
                                   buy_price, sell_price, matched_at, realized_pnl, created_at, strategy_id)
        VALUES (?, ?, '2330', 'b', 's', 1, ?, ?, '2026-06-05T09:00:00', 0, '2026-06-05T09:00:00', '')
        """,
        (match_id, account_id, buy_price, sell_price)
    )


def _insert_fee(conn, account_id, event_type, amount):
    conn.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency,
                                  source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES (?, ?, 'run-x', ?, ?, 'TWD', 'FILL', 'fill-x', '2026-06-05T09:00:00', ?, datetime('now'))
        """,
        (f"led-{event_type}-{amount}", account_id, event_type, amount, f"idem-{event_type}-{amount}")
    )


def test_raw_signal_return_is_unweighted_average_of_per_trade_returns(runner):
    conn = runner.db_conn
    _insert_match(conn, "m1", "acc-x", 1000000, 1100000)  # +10%
    _insert_match(conn, "m2", "acc-x", 1000000, 900000)   # -10%
    conn.commit()

    layers = runner._calculate_return_layers("acc-x", 100000, 100000)
    assert layers["raw_signal_return"] == pytest.approx(0.0)


def test_modeled_executable_return_is_final_over_initial_minus_one(runner):
    layers = runner._calculate_return_layers("acc-x", 100000, 110000)
    assert layers["modeled_executable_return"] == pytest.approx(0.1)


def test_strategy_and_full_system_layers_add_back_fee_and_tax_and_coincide(runner):
    conn = runner.db_conn
    _insert_fee(conn, "acc-x", "BROKER_FEE", -100)
    _insert_fee(conn, "acc-x", "TRANSACTION_TAX", -200)
    conn.commit()

    layers = runner._calculate_return_layers("acc-x", 100000, 109700)
    # final_equity 109700 + 補回 300 手續費/稅 = 110000 → 10%
    assert layers["strategy_portfolio_return"] == pytest.approx(0.1)
    assert layers["full_system_portfolio_return"] == layers["strategy_portfolio_return"]
    assert layers["modeled_executable_return"] == pytest.approx(0.097)
    assert layers["fee_tax_drag"] == pytest.approx(0.003)


def test_raw_signal_return_none_when_no_closed_trades(runner):
    layers = runner._calculate_return_layers("acc-x", 100000, 100000)
    assert layers["raw_signal_return"] is None
