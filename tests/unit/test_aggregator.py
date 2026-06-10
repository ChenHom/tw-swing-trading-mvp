import pytest
from datetime import date, datetime
from src.contracts.models import MinuteBar, MarketBar
from src.market_data.aggregator import DailyBarAggregator, MarketBarValidator

def test_daily_bar_aggregator():
    trade_date = date(2026, 6, 10)
    # Generate some mock minute bars
    # One before session, three inside session, one after session
    bars = [
        MinuteBar(time=datetime(2026, 6, 10, 8, 59, 0), open=100.0, high=100.5, low=99.5, close=100.0, volume=10, amount=1000.0),
        MinuteBar(time=datetime(2026, 6, 10, 9, 0, 0), open=100.0, high=101.0, low=99.0, close=99.5, volume=10, amount=1000.0),
        MinuteBar(time=datetime(2026, 6, 10, 11, 0, 0), open=99.5, high=102.5, low=99.5, close=102.0, volume=20, amount=2020.0),
        MinuteBar(time=datetime(2026, 6, 10, 13, 30, 0), open=102.0, high=102.0, low=101.0, close=101.5, volume=15, amount=1522.5),
        MinuteBar(time=datetime(2026, 6, 10, 13, 31, 0), open=101.5, high=102.0, low=101.0, close=101.5, volume=5, amount=500.0),
    ]
    
    daily_bar = DailyBarAggregator.aggregate(
        symbol="2330",
        exchange="TSE",
        instrument_type="STOCK",
        trade_date=trade_date,
        minute_bars=bars,
        source="shioaji",
        source_fetched_at="2026-06-10T14:00:00Z",
        raw_payload_checksum="md5sum123"
    )
    
    assert daily_bar is not None
    assert daily_bar.symbol == "2330"
    # Prices should be scaled x 10000
    # First regular bar is at 09:00:00 -> open=100.0 -> 1000000
    assert daily_bar.open == 1000000
    # Last regular bar is at 13:30:00 -> close=101.5 -> 1015000
    assert daily_bar.close == 1015000
    # Max high of regular bars is 102.5 -> 1025000
    assert daily_bar.high == 1025000
    # Min low of regular bars is 99.0 -> 990000
    assert daily_bar.low == 990000
    # Sum of volumes for regular bars: 10 + 20 + 15 = 45
    assert daily_bar.volume == 45
    # Sum of amounts for regular bars: 1000.0 + 2020.0 + 1522.5 = 4542.5 -> 4542 (banker's rounding)
    assert daily_bar.amount == 4542
    
def test_market_bar_validator_success():
    bar = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 10),
        open=1000000, high=1020000, low=990000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_fetched_at="now", raw_payload_checksum="sum"
    )
    # Should not raise any error
    MarketBarValidator.validate(bar)

def test_market_bar_validator_failures():
    # Invalid High (less than open)
    bar = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 10),
        open=1000000, high=990000, low=950000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_fetched_at="now", raw_payload_checksum="sum"
    )
    with pytest.raises(ValueError, match="MARKET_BAR_INVALID"):
        MarketBarValidator.validate(bar)
        
    # Negative Price
    bar2 = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 10),
        open=-1000000, high=1020000, low=990000, close=1010000,
        volume=100, amount=10000,
        source="shioaji", source_fetched_at="now", raw_payload_checksum="sum"
    )
    with pytest.raises(ValueError, match="MARKET_BAR_INVALID"):
        MarketBarValidator.validate(bar2)
