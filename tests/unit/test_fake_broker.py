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
    fills, status, unfilled = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)

    assert status == "FILLED"
    assert len(fills) == 1
    assert fills[0]["symbol"] == "2330"
    assert fills[0]["side"] == "BUY"
    assert fills[0]["price"] == 1001000
    assert fills[0]["quantity"] == 1000
    assert unfilled == []

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
    fills, status, unfilled = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)
    assert status == "WAITING_MARKET_DATA"
    assert len(fills) == 0
    assert unfilled == []


def test_fake_broker_limit_up_locked_buy_unfilled(temp_repo):
    """漲停鎖死（當日 high=low=close，較前日收盤 +10%）：BUY 單無對手 → UNFILLED。"""
    repo, conn = temp_repo
    repo.upsert(MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 10),
        open=1000000, high=1000000, low=1000000, close=1000000,
        volume=100, amount=10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    ))
    repo.upsert(MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1100000, high=1100000, low=1100000, close=1100000,  # 鎖死漲停 +10%
        volume=50, amount=5500,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    ))

    broker = FakeBroker(repo)
    orders = [{
        "signal_id": "sig-1", "symbol": "2330", "action": "open_long",
        "quantity": 1000, "is_odd_lot": False, "estimated_price": 110.0
    }]
    fills, status, unfilled = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)

    assert status == "FILLED"  # batch 仍處理，僅該筆 UNFILLED
    assert fills == []
    assert len(unfilled) == 1
    assert unfilled[0]["reason"] == "UNFILLED_LIMIT_UP_LOCKED"

    # 但 SELL（出場）在漲停鎖死當日仍可成交（賣方面對大量買單）
    sell_orders = [{
        "signal_id": "sig-2", "symbol": "2330", "action": "close_long",
        "quantity": 1000, "is_odd_lot": False, "estimated_price": 110.0
    }]
    fills2, status2, unfilled2 = broker.execute_orders(sell_orders, date(2026, 6, 11), slippage_bps=10)
    assert len(fills2) == 1
    assert unfilled2 == []


def test_fake_broker_zero_volume_unfilled(temp_repo):
    """零量（停牌）：不論方向皆 UNFILLED。"""
    repo, conn = temp_repo
    repo.upsert(MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1000000, low=1000000, close=1000000,
        volume=0, amount=0,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    ))

    broker = FakeBroker(repo)
    orders = [{
        "signal_id": "sig-1", "symbol": "2330", "action": "open_long",
        "quantity": 1000, "is_odd_lot": False, "estimated_price": 100.0
    }]
    fills, status, unfilled = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)

    assert fills == []
    assert len(unfilled) == 1
    assert unfilled[0]["reason"] == "UNFILLED_ZERO_VOLUME"


def test_fake_broker_odd_lot_extra_slippage(temp_repo):
    """零股折損：is_odd_lot 訂單滑價放大 3 倍。"""
    repo, conn = temp_repo
    repo.upsert(MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1020000, low=990000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    ))

    broker = FakeBroker(repo)
    orders = [{
        "signal_id": "sig-1", "symbol": "2330", "action": "open_long",
        "quantity": 30, "is_odd_lot": True, "estimated_price": 100.0
    }]
    fills, status, unfilled = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)

    assert unfilled == []
    # 1000000 * (1 + 30/10000) = 1003000（3x 滑價）
    assert fills[0]["price"] == 1003000


def test_fake_broker_backtest_mode_fills_others_when_one_symbol_missing(temp_repo):
    """A2：require_all_bars=False（回測）——缺 bar 的單記 UNFILLED_NO_BAR，
    其餘訂單照常成交；不得整批 WAITING 讓當天所有訂單消失。"""
    repo, conn = temp_repo
    repo.upsert(MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1020000, low=990000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    ))
    broker = FakeBroker(repo)
    orders = [
        {"signal_id": "sig-ok", "symbol": "2330", "action": "open_long",
         "quantity": 1000, "is_odd_lot": False, "estimated_price": 100.0},
        {"signal_id": "sig-halted", "symbol": "9999", "action": "close_long",
         "quantity": 500, "is_odd_lot": False, "estimated_price": 50.0},
    ]

    fills, status, unfilled = broker.execute_orders(
        orders, date(2026, 6, 11), slippage_bps=10, require_all_bars=False
    )

    assert status == "FILLED"
    assert [f["symbol"] for f in fills] == ["2330"]
    assert len(unfilled) == 1
    assert unfilled[0]["symbol"] == "9999"
    assert unfilled[0]["side"] == "SELL"
    assert unfilled[0]["reason"] == "UNFILLED_NO_BAR"

    # live 語意不變：require_all_bars 預設 True → 整批 WAITING
    fills2, status2, unfilled2 = broker.execute_orders(orders, date(2026, 6, 11), slippage_bps=10)
    assert status2 == "WAITING_MARKET_DATA"
    assert fills2 == [] and unfilled2 == []
