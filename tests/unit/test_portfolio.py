import pytest
from datetime import date
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection

@pytest.fixture
def db_conn(tmp_path):
    db_file = tmp_path / "test_portfolio.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    yield conn
    conn.close()

def test_portfolio_deposit_and_balance(db_conn):
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    
    # Initialize account with cash
    ledger.deposit("acc-1", "run-1", 300000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")
    
    # Check balance snapshot
    balance = projection.get_cash_balance("acc-1")
    assert balance == 300000

def test_portfolio_buy_and_sell_transaction_cycle(db_conn):
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    
    # 1. Deposit 300,000 cash
    ledger.deposit("acc-1", "run-1", 300000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-1")
    
    # 2. Record BUY fill: 1000 shares of 2330 at 100.0 (1,000,000 scaled) on 2026-06-11
    # Trade value = 100,000 TWD
    # Broker fee = max(20, int(round(100000 * 0.001425))) = 143 TWD
    # Total cash reduction = 100,143 TWD
    fill_buy = {
        "fill_id": "fill-buy-1",
        "account_id": "acc-1",
        "run_id": "run-1",
        "order_id": "order-1",
        "execution_key": "exec-key-1",
        "symbol": "2330",
        "side": "BUY",
        "quantity": 1000,
        "price": 1000000,  # 100.0 x 10000
        "filled_at": "2026-06-11T09:00:00+08:00",
        "strategy_id": "trend_pullback"
    }
    
    projection.apply_fill_transaction(fill_buy)
    
    # Verify position lot created
    lots = projection.get_position_lots("acc-1", "2330")
    assert len(lots) == 1
    assert lots[0]["quantity"] == 1000
    assert lots[0]["price"] == 1000000
    
    # Verify cash deducted: 300,000 - 100,142 = 199,858 TWD
    assert projection.get_cash_balance("acc-1") == 199858
    
    # 3. Record SELL fill: 1000 shares of 2330 at 105.0 (1,050,000 scaled) on 2026-06-12
    # Trade proceeds = 105,000 TWD
    # Broker fee = max(20, int(round(105000 * 0.001425))) = 150 TWD
    # Tax = int(round(105000 * 0.003)) = 315 TWD
    # Net cash addition = 105,000 - 150 - 315 = 104,535 TWD
    # Final cash balance = 199,858 + 104,535 = 304,393 TWD
    fill_sell = {
        "fill_id": "fill-sell-1",
        "account_id": "acc-1",
        "run_id": "run-1",
        "order_id": "order-2",
        "execution_key": "exec-key-2",
        "symbol": "2330",
        "side": "SELL",
        "quantity": 1000,
        "price": 1050000,  # 105.0 x 10000
        "filled_at": "2026-06-12T09:00:00+08:00",
        "strategy_id": "trend_pullback"
    }
    
    projection.apply_fill_transaction(fill_sell)
    
    # Verify lot is cleared
    lots_after = projection.get_position_lots("acc-1", "2330")
    assert len(lots_after) == 0
    
    # Verify cash balance
    assert projection.get_cash_balance("acc-1") == 304393
    
    # Verify realized PnL recorded
    pnl_records = projection.get_realized_pnl("acc-1")
    assert len(pnl_records) == 1
    assert pnl_records[0]["symbol"] == "2330"
    # Gross realized amount = 105,000 - 100,000 = 5000
    assert pnl_records[0]["realized_amount"] == 5000
    assert pnl_records[0]["tax_amount"] == 315
    assert pnl_records[0]["fee_amount"] == 150

def test_portfolio_rebuild_and_reconcile(db_conn):
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)
    
    # 1. Setup account and execute buy
    ledger.deposit("acc-2", "run-1", 200000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-2")
    
    fill_buy = {
        "fill_id": "fill-buy-2",
        "account_id": "acc-2",
        "run_id": "run-1",
        "order_id": "order-1",
        "execution_key": "exec-key-3",
        "symbol": "2330",
        "side": "BUY",
        "quantity": 100,
        "price": 1000000,
        "filled_at": "2026-06-11T09:00:00+08:00",
        "strategy_id": "trend_pullback"
    }
    projection.apply_fill_transaction(fill_buy)
    
    # Reconcile should be OK
    result = projection.reconcile("acc-2")
    assert result["status"] == "RECONCILE_OK"
    
    # 2. Corrupt cash balance to test reconcile failure
    db_conn.execute("UPDATE cash_balances SET balance = 5000 WHERE account_id = 'acc-2'")
    db_conn.commit()
    
    corrupt_result = projection.reconcile("acc-2")
    assert corrupt_result["status"] == "CASH_BALANCE_MISMATCH"
    
    # 3. Call rebuild and verify it's OK again
    projection.rebuild_from_ledger("acc-2")
    rebuilt_result = projection.reconcile("acc-2")
    assert rebuilt_result["status"] == "RECONCILE_OK"
    assert projection.get_cash_balance("acc-2") == 200000 - (10000 + 20) # 100 * 100 = 10,000 trade value + 20 min fee



def test_rebuild_same_day_buy_then_sell_order_is_stable(db_conn):
    """B8：同日同刻（filled_at 相同）先買後賣，rebuild 重放必須維持寫入順序
    （tiebreaker: created_at, rowid），不得先重放 SELL 而炸 SELL_WITHOUT_POSITION。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)

    ledger.deposit("acc-b8", "run-1", 200000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-b8")

    same_ts = "2026-06-11T09:00:00+08:00"
    projection.apply_fill_transaction({
        "fill_id": "b8-buy", "account_id": "acc-b8", "run_id": "run-1",
        "order_id": "o1", "execution_key": "b8-k1", "symbol": "2330",
        "side": "BUY", "quantity": 100, "price": 1000000,
        "filled_at": same_ts, "strategy_id": "MANUAL"
    })
    projection.apply_fill_transaction({
        "fill_id": "b8-sell", "account_id": "acc-b8", "run_id": "run-1",
        "order_id": "o2", "execution_key": "b8-k2", "symbol": "2330",
        "side": "SELL", "quantity": 100, "price": 1010000,
        "filled_at": same_ts, "strategy_id": "MANUAL"
    })

    projection.rebuild_from_ledger("acc-b8")  # 不得 raise
    assert projection.reconcile("acc-b8")["status"] == "RECONCILE_OK"
    matches = db_conn.execute(
        "SELECT buy_fill_id, sell_fill_id FROM fifo_matches WHERE account_id = 'acc-b8'"
    ).fetchall()
    assert len(matches) == 1
    assert (matches[0]["buy_fill_id"], matches[0]["sell_fill_id"]) == ("b8-buy", "b8-sell")


def test_duplicate_execution_key_apply_is_noop(db_conn):
    """B9：同 execution_key 二次 apply（如 run 崩潰重跑）→ 冪等 no-op，
    不得重複扣款/建 lot。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)

    ledger.deposit("acc-b9", "run-1", 200000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-b9")

    fill = {
        "fill_id": "b9-f1", "account_id": "acc-b9", "run_id": "run-1",
        "order_id": "o1", "execution_key": "b9-key", "symbol": "2330",
        "side": "BUY", "quantity": 100, "price": 1000000,
        "filled_at": "2026-06-11T09:00:00+08:00", "strategy_id": "trend_breakout"
    }
    projection.apply_fill_transaction(fill)
    cash_after_first = projection.get_cash_balance("acc-b9")

    retry = dict(fill, fill_id="b9-f1-retry", order_id="o1-retry")  # 重跑會拿到新 fill_id
    projection.apply_fill_transaction(retry)  # 不得 raise、不得重複入帳

    assert projection.get_cash_balance("acc-b9") == cash_after_first
    n_fills = db_conn.execute(
        "SELECT COUNT(*) FROM fills WHERE account_id = 'acc-b9'"
    ).fetchone()[0]
    n_lots = db_conn.execute(
        "SELECT COUNT(*) FROM position_lots WHERE account_id = 'acc-b9'"
    ).fetchone()[0]
    assert (n_fills, n_lots) == (1, 1)
    assert projection.reconcile("acc-b9")["status"] == "RECONCILE_OK"


def test_fifo_match_net_realized_pnl(db_conn):
    """A3：net_realized_pnl = 毛損益 −（買腳手續費+賣腳手續費+證交稅）的按量分攤。"""
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)

    ledger.deposit("acc-a3", "run-1", 500000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-a3")

    projection.apply_fill_transaction({
        "fill_id": "a3-buy", "account_id": "acc-a3", "run_id": "run-1",
        "order_id": "o1", "execution_key": "a3-k1", "symbol": "2330",
        "side": "BUY", "quantity": 1000, "price": 1000000,   # 100 元 × 1000 股
        "filled_at": "2026-06-11T09:00:00+08:00", "strategy_id": "trend_breakout"
    })
    projection.apply_fill_transaction({
        "fill_id": "a3-sell", "account_id": "acc-a3", "run_id": "run-1",
        "order_id": "o2", "execution_key": "a3-k2", "symbol": "2330",
        "side": "SELL", "quantity": 1000, "price": 1100000,  # 110 元 × 1000 股
        "filled_at": "2026-06-12T09:00:00+08:00", "strategy_id": "trend_breakout"
    })

    row = db_conn.execute(
        "SELECT realized_pnl, net_realized_pnl FROM fifo_matches WHERE account_id = 'acc-a3'"
    ).fetchone()
    # 毛 = (110-100)×1000 = 10000 元
    assert row["realized_pnl"] == 10000
    # 買費 max(20, round(100000×0.001425))=142（banker's rounding，與 ledger 實收一致）、
    # 賣費 max(20, round(110000×0.001425))=157、稅 round(110000×0.003)=330
    assert row["net_realized_pnl"] == 10000 - 142 - 157 - 330


def test_buy_fill_cannot_overdraw_cash(db_conn):
    """B5：engine 路徑（enforce_cash=True）BUY 實付超過現金 → raise 且整組 rollback
    （現金不為負、無殘留 lot/fill）；record-fill/rebuild 事實記錄路徑不受此限。"""
    import pytest as _pytest
    ledger = PortfolioLedger(db_conn)
    projection = PortfolioProjection(db_conn)

    ledger.deposit("acc-b5", "run-1", 10000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-b5")

    overdraw = {
        "fill_id": "b5-f1", "account_id": "acc-b5", "run_id": "run-1",
        "order_id": "o1", "execution_key": "b5-k1", "symbol": "2330",
        "side": "BUY", "quantity": 200, "price": 1000000,  # 需 20000+費 > 現金 10000
        "filled_at": "2026-06-11T09:00:00+08:00", "strategy_id": "trend_breakout"
    }
    with _pytest.raises(ValueError, match="INSUFFICIENT_CASH_AT_FILL"):
        projection.apply_fill_transaction(overdraw, enforce_cash=True)

    assert projection.get_cash_balance("acc-b5") == 10000
    n = db_conn.execute("SELECT COUNT(*) FROM fills WHERE account_id = 'acc-b5'").fetchone()[0]
    assert n == 0
    assert projection.reconcile("acc-b5")["status"] == "RECONCILE_OK"
