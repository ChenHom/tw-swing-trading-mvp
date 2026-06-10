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
        "filled_at": "2026-06-11T09:00:00+08:00"
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
        "filled_at": "2026-06-12T09:00:00+08:00"
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
        "filled_at": "2026-06-11T09:00:00+08:00"
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

