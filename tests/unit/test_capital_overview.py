"""build_capital_overview service 直測：市值/報酬率/fallback/聚合/邊界。

直接種事實表（cash_ledger / cash_balances / position_lots / market_bars）以精確控制
數字；不走 fill 引擎副作用，讓斷言不依賴手續費/稅的浮動。
"""
from datetime import date

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.market_data.repository import SqliteMarketBarRepository
from src.contracts.models import MarketBar
from src.application.services import dashboard as dash


def _conn(tmp_path, name="cap.db"):
    db = tmp_path / name
    init_db(str(db))
    return get_db_connection(str(db))


def _deposit(conn, account_id, amount):
    conn.execute(
        "INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency, "
        "source_type, source_id, occurred_at, idempotency_key, created_at) "
        "VALUES (?, ?, 'r1', 'INITIAL_DEPOSIT', ?, 'TWD', 'DEPOSIT', ?, '2026-06-12', ?, '2026-06-12')",
        (f"ld-{account_id}", account_id, amount, f"src-{account_id}", f"idem-{account_id}"),
    )
    conn.commit()


def _set_cash(conn, account_id, balance):
    conn.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) "
        "VALUES (?, ?, 'TWD', '2026-06-12') "
        "ON CONFLICT(account_id) DO UPDATE SET balance=excluded.balance",
        (account_id, balance),
    )
    conn.commit()


def _lot(conn, account_id, symbol, quantity, price, strategy_id="trend_breakout", suffix=""):
    lot_id = f"lot-{account_id}-{symbol}-{strategy_id}{suffix}"
    conn.execute(
        "INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, "
        "fill_id, created_at, is_long_term, strategy_id) "
        "VALUES (?, ?, ?, ?, ?, '2026-06-10', ?, '2026-06-10', 0, ?)",
        (lot_id, account_id, symbol, quantity, price, f"fill-{lot_id}", strategy_id),
    )
    conn.commit()


def _bar(conn, symbol, trade_date, close):
    SqliteMarketBarRepository(conn).upsert(MarketBar(
        symbol=symbol, exchange="TWSE", instrument_type="STOCK",
        trade_date=date.fromisoformat(trade_date),
        open=close, high=close, low=close, close=close,
        volume=1000, amount=close, source="TEST",
        source_fetched_at="2026-06-12T00:00:00+08:00", raw_payload_checksum="x",
    ))


def _build(conn, account_id, view_date="2026-06-12"):
    return dash.build_capital_overview(
        conn, PortfolioProjection(conn), account_id,
        date.fromisoformat(view_date), SqliteMarketBarRepository(conn),
    )


def test_market_value_with_bar(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 200000)
    _lot(conn, "a", "2330", 1000, 1000000)        # 成本 100.00
    _bar(conn, "2330", "2026-06-12", 1100000)     # 現價 110.00
    cap = _build(conn, "a")
    assert cap["positions_value"] == 110000        # int(1000*1100000//10000)
    assert cap["total_equity"] == 200000 + 110000
    assert cap["any_stale"] is False
    row = next(r for r in cap["allocation"] if r["symbol"] == "2330")
    assert row["stale"] is False
    conn.close()


def test_fallback_when_no_bar(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 0)
    _lot(conn, "a", "2330", 1000, 1000000)        # 成本 100.00，不種當日 bar
    cap = _build(conn, "a")
    assert cap["positions_value"] == 100000        # fallback 用均價當現價
    assert cap["any_stale"] is True
    row = next(r for r in cap["allocation"] if r["symbol"] == "2330")
    assert row["stale"] is True
    conn.close()


def test_return_pct(tmp_path):
    conn = _conn(tmp_path)
    _deposit(conn, "a", 300000)
    _set_cash(conn, "a", 200000)
    _lot(conn, "a", "2330", 1000, 1000000)
    _bar(conn, "2330", "2026-06-12", 1100000)     # 市值 110000
    cap = _build(conn, "a")
    assert cap["net_principal"] == 300000
    assert cap["total_equity"] == 310000
    assert cap["total_return"] == 10000
    assert cap["return_pct"] == pytest.approx(10000 / 300000 * 100)
    conn.close()


def test_no_principal_returns_none(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 50000)                    # 有現金但無 INITIAL_DEPOSIT
    cap = _build(conn, "a")
    assert cap["net_principal"] == 0
    assert cap["return_pct"] is None
    conn.close()


def test_allocation_includes_cash_and_sums(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 90000)
    _lot(conn, "a", "2330", 1000, 1000000)
    _bar(conn, "2330", "2026-06-12", 1000000)     # 市值 100000
    cap = _build(conn, "a")
    assert cap["allocation"][0]["kind"] == "cash"
    assert cap["allocation"][0]["label"] == "現金"
    assert sum(r["value"] for r in cap["allocation"]) == cap["total_equity"]
    for r in cap["allocation"]:
        assert r["ratio"] == pytest.approx(r["value"] / cap["total_equity"])
    conn.close()


def test_only_cash(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 50000)
    cap = _build(conn, "a")
    assert len(cap["allocation"]) == 1
    assert cap["allocation"][0]["kind"] == "cash"
    assert cap["allocation"][0]["ratio"] == pytest.approx(1.0)
    conn.close()


def test_zero_cash_with_positions(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 0)
    _lot(conn, "a", "2330", 1000, 1000000)
    _bar(conn, "2330", "2026-06-12", 1000000)
    cap = _build(conn, "a")
    kinds = [r["kind"] for r in cap["allocation"]]
    assert "cash" not in kinds
    assert sum(r["value"] for r in cap["allocation"]) == cap["positions_value"] == cap["total_equity"]
    conn.close()


def test_no_assets_empty_allocation(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 0)
    cap = _build(conn, "a")
    assert cap["total_equity"] == 0
    assert cap["allocation"] == []
    conn.close()


def test_cross_strategy_same_symbol_aggregated(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 0)
    _lot(conn, "a", "2330", 1000, 1000000, strategy_id="trend_breakout", suffix="-1")
    _lot(conn, "a", "2330", 2000, 1000000, strategy_id="pullback_rebound", suffix="-2")
    _bar(conn, "2330", "2026-06-12", 1000000)     # 100.00
    cap = _build(conn, "a")
    rows = [r for r in cap["allocation"] if r["symbol"] == "2330"]
    assert len(rows) == 1                          # 跨策略聚合為一塊
    assert rows[0]["value"] == int(3000 * 1000000 // 10000)  # 3000 股 @ 100 = 300000
    conn.close()


def test_latest_bar_as_of_when_no_bar_on_view_date(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 200000)
    _lot(conn, "a", "2330", 1000, 1000000)        # 成本 100.00
    _bar(conn, "2330", "2026-06-11", 1100000)     # 前一天的現價 110.00
    # 我們不種 2026-06-12 (view_date) 的 bar
    cap = _build(conn, "a", "2026-06-12")
    # 市值應該要是 110000 (使用前一天 2026-06-11 的收盤價)
    assert cap["positions_value"] == 110000
    assert cap["total_equity"] == 200000 + 110000
    # 因為是用前一天的 bar，相較於 view_date，應該是 stale=True
    assert cap["any_stale"] is True
    row = next(r for r in cap["allocation"] if r["symbol"] == "2330")
    assert row["stale"] is True
    conn.close()


def test_dashboard_uses_point_in_time_latest_bar(tmp_path):
    conn = _conn(tmp_path)
    _set_cash(conn, "a", 200000)
    _lot(conn, "a", "2330", 1000, 1000000)        # 成本 100.00
    _bar(conn, "2330", "2026-06-11", 1100000)     # 前一天的現價 110.00
    # view_date 為 2026-06-12，無當日 bar
    db_data = dash.build_dashboard(
        conn, PortfolioProjection(conn), "a",
        date.fromisoformat("2026-06-12"), market_repo=SqliteMarketBarRepository(conn)
    )
    # positions 裡面的 2330 的 last_close 應該是 110.00
    pos_2330 = next(p for p in db_data["positions"] if p["symbol"] == "2330")
    assert pos_2330["last_close"] == 110.00
    conn.close()
