"""FIFO 策略隔離、per-strategy reconcile 與 watermark rebuild 存活測試。"""
import pytest
from datetime import date
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection


@pytest.fixture
def db_conn(tmp_path):
    db_file = tmp_path / "test_multi.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    yield conn
    conn.close()


def _fill(fill_id, symbol, side, qty, price, strategy_id, account="acc-1", filled_at="2026-06-11T09:00:00+08:00"):
    return {
        "fill_id": fill_id,
        "account_id": account,
        "run_id": "run-1",
        "order_id": f"ord-{fill_id}",
        "execution_key": f"key-{fill_id}",
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "price": price,
        "filled_at": filled_at,
        "strategy_id": strategy_id
    }


def test_fifo_isolation_between_strategies(db_conn):
    """策略 A 賣出時，絕不可扣到策略 B 較早買入的同標的 lot。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")

    # B 先買（acquired earlier），A 後買 — 舊版全帳戶 FIFO 會先扣到 B 的 lot
    projection.apply_fill_transaction(
        _fill("f-b-buy", "2330", "BUY", 200, 900000, "pullback_rebound", filled_at="2026-06-10T09:00:00+08:00"))
    projection.apply_fill_transaction(
        _fill("f-a-buy", "2330", "BUY", 100, 1000000, "trend_breakout", filled_at="2026-06-11T09:00:00+08:00"))

    # A 全數出場
    projection.apply_fill_transaction(
        _fill("f-a-sell", "2330", "SELL", 100, 1100000, "trend_breakout", filled_at="2026-06-12T09:00:00+08:00"))

    cursor = db_conn.cursor()
    cursor.execute("SELECT strategy_id, SUM(quantity) as qty FROM position_lots WHERE symbol='2330' GROUP BY strategy_id")
    remaining = {r["strategy_id"]: r["qty"] for r in cursor.fetchall()}
    assert remaining == {"pullback_rebound": 200}  # B 的 lot 完整保留

    # 損益歸因到 A
    cursor.execute("SELECT strategy_id, realized_amount FROM realized_pnl")
    pnl = cursor.fetchall()
    assert len(pnl) == 1
    assert pnl[0]["strategy_id"] == "trend_breakout"
    assert pnl[0]["realized_amount"] == 100 * 10  # (110-100) x 100 股

    # fifo_matches 也帶策略
    cursor.execute("SELECT strategy_id FROM fifo_matches")
    assert all(r["strategy_id"] == "trend_breakout" for r in cursor.fetchall())

    assert projection.reconcile("acc-1")["status"] == "RECONCILE_OK"


def test_sell_more_than_strategy_bucket_fails(db_conn):
    """同標的他策略有貨，但本策略 bucket 不足時必須擋下（不可越界扣帳）。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")

    projection.apply_fill_transaction(_fill("f-b-buy", "2330", "BUY", 200, 900000, "pullback_rebound"))

    with pytest.raises(ValueError, match="SELL_WITHOUT_POSITION"):
        projection.apply_fill_transaction(_fill("f-a-sell", "2330", "SELL", 100, 1000000, "trend_breakout"))


def test_get_strategy_positions_weighted_avg(db_conn):
    """加權均價：100股@100 + 300股@120 → wavg = 115（非未加權的 110）。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")

    projection.apply_fill_transaction(_fill("f1", "2330", "BUY", 100, 1000000, "trend_breakout"))
    projection.apply_fill_transaction(_fill("f2", "2330", "BUY", 300, 1200000, "trend_breakout"))

    positions = projection.get_strategy_positions("acc-1")
    pos = positions[("trend_breakout", "2330")]
    assert pos["quantity"] == 400
    assert pos["wavg_price"] == 1150000


def test_watermark_survives_rebuild(db_conn):
    """watermark 是事實表，rebuild_from_ledger 不得清除（§2.2 v3 核心修正）。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")

    projection.apply_fill_transaction(_fill("f1", "2330", "BUY", 100, 1000000, "trend_breakout"))
    projection.upsert_high_watermark("acc-1", "trend_breakout", "2330", "2026-06-11", 1200000)

    projection.rebuild_from_ledger("acc-1")

    high = projection.get_position_high("acc-1", "trend_breakout", "2330", "2026-06-01")
    assert high == 1200000
    # rebuild 後 lots 也帶回 strategy_id
    positions = projection.get_strategy_positions("acc-1")
    assert ("trend_breakout", "2330") in positions


def test_watermark_window_resets_on_reentry(db_conn):
    """出清後重建倉，watermark 視窗以新 lot 取得日起算，舊高點不得沿用。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger("acc-1")

    # 舊倉位期間的高點
    projection.upsert_high_watermark("acc-1", "trend_breakout", "2330", "2026-06-02", 1500000)
    # 新倉位 6/10 起
    projection.upsert_high_watermark("acc-1", "trend_breakout", "2330", "2026-06-10", 1010000)

    high = projection.get_position_high("acc-1", "trend_breakout", "2330", "2026-06-10")
    assert high == 1010000  # 舊高點 150 不在視窗內
