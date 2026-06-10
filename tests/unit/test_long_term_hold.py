import pytest
from datetime import date, datetime
import sqlite3
import os
from src.contracts.models import MarketBar, TrendPullbackParams
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.trend_pullback import TrendPullbackStrategy
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.cli import cmd_trade_record_fill

class MockPointInTimeMarketData:
    def __init__(self, history_bars):
        self.history_bars = history_bars
        self.as_of_date = date(2026, 6, 10)

    def history(self, symbol, limit):
        return self.history_bars.get(symbol, [])[-limit:]

    def latest(self, symbol):
        bars = self.history_bars.get(symbol, [])
        return bars[-1] if bars else None

def test_strategy_excludes_long_term_position():
    # Setup strategy params (5% stop loss, 10% take profit)
    params = TrendPullbackParams(ma_short=2, ma_long=5, stop_loss_bps=500, take_profit_bps=1000)
    strategy = TrendPullbackStrategy(params, ["2330"])
    
    # 5 bars, latest close is 90.0. 
    # Since entry price is 100.0, this would normally trigger a STOP_LOSS_EXIT (10% drop > 5% SL)
    history = {
        "2330": [
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 3), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 4), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 5), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 8), open=1000000, high=1000000, low=1000000, close=1000000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c"),
            MarketBar(symbol="2330", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 10), open=900000, high=900000, low=900000, close=900000, volume=1, amount=1, source="s", source_fetched_at="n", raw_payload_checksum="c")
        ]
    }
    market_data = MockPointInTimeMarketData(history)
    
    # 1. Normal position: should exit
    portfolio_normal = PortfolioSnapshot(
        available_cash=100000,
        positions={"2330": PositionSnapshot(symbol="2330", quantity=100, entry_price=1000000, is_long_term=False)}
    )
    context = SignalGenerationContext(
        as_of_date=date(2026, 6, 10),
        strategy_id="trend_pullback",
        strategy_version="1.0.0",
        run_id="run-1",
        approval_id="app-1",
        params_hash="hash"
    )
    
    bundle_normal = strategy.generate(context, market_data, portfolio_normal)
    assert len(bundle_normal.signals) == 1
    assert bundle_normal.signals[0].action == "SELL"
    assert bundle_normal.signals[0].reason_code == "STOP_LOSS_EXIT"
    
    # 2. Long-term position: should NOT exit
    portfolio_long_term = PortfolioSnapshot(
        available_cash=100000,
        positions={"2330": PositionSnapshot(symbol="2330", quantity=100, entry_price=1000000, is_long_term=True)}
    )
    
    bundle_long_term = strategy.generate(context, market_data, portfolio_long_term)
    assert len(bundle_long_term.signals) == 0

def test_db_long_term_fill_record(tmp_path):
    db_file = str(tmp_path / "test_app.db")
    init_db(db_file)
    
    conn = get_db_connection(db_file)
    projection = PortfolioProjection(conn)
    
    # Insert initial cash
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO cash_balances (account_id, balance, currency, updated_at)
        VALUES ('simulation-main', 1000000, 'TWD', datetime('now'))
        """
    )
    cursor.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency, source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES ('dep-1', 'simulation-main', 'run-init', 'INITIAL_DEPOSIT', 1000000, 'TWD', 'SYSTEM', 'sys-1', datetime('now'), 'idemp-1', datetime('now'))
        """
    )
    conn.commit()
    
    # Record a long-term fill
    fill_payload = {
        "fill_id": "fill-manual-1",
        "account_id": "simulation-main",
        "run_id": "manual-run-1",
        "order_id": "ord-manual-1",
        "execution_key": "manual-fill-2330-1",
        "symbol": "2330",
        "side": "BUY",
        "quantity": 10,
        "price": 1000000,  # 100.0
        "filled_at": datetime.now().isoformat(),
        "is_long_term": 1
    }
    
    projection.apply_fill_transaction(fill_payload)
    
    # Check fills
    cursor.execute("SELECT is_long_term FROM fills WHERE fill_id = 'fill-manual-1'")
    fill_row = cursor.fetchone()
    assert fill_row["is_long_term"] == 1
    
    # Check position lots
    cursor.execute("SELECT is_long_term FROM position_lots WHERE symbol = '2330'")
    lot_row = cursor.fetchone()
    assert lot_row["is_long_term"] == 1
    
    # Rebuild from ledger
    projection.rebuild_from_ledger("simulation-main")
    
    # Verify still is_long_term after rebuild
    cursor.execute("SELECT is_long_term FROM position_lots WHERE symbol = '2330'")
    lot_row_rebuilt = cursor.fetchone()
    assert lot_row_rebuilt["is_long_term"] == 1
    
    # Try to sell 5 shares as a strategy exit (is_long_term = 0).
    # This should fail because we only hold long-term lots (is_long_term = 1),
    # so there are no matching managed lots to match.
    sell_payload = {
        "fill_id": "fill-manual-sell-1",
        "account_id": "simulation-main",
        "run_id": "manual-run-2",
        "order_id": "ord-manual-2",
        "execution_key": "manual-fill-2330-sell-1",
        "symbol": "2330",
        "side": "SELL",
        "quantity": 5,
        "price": 1100000, # 110.0
        "filled_at": datetime.now().isoformat(),
        "is_long_term": 0
    }
    
    with pytest.raises(ValueError, match="SELL_WITHOUT_POSITION"):
        projection.apply_fill_transaction(sell_payload)
        
    # Now record a BUY with is_long_term = 0 (strategy position)
    buy_managed_payload = {
        "fill_id": "fill-manual-buy-2",
        "account_id": "simulation-main",
        "run_id": "manual-run-3",
        "order_id": "ord-manual-3",
        "execution_key": "manual-fill-2330-buy-2",
        "symbol": "2330",
        "side": "BUY",
        "quantity": 5,
        "price": 1000000,
        "filled_at": datetime.now().isoformat(),
        "is_long_term": 0
    }
    projection.apply_fill_transaction(buy_managed_payload)
    
    # Now SELL 5 shares of managed position. This should succeed!
    projection.apply_fill_transaction(sell_payload)
    
    # Verify that the long-term lot of 10 shares remains untouched
    cursor.execute("SELECT quantity, is_long_term FROM position_lots WHERE symbol = '2330'")
    lots = cursor.fetchall()
    assert len(lots) == 1
    assert lots[0]["quantity"] == 10
    assert lots[0]["is_long_term"] == 1
    
    conn.close()
