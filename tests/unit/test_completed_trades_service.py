from datetime import date

import pytest

from src.portfolio.db import get_db_connection, init_db
from src.application.services.completed_trades import (
    build_completed_trade_history,
    list_close_dates,
    read_completed_trades,
)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "completed-trades.db"
    init_db(str(path))
    connection = get_db_connection(str(path))
    yield connection
    connection.close()


def _fill(conn, fill_id, account, symbol, side, quantity, price, filled_at, strategy="s1"):
    conn.execute(
        """
        INSERT INTO fills (
            fill_id, account_id, run_id, order_id, execution_key, symbol, side,
            quantity, price, filled_at, reverses_fill_id, created_at,
            is_long_term, source, strategy_id
        ) VALUES (?, ?, 'run-test', ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 'STRATEGY', ?)
        """,
        (
            fill_id,
            account,
            f"order-{fill_id}",
            f"key-{fill_id}",
            symbol,
            side,
            quantity,
            price,
            filled_at,
            filled_at,
            strategy,
        ),
    )
    conn.commit()


def _match(
    conn,
    match_id,
    account,
    symbol,
    buy_id,
    sell_id,
    quantity,
    buy_price,
    sell_price,
    gross,
    net,
    strategy="s1",
    matched_at="2026-06-10T09:00:00+08:00",
):
    conn.execute(
        """
        INSERT INTO fifo_matches (
            match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
            buy_price, sell_price, matched_at, realized_pnl, created_at,
            strategy_id, net_realized_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            account,
            symbol,
            buy_id,
            sell_id,
            quantity,
            buy_price,
            sell_price,
            matched_at,
            gross,
            matched_at,
            strategy,
            net,
        ),
    )
    conn.commit()


def test_one_sell_consuming_two_buy_lots_is_one_trade(conn):
    _fill(conn, "buy-1", "a", "2330", "BUY", 60, 1000000, "2026-06-01T09:00:00+08:00")
    _fill(conn, "buy-2", "a", "2330", "BUY", 40, 1100000, "2026-06-03T09:00:00+08:00")
    _fill(conn, "sell-1", "a", "2330", "SELL", 100, 1200000, "2026-06-10T09:00:00+08:00")
    _match(conn, "m1", "a", "2330", "buy-1", "sell-1", 60, 1000000, 1200000, 1200, 1100)
    _match(conn, "m2", "a", "2330", "buy-2", "sell-1", 40, 1100000, 1200000, 400, 350)

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert len(trades) == 1
    assert trades[0]["sell_fill_id"] == "sell-1"
    assert trades[0]["quantity"] == 100
    assert trades[0]["weighted_buy_price"] == 104.0
    assert trades[0]["gross_pnl"] == 1600
    assert trades[0]["net_pnl"] == 1450
    assert trades[0]["cost_twd"] == 150
    assert len(trades[0]["lots"]) == 2


def _single_lot_trade(
    conn,
    suffix,
    account="a",
    strategy="s1",
    symbol="2330",
    buy_date="2026-06-01",
    sell_date="2026-06-10",
    quantity=10,
    gross=100,
    net=80,
):
    buy_id = f"buy-{suffix}"
    sell_id = f"sell-{suffix}"
    _fill(conn, buy_id, account, symbol, "BUY", quantity, 1000000, f"{buy_date}T09:00:00+08:00", strategy)
    _fill(conn, sell_id, account, symbol, "SELL", quantity, 1100000, f"{sell_date}T09:00:00+08:00", strategy)
    _match(
        conn,
        f"match-{suffix}",
        account,
        symbol,
        buy_id,
        sell_id,
        quantity,
        1000000,
        1100000,
        gross,
        net,
        strategy,
        f"{sell_date}T09:00:00+08:00",
    )


def test_close_dates_only_include_fifo_match_dates_for_account(conn):
    _single_lot_trade(conn, "old", sell_date="2026-06-10")
    _single_lot_trade(conn, "new", sell_date="2026-06-20", symbol="2317")
    _single_lot_trade(conn, "other", account="b", sell_date="2026-06-30", symbol="2454")
    _fill(conn, "open-buy", "a", "2886", "BUY", 10, 1000000, "2026-07-01T09:00:00+08:00")

    assert list_close_dates(conn, "a") == ["2026-06-20", "2026-06-10"]


def test_history_defaults_latest_and_navigates_only_close_dates(conn):
    _single_lot_trade(conn, "old", sell_date="2026-06-10")
    _single_lot_trade(conn, "mid", sell_date="2026-06-12", symbol="2317")
    _single_lot_trade(conn, "new", sell_date="2026-06-20", symbol="2454")

    latest = build_completed_trade_history(conn, "a")
    middle = build_completed_trade_history(conn, "a", date(2026, 6, 12))

    assert latest["selected_date"] == "2026-06-20"
    assert latest["newer_date"] is None
    assert latest["older_date"] == "2026-06-12"
    assert middle["newer_date"] == "2026-06-20"
    assert middle["older_date"] == "2026-06-10"


def test_unknown_match_net_makes_sell_and_day_net_unknown(conn):
    _fill(conn, "buy-1", "a", "2330", "BUY", 60, 1000000, "2026-06-01T09:00:00+08:00")
    _fill(conn, "buy-2", "a", "2330", "BUY", 40, 1100000, "2026-06-03T09:00:00+08:00")
    _fill(conn, "sell-1", "a", "2330", "SELL", 100, 1200000, "2026-06-10T09:00:00+08:00")
    _match(conn, "m1", "a", "2330", "buy-1", "sell-1", 60, 1000000, 1200000, 200, 100)
    _match(conn, "m2", "a", "2330", "buy-2", "sell-1", 40, 1100000, 1200000, 100, None)

    history = build_completed_trade_history(conn, "a", date(2026, 6, 10))
    trade = history["trades"][0]

    assert trade["gross_pnl"] == 300
    assert trade["net_pnl"] is None
    assert trade["cost_twd"] is None
    assert trade["return_pct"] is None
    assert history["summary"] == {
        "count": 1,
        "gross_pnl": 300,
        "cost_twd": None,
        "net_pnl": None,
        "has_unknown_net": True,
    }


def test_valid_date_without_close_keeps_date_and_returns_empty(conn):
    _single_lot_trade(conn, "one", sell_date="2026-06-10")

    history = build_completed_trade_history(conn, "a", date(2026, 6, 11))

    assert history["selected_date"] == "2026-06-11"
    assert history["trades"] == []
    assert history["newer_date"] is None
    assert history["older_date"] is None


def test_corrupt_cross_account_fill_references_are_not_displayed(conn):
    _fill(conn, "buy-b", "b", "2330", "BUY", 10, 1000000, "2026-06-01T09:00:00+08:00")
    _fill(conn, "sell-b", "b", "2330", "SELL", 10, 1100000, "2026-06-10T09:00:00+08:00")
    _match(conn, "bad-account-match", "a", "2330", "buy-b", "sell-b", 10, 1000000, 1100000, 100, 80)

    assert read_completed_trades(conn, "a", date(2026, 6, 10)) == []
    assert list_close_dates(conn, "a") == []


def test_fifo_row_not_joined_to_buy_and_sell_sides_is_not_displayed(conn):
    _fill(conn, "wrong-buy", "a", "2330", "SELL", 10, 1000000, "2026-06-01T09:00:00+08:00")
    _fill(conn, "wrong-sell", "a", "2330", "BUY", 10, 1100000, "2026-06-10T09:00:00+08:00")
    _match(conn, "bad-side-match", "a", "2330", "wrong-buy", "wrong-sell", 10, 1000000, 1100000, 100, 80)

    assert read_completed_trades(conn, "a", date(2026, 6, 10)) == []
    assert list_close_dates(conn, "a") == []


def test_two_sells_same_symbol_strategy_and_date_remain_two_rows(conn):
    _single_lot_trade(conn, "first", quantity=40)
    _single_lot_trade(conn, "second", quantity=60)

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert [trade["sell_fill_id"] for trade in trades] == ["sell-first", "sell-second"]


def test_partial_sell_reports_only_matched_quantity(conn):
    _fill(conn, "buy-partial", "a", "2330", "BUY", 100, 1000000, "2026-06-01T09:00:00+08:00")
    _fill(conn, "sell-partial", "a", "2330", "SELL", 40, 1100000, "2026-06-10T09:00:00+08:00")
    _match(conn, "match-partial", "a", "2330", "buy-partial", "sell-partial", 40, 1000000, 1100000, 400, 300)

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert len(trades) == 1
    assert trades[0]["quantity"] == 40


def test_holding_days_use_earliest_matched_buy_calendar_date(conn):
    _fill(conn, "buy-fri", "a", "2330", "BUY", 5, 1000000, "2026-06-05T09:00:00+08:00")
    _fill(conn, "buy-mon", "a", "2330", "BUY", 5, 1000000, "2026-06-08T09:00:00+08:00")
    _fill(conn, "sell-mon", "a", "2330", "SELL", 10, 1100000, "2026-06-08T15:00:00+08:00")
    _match(conn, "match-fri", "a", "2330", "buy-fri", "sell-mon", 5, 1000000, 1100000, 50, 40, matched_at="2026-06-08T15:00:00+08:00")
    _match(conn, "match-mon", "a", "2330", "buy-mon", "sell-mon", 5, 1000000, 1100000, 50, 40, matched_at="2026-06-08T15:00:00+08:00")

    trade = read_completed_trades(conn, "a", date(2026, 6, 8))[0]

    assert trade["holding_days"] == 3
    assert [lot["holding_days"] for lot in trade["lots"]] == [3, 0]


def test_same_symbol_and_close_date_stays_separate_by_strategy(conn):
    _single_lot_trade(conn, "breakout", strategy="trend_breakout")
    _single_lot_trade(conn, "rider", strategy="trend_rider")

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert [(trade["sell_fill_id"], trade["strategy_id"]) for trade in trades] == [
        ("sell-breakout", "trend_breakout"),
        ("sell-rider", "trend_rider"),
    ]


def test_close_date_includes_start_and_end_of_day_without_next_day(conn):
    _single_lot_trade(conn, "start", sell_date="2026-06-10", symbol="2317")
    _single_lot_trade(conn, "end", sell_date="2026-06-10", symbol="2330")
    _single_lot_trade(conn, "next", sell_date="2026-06-11", symbol="2454")
    conn.execute(
        "UPDATE fifo_matches SET matched_at = ? WHERE match_id = ?",
        ("2026-06-10T00:00:00+08:00", "match-start"),
    )
    conn.execute(
        "UPDATE fifo_matches SET matched_at = ? WHERE match_id = ?",
        ("2026-06-10T23:59:59+08:00", "match-end"),
    )
    conn.commit()

    trades = read_completed_trades(conn, "a", date(2026, 6, 10))

    assert {trade["sell_fill_id"] for trade in trades} == {"sell-start", "sell-end"}


def test_completed_trade_queries_do_not_write(conn):
    _single_lot_trade(conn, "readonly")
    changes_before = conn.total_changes

    build_completed_trade_history(conn, "a", date(2026, 6, 10))

    assert conn.total_changes == changes_before
