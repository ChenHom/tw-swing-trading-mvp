import pytest
from datetime import date
from src.contracts.models import MarketBar, TrendPullbackParams
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.trend_pullback import TrendPullbackStrategy

class MockPointInTimeMarketData:
    def __init__(self, history_bars):
        self.history_bars = history_bars
        self.as_of_date = date(2026, 6, 10)

    def history(self, symbol, limit):
        return self.history_bars.get(symbol, [])[-limit:]

    def latest(self, symbol):
        bars = self.history_bars.get(symbol, [])
        return bars[-1] if bars else None

def test_strategy_insufficient_history():
    params = TrendPullbackParams(ma_short=2, ma_long=5)
    strategy = TrendPullbackStrategy(params, ["2330"])
    
    # We only provide 3 bars, but ma_long requires 5
    history = {
        "2330": [
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 8), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="shioaji", source_fetched_at="now", raw_payload_checksum="chk"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 9), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="shioaji", source_fetched_at="now", raw_payload_checksum="chk"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 10), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="shioaji", source_fetched_at="now", raw_payload_checksum="chk")
        ]
    }
    market_data = MockPointInTimeMarketData(history)
    portfolio = PortfolioSnapshot(available_cash=100000, positions={})
    
    context = SignalGenerationContext(
        as_of_date=date(2026, 6, 10),
        strategy_id="trend_pullback",
        strategy_version="1.0.0",
        run_id="run-1",
        approval_id="app-1",
        params_hash="hash"
    )
    
    bundle = strategy.generate(context, market_data, portfolio)
    assert len(bundle.signals) == 0

def test_strategy_buy_signal():
    params = TrendPullbackParams(ma_short=2, ma_long=5)
    strategy = TrendPullbackStrategy(params, ["2330"])
    
    # Prices:
    # Day 1: 90
    # Day 2: 90
    # Day 3: 92
    # Day 4: 105
    # Day 5: 98 (Pullback!)
    # ma_long (5 days) = (90 + 90 + 92 + 105 + 98)/5 = 95.0
    # ma_short (2 days) = (105 + 98)/2 = 101.5
    # Conditions:
    # 1. latest_close (98) > ma_long (95) -> True
    # 2. ma_short (101.5) > ma_long (95) -> True
    # 3. latest_close (98) < ma_short (101.5) -> True
    # Should trigger BUY signal!
    history = {
        "2330": [
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 4), open=900000, high=900000, low=900000, close=900000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 5), open=900000, high=900000, low=900000, close=900000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 8), open=920000, high=920000, low=920000, close=920000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 9), open=1050000, high=1050000, low=1050000, close=1050000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 10), open=980000, high=980000, low=980000, close=980000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c")
        ]
    }
    market_data = MockPointInTimeMarketData(history)
    portfolio = PortfolioSnapshot(available_cash=100000, positions={})
    
    context = SignalGenerationContext(
        as_of_date=date(2026, 6, 10),
        strategy_id="trend_pullback",
        strategy_version="1.0.0",
        run_id="run-1",
        approval_id="app-1",
        params_hash="hash"
    )
    
    bundle = strategy.generate(context, market_data, portfolio)
    assert len(bundle.signals) == 1
    assert bundle.signals[0].action == "BUY"
    assert bundle.signals[0].reference_price == 98.0
    assert bundle.signals[0].reason_code == "TREND_PULLBACK_ENTRY"

def test_strategy_stop_loss_exit():
    params = TrendPullbackParams(ma_short=2, ma_long=5, stop_loss_bps=500) # 5% SL
    strategy = TrendPullbackStrategy(params, ["2330"])
    
    # We hold 2330 with entry price 100.0 (1,000,000 scaled)
    # Today's close is 94.0 (940,000 scaled), which is a 6% drop (exceeds 5% SL)
    history = {
        "2330": [
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 3), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 4), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 5), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 8), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 10), open=940000, high=940000, low=940000, close=940000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c")
        ]
    }
    market_data = MockPointInTimeMarketData(history)
    portfolio = PortfolioSnapshot(
        available_cash=100000,
        positions={"2330": PositionSnapshot(symbol="2330", quantity=100, entry_price=1000000)}
    )
    
    context = SignalGenerationContext(
        as_of_date=date(2026, 6, 10),
        strategy_id="trend_pullback",
        strategy_version="1.0.0",
        run_id="run-1",
        approval_id="app-1",
        params_hash="hash"
    )
    
    bundle = strategy.generate(context, market_data, portfolio)
    assert len(bundle.signals) == 1
    assert bundle.signals[0].action == "SELL"
    assert bundle.signals[0].reason_code == "STOP_LOSS_EXIT"
