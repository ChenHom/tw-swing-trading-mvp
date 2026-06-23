"""P1-T6：成本占已實現損益比例 + 分年表。
用合成 fifo_matches/cash_ledger + equity_curve（含 date）直接寫入 db 斷言。"""
from datetime import date

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import BacktestRunner


@pytest.fixture
def runner(tmp_path):
    db_file = tmp_path / "test_cost_yearly.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    yield r
    conn.close()


def _insert_match(conn, match_id, account_id, matched_at, realized_pnl):
    conn.execute(
        """
        INSERT INTO fifo_matches (match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
                                   buy_price, sell_price, matched_at, realized_pnl, created_at, strategy_id)
        VALUES (?, ?, '2330', 'b', 's', 1, 100, 100, ?, ?, ?, '')
        """,
        (match_id, account_id, matched_at, realized_pnl, matched_at)
    )


def _insert_fee(conn, account_id, ledger_id, event_type, amount):
    conn.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency,
                                  source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES (?, ?, 'run-x', ?, ?, 'TWD', 'FILL', 'fill-x', '2026-06-05T09:00:00', ?, datetime('now'))
        """,
        (ledger_id, account_id, event_type, amount, ledger_id)
    )


def test_cost_to_gross_pnl_ratio_divides_fee_tax_by_gross_realized_pnl(runner):
    conn = runner.db_conn
    _insert_match(conn, "m1", "acc-x", "2026-06-05T09:00:00", 10000)
    _insert_fee(conn, "acc-x", "led-fee", "BROKER_FEE", -1000)
    _insert_fee(conn, "acc-x", "led-tax", "TRANSACTION_TAX", -2000)
    conn.commit()

    cost_ratio = runner._calculate_cost_ratio("acc-x")
    assert cost_ratio["gross_realized_pnl"] == 10000
    assert cost_ratio["total_fee_tax_paid"] == 3000
    assert cost_ratio["cost_to_gross_pnl_ratio"] == pytest.approx(0.3)


def test_cost_to_gross_pnl_ratio_none_when_gross_pnl_not_positive(runner):
    conn = runner.db_conn
    _insert_match(conn, "m1", "acc-x", "2026-06-05T09:00:00", -5000)
    conn.commit()

    cost_ratio = runner._calculate_cost_ratio("acc-x")
    assert cost_ratio["cost_to_gross_pnl_ratio"] is None


def test_yearly_breakdown_splits_return_and_trades_per_calendar_year(runner):
    conn = runner.db_conn
    _insert_match(conn, "m1", "acc-x", "2025-03-01T09:00:00", 1000)
    _insert_match(conn, "m2", "acc-x", "2026-04-01T09:00:00", -500)
    conn.commit()

    equity_curve = [
        {"date": date(2025, 1, 2), "equity": 100000},
        {"date": date(2025, 12, 30), "equity": 110000},  # 2025 報酬 +10%
        {"date": date(2026, 1, 2), "equity": 110000},
        {"date": date(2026, 12, 30), "equity": 99000},  # 2026 接續 2025 末值，報酬 -10%
    ]
    breakdown = runner._calculate_yearly_breakdown("acc-x", equity_curve)

    assert [b["year"] for b in breakdown] == [2025, 2026]
    assert breakdown[0]["year_return"] == pytest.approx(0.1)
    assert breakdown[0]["realized_pnl"] == 1000
    assert breakdown[0]["trade_count"] == 1
    assert breakdown[0]["win_rate"] == pytest.approx(1.0)

    assert breakdown[1]["start_equity"] == 110000  # 接續上一年 end_equity，非該年自身首點
    assert breakdown[1]["year_return"] == pytest.approx(-0.1)
    assert breakdown[1]["realized_pnl"] == -500
    assert breakdown[1]["win_rate"] == pytest.approx(0.0)


def test_yearly_breakdown_empty_when_no_equity_curve(runner):
    assert runner._calculate_yearly_breakdown("acc-x", []) == []
