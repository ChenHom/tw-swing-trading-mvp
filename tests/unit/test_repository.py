import pytest
from datetime import date
from src.contracts.models import MarketBar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    yield conn
    conn.close()

def test_sqlite_repository_upsert_and_find(temp_db):
    repo = SqliteMarketBarRepository(temp_db)
    
    bar = MarketBar(
        symbol="2330",
        exchange="TSE",
        instrument_type="STOCK",
        trade_date=date(2026, 6, 10),
        open=1000000,
        high=1020000,
        low=990000,
        close=1010000,
        volume=100,
        amount=10000,
        source="shioaji",
        source_timezone="Asia/Taipei",
        is_complete=1,
        source_fetched_at="2026-06-10T14:00:00Z",
        raw_payload_checksum="chksum"
    )
    
    # Upsert the bar
    repo.upsert(bar)
    
    # Retrieve using find
    found = repo.find("2330", date(2026, 6, 10))
    assert found is not None
    assert found.symbol == "2330"
    assert found.open == 1000000
    assert found.high == 1020000
    assert found.close == 1010000
    assert found.volume == 100
    
    # Update and upsert again (ON CONFLICT test)
    bar.close = 1015000
    repo.upsert(bar)
    
    found_updated = repo.find("2330", date(2026, 6, 10))
    assert found_updated.close == 1015000

def test_sqlite_repository_point_in_time(temp_db):
    repo = SqliteMarketBarRepository(temp_db)
    
    # Insert 3 days of data: 2026-06-08, 2026-06-09, 2026-06-10
    dates = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    for i, d in enumerate(dates):
        bar = MarketBar(
            symbol="2330",
            exchange="TSE",
            instrument_type="STOCK",
            trade_date=d,
            open=1000000 + i * 10000,
            high=1020000 + i * 10000,
            low=990000 + i * 10000,
            close=1010000 + i * 10000,
            volume=100,
            amount=10000,
            source="shioaji",
            source_timezone="Asia/Taipei",
            is_complete=1,
            source_fetched_at="2026-06-10",
            raw_payload_checksum="chk"
        )
        repo.upsert(bar)
        
    # Get a point-in-time view as of 2026-06-09
    pit_view = repo.as_of(date(2026, 6, 9))
    assert pit_view.as_of_date == date(2026, 6, 9)
    
    # Latest should be 2026-06-09
    latest = pit_view.latest("2330")
    assert latest is not None
    assert latest.trade_date == date(2026, 6, 9)
    assert latest.close == 1020000 # 1010000 + 10000
    
    # History with limit 3 as of 2026-06-09 should only return 2 bars (2026-06-08 and 2026-06-09)
    # sorted chronologically (ascending): 2026-06-08 first, 2026-06-09 second.
    history = pit_view.history("2330", limit=3)
    assert len(history) == 2
    assert history[0].trade_date == date(2026, 6, 8)
    assert history[1].trade_date == date(2026, 6, 9)
    
    # History with limit 1 as of 2026-06-09
    history_limit_1 = pit_view.history("2330", limit=1)
    assert len(history_limit_1) == 1
    assert history_limit_1[0].trade_date == date(2026, 6, 9)
