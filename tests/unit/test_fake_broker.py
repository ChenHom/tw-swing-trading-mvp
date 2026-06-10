import pytest
from datetime import date
from src.contracts.models import MarketBar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.broker.fake_broker import FakeBroker

@pytest.fixture
def temp_repo(tmp_path):
    db_file = tmp_path / "test_broker.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    repo = SqliteMarketBarRepository(conn)
    yield repo, conn
    conn.close()

def test_fake_broker_fill_success(temp_repo):
    repo, conn = temp_repo
    
    # 1. Insert daily bar for 2026-06-11
    # open price 100.0 (1000000 scaled)
    bar = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1020000, low=990000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    )
    repo.upsert(bar)
    
    broker = FakeBroker(repo)
    
    # Order plan: Buy 1000 shares of 2330
    orders = [
        {
            "signal_id": "sig-1",
            "symbol": "2330",
            "action": "open_long",
            "quantity": 1000,
            "is_odd_lot": False,
            "estimated_price": 100.0
        }
    ]
    
    # Slippage 10 bps
    # Fill price should be: 1000000 * (1 + 10 / 10000) = 1001000
    fills, status = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)
    
    assert status == "FILLED"
    assert len(fills) == 1
    assert fills[0]["symbol"] == "2330"
    assert fills[0]["side"] == "BUY"
    assert fills[0]["price"] == 1001000
    assert fills[0]["quantity"] == 1000

def test_fake_broker_waiting_market_data(temp_repo):
    repo, conn = temp_repo
    broker = FakeBroker(repo)
    
    orders = [
        {
            "signal_id": "sig-1",
            "symbol": "2330",
            "action": "open_long",
            "quantity": 1000,
            "is_odd_lot": False,
            "estimated_price": 100.0
        }
    ]
    
    # No market data for 2026-06-11, should return WAITING_MARKET_DATA
    fills, status = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)
    assert status == "WAITING_MARKET_DATA"
    assert len(fills) == 0
