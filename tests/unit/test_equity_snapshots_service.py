"""equity_snapshots service 直測：邊界日/平倉歸零/跨日累加/upsert 冪等/回填冪等，
以及對既有已受信任路徑（cash_balances / build_capital_overview）的交叉驗證不變式。

單元測試只能證明「邏輯符合我理解的邏輯」，測不出「我理解的邏輯本身錯了」——
交叉驗證測試才是抓重播邏輯設計性錯誤的關鍵（見 plan breezy-coalescing-sprout.md）。
"""
from datetime import date, timedelta

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.market_data.repository import SqliteMarketBarRepository
from src.contracts.models import MarketBar
from src.application.services import dashboard as dash
from src.application.services.equity_snapshots import (
    compute_equity_snapshot, save_equity_snapshot, backfill_equity_snapshots, read_equity_curve,
)


def _conn(tmp_path, name="eq.db"):
    db = tmp_path / name
    init_db(str(db))
    return get_db_connection(str(db))


def _fill(conn, account_id, symbol, side, quantity, price, filled_at, strategy_id="s1"):
    fill_id = f"fill-{account_id}-{symbol}-{filled_at}"
    conn.execute(
        "INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side, "
        "quantity, price, filled_at, reverses_fill_id, created_at, is_long_term, source, strategy_id) "
        "VALUES (?, ?, 'r1', 'o1', ?, ?, ?, ?, ?, ?, NULL, datetime('now'), 0, 'STRATEGY', ?)",
        (fill_id, account_id, fill_id, symbol, side, quantity, price, filled_at, strategy_id),
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


def _daily_run(conn, account_id, run_date, status, strategy_id="MULTI"):
    conn.execute(
        "INSERT INTO daily_runs (run_id, run_date, account_id, strategy_id, status, "
        "market_sync_status, execution_status, signal_generation_status, report_status, "
        "started_at, completed_at, last_error_code) "
        "VALUES (?, ?, ?, ?, ?, 'COMPLETED', 'COMPLETED', 'COMPLETED', 'COMPLETED', "
        "datetime('now'), datetime('now'), NULL)",
        (f"run-{account_id}-{run_date}", run_date, account_id, strategy_id, status),
    )
    conn.commit()


def test_fill_on_as_of_date_included_day_before_excluded(tmp_path):
    conn = _conn(tmp_path)
    _fill(conn, "a", "2330", "BUY", 100, 1000000, "2026-06-20T09:00:00+08:00")
    _bar(conn, "2330", "2026-06-20", 1000000)
    market_repo = SqliteMarketBarRepository(conn)

    snap_on = compute_equity_snapshot(conn, market_repo, "a", date(2026, 6, 20))
    snap_before = compute_equity_snapshot(conn, market_repo, "a", date(2026, 6, 19))

    assert snap_on["positions_value"] == 10000         # int(100 * 1000000 // 10000)
    assert snap_before["positions_value"] == 0
    conn.close()


def test_closed_position_excluded_from_value(tmp_path):
    conn = _conn(tmp_path)
    _fill(conn, "a", "2330", "BUY", 100, 1000000, "2026-06-10T09:00:00+08:00")
    _fill(conn, "a", "2330", "SELL", 100, 1100000, "2026-06-12T09:00:00+08:00")
    _bar(conn, "2330", "2026-06-15", 1200000)
    market_repo = SqliteMarketBarRepository(conn)

    snap = compute_equity_snapshot(conn, market_repo, "a", date(2026, 6, 15))

    assert snap["positions_value"] == 0
    conn.close()


def test_cross_day_position_accumulates(tmp_path):
    conn = _conn(tmp_path)
    _fill(conn, "a", "2330", "BUY", 50, 1000000, "2026-06-10T09:00:00+08:00")
    _fill(conn, "a", "2330", "BUY", 50, 1000000, "2026-06-11T09:00:00+08:00")
    _bar(conn, "2330", "2026-06-11", 1000000)
    market_repo = SqliteMarketBarRepository(conn)

    snap = compute_equity_snapshot(conn, market_repo, "a", date(2026, 6, 11))

    assert snap["positions_value"] == int(100 * 1000000 // 10000)
    conn.close()


def test_date_before_any_activity_is_zero(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency, "
        "source_type, source_id, occurred_at, idempotency_key, created_at) "
        "VALUES ('ld1', 'a', 'r1', 'INITIAL_DEPOSIT', 500000, 'TWD', 'DEPOSIT', 'src', "
        "'2026-06-15T00:00:00+08:00', 'idem', '2026-06-15')"
    )
    _fill(conn, "a", "2330", "BUY", 100, 1000000, "2026-06-20T09:00:00+08:00")
    conn.commit()
    market_repo = SqliteMarketBarRepository(conn)

    snap = compute_equity_snapshot(conn, market_repo, "a", date(2026, 6, 10))

    assert snap == {"cash": 0, "positions_value": 0, "total_equity": 0}
    conn.close()


def test_save_equity_snapshot_upsert(tmp_path):
    conn = _conn(tmp_path)
    save_equity_snapshot(conn, "a", date(2026, 6, 20), {"cash": 1, "positions_value": 2, "total_equity": 3})
    save_equity_snapshot(conn, "a", date(2026, 6, 20), {"cash": 10, "positions_value": 20, "total_equity": 30})

    rows = conn.execute("SELECT * FROM equity_snapshots WHERE account_id = 'a'").fetchall()

    assert len(rows) == 1
    assert rows[0]["cash"] == 10 and rows[0]["positions_value"] == 20 and rows[0]["total_equity"] == 30
    conn.close()


def test_cross_validates_against_ledger_and_capital_overview(tmp_path):
    """交叉驗證不變式：ledger 重播出的 cash/市值，必須跟兩條完全獨立實作的既有路徑
    （PortfolioProjection 維護的 cash_balances、build_capital_overview 算的市值）逐分錢相等。
    """
    conn = _conn(tmp_path)
    account_id = "a"
    as_of = date(2026, 6, 12)

    PortfolioLedger(conn).deposit(account_id, "r1", 500000, "TWD", date(2026, 6, 10))
    projection = PortfolioProjection(conn)
    projection.rebuild_from_ledger(account_id)  # 初始化 cash_balances 起始值（比照 backtest.py 用法）
    projection.apply_fill_transaction({
        "fill_id": "f1", "account_id": account_id, "run_id": "r1",
        "order_id": "o1", "execution_key": "k1", "symbol": "2330",
        "side": "BUY", "quantity": 1000, "price": 1000000,
        "filled_at": "2026-06-12T09:00:00+08:00", "strategy_id": "s1",
    })
    _bar(conn, "2330", "2026-06-12", 1100000)
    market_repo = SqliteMarketBarRepository(conn)

    snap = compute_equity_snapshot(conn, market_repo, account_id, as_of)

    # 獨立來源 1：PortfolioProjection 維護的權威現金餘額
    assert snap["cash"] == projection.get_cash_balance(account_id)

    # 獨立來源 2：另一支獨立寫的市值計算（build_capital_overview）
    cap = dash.build_capital_overview(conn, projection, account_id, as_of, market_repo)
    assert snap["positions_value"] == cap["positions_value"]
    assert snap["total_equity"] == cap["total_equity"]
    conn.close()


def test_backfill_creates_snapshot_per_completed_run_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    account_id = "a"
    _daily_run(conn, account_id, "2026-06-10", "COMPLETED")
    _daily_run(conn, account_id, "2026-06-11", "COMPLETED")
    _daily_run(conn, account_id, "2026-06-12", "WAITING")  # 未完成日不該被回填
    _fill(conn, account_id, "2330", "BUY", 100, 1000000, "2026-06-10T09:00:00+08:00")
    _bar(conn, "2330", "2026-06-10", 1000000)
    _bar(conn, "2330", "2026-06-11", 1000000)
    market_repo = SqliteMarketBarRepository(conn)

    count1 = backfill_equity_snapshots(conn, market_repo, account_id)
    assert count1 == 2
    rows1 = [dict(r) for r in conn.execute(
        "SELECT snapshot_date, total_equity FROM equity_snapshots WHERE account_id = ? ORDER BY snapshot_date",
        (account_id,)
    ).fetchall()]
    assert [r["snapshot_date"] for r in rows1] == ["2026-06-10", "2026-06-11"]

    count2 = backfill_equity_snapshots(conn, market_repo, account_id)
    assert count2 == 2
    rows2 = [dict(r) for r in conn.execute(
        "SELECT snapshot_date, total_equity FROM equity_snapshots WHERE account_id = ? ORDER BY snapshot_date",
        (account_id,)
    ).fetchall()]
    assert rows1 == rows2
    conn.close()


def test_read_equity_curve_field_mapping_and_order(tmp_path):
    conn = _conn(tmp_path)
    save_equity_snapshot(conn, "a", date(2026, 6, 11), {"cash": 1, "positions_value": 2, "total_equity": 3})
    save_equity_snapshot(conn, "a", date(2026, 6, 10), {"cash": 10, "positions_value": 20, "total_equity": 30})

    rows = read_equity_curve(conn, "a")

    assert [r["date"] for r in rows] == ["2026-06-10", "2026-06-11"]
    assert rows[0] == {
        "date": "2026-06-10", "cash": 10, "position_value": 20,
        "equity": 30, "daily_pnl": None,
    }
    assert rows[1]["daily_pnl"] == -27
    conn.close()


def test_read_equity_curve_adjusts_external_flows_but_keeps_dividend_as_profit(tmp_path):
    conn = _conn(tmp_path)
    snapshots = [
        ("2026-06-10", 100_000),
        ("2026-06-11", 112_000),
        ("2026-06-12", 106_000),
        ("2026-06-13", 111_000),
    ]
    for snapshot_date, equity in snapshots:
        save_equity_snapshot(
            conn, "a", date.fromisoformat(snapshot_date),
            {"cash": equity, "positions_value": 0, "total_equity": equity},
        )
    conn.executemany(
        "INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency, "
        "source_type, source_id, occurred_at, idempotency_key, created_at) "
        "VALUES (?, 'a', 'r1', ?, ?, 'TWD', ?, ?, ?, ?, ?)",
        [
            ("flow-deposit", "INITIAL_DEPOSIT", 10_000, "SYSTEM", "deposit", "2026-06-11T08:00:00+08:00", "idem-deposit", "2026-06-11"),
            ("flow-withdraw", "CASH_ADJUSTMENT", -5_000, "MANUAL", "withdraw", "2026-06-12T08:00:00+08:00", "idem-withdraw", "2026-06-12"),
            ("flow-dividend", "DIVIDEND", 3_000, "CORPORATE_ACTION", "dividend", "2026-06-13T08:00:00+08:00", "idem-dividend", "2026-06-13"),
        ],
    )
    conn.commit()

    rows = read_equity_curve(conn, "a")

    assert rows[0]["daily_pnl"] is None
    assert rows[1]["daily_pnl"] == 2_000
    assert rows[2]["daily_pnl"] == -1_000
    assert rows[3]["daily_pnl"] == 5_000
    conn.close()


def test_read_equity_curve_returns_all_available_snapshots(tmp_path):
    conn = _conn(tmp_path)
    start = date(2025, 1, 1)
    conn.executemany(
        "INSERT INTO equity_snapshots "
        "(account_id, snapshot_date, cash, positions_value, total_equity, created_at) "
        "VALUES ('a', ?, ?, 0, ?, datetime('now'))",
        [
            ((start + timedelta(days=i)).isoformat(), 100_000 + i, 100_000 + i)
            for i in range(181)
        ],
    )
    conn.commit()

    rows = read_equity_curve(conn, "a")

    assert len(rows) == 181
    assert rows[0]["date"] == "2025-01-01"
    assert rows[-1]["date"] == "2025-06-30"
    conn.close()
