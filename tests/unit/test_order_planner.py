import pytest
from src.contracts.models import SignalItem, LimitsInfo
from src.trading.planner import OrderPlanner, PortfolioState

@pytest.fixture
def manifest_limits():
    return LimitsInfo(
        currency="TWD",
        max_order_value=20000,
        max_daily_buy_value=40000,
        max_open_positions=2
    )

def test_order_planner_buy_open_long(manifest_limits):
    signal = SignalItem(
        signal_id="sig-1",
        symbol="2330",
        action="BUY",
        reference_price=500.0,
        reason_code="ENTRY"
    )
    
    # Portfolio has cash 100,000 and 0 positions
    portfolio = PortfolioState(
        available_cash=100000,
        positions={},
        daily_buy_value_spent=0
    )
    
    # Strategy order budget is 30,000, but manifest limit is 20,000.
    # Therefore, order_budget = min(30000, 20000, 40000, 10000) = 20000
    strategy_budget = 30000
    
    orders = OrderPlanner.plan_order(
        signal=signal,
        portfolio=portfolio,
        strategy_budget=strategy_budget,
        manifest_limits=manifest_limits
    )
    
    # 20,000 budget / 500 estimated price = 40 shares.
    # 40 shares is < 1000, so it should be 40 odd shares.
    assert len(orders) == 1
    assert orders[0]["action"] == "open_long"
    assert orders[0]["quantity"] == 40
    assert orders[0]["is_odd_lot"] is True

def test_order_planner_buy_split_lots(manifest_limits):
    # Stock is cheap, so we buy more shares
    signal = SignalItem(
        signal_id="sig-2",
        symbol="2317",
        action="BUY",
        reference_price=15.0,
        reason_code="ENTRY"
    )
    # Cash 100,000, strategy budget 30,000. Manifest limit 20,000.
    # Target quantity = 20000 / 15.0 = 1333.33 -> 1333 shares.
    # Splits into: 1000 board lot, 333 odd lot.
    portfolio = PortfolioState(available_cash=100000, positions={}, daily_buy_value_spent=0)
    orders = OrderPlanner.plan_order(signal, portfolio, 30000, manifest_limits)
    
    assert len(orders) == 2
    # Board lot
    assert orders[0]["action"] == "open_long"
    assert orders[0]["quantity"] == 1000
    assert orders[0]["is_odd_lot"] is False
    # Odd lot
    assert orders[1]["action"] == "open_long"
    assert orders[1]["quantity"] == 333
    assert orders[1]["is_odd_lot"] is True

def test_order_planner_max_positions_exceeded(manifest_limits):
    signal = SignalItem(
        signal_id="sig-3",
        symbol="2303",
        action="BUY",
        reference_price=50.0,
        reason_code="ENTRY"
    )
    # We already have 2 open positions (2330 and 2317), which equals max_open_positions=2
    portfolio = PortfolioState(
        available_cash=100000,
        positions={"2330": 1000, "2317": 500},
        daily_buy_value_spent=0
    )
    
    with pytest.raises(ValueError, match="MAX_OPEN_POSITIONS_EXCEEDED"):
        OrderPlanner.plan_order(signal, portfolio, 30000, manifest_limits)

def test_order_planner_sell_close_long():
    signal = SignalItem(
        signal_id="sig-4",
        symbol="2330",
        action="SELL",
        reference_price=500.0,
        reason_code="EXIT"
    )
    # We hold 1200 shares of 2330
    portfolio = PortfolioState(
        available_cash=100000,
        positions={"2330": 1200},
        daily_buy_value_spent=0
    )
    # Sell is not subject to manifest buy limits
    limits = LimitsInfo(currency="TWD", max_order_value=20000, max_daily_buy_value=40000, max_open_positions=2)
    
    orders = OrderPlanner.plan_order(signal, portfolio, 30000, limits)
    
    # Splits 1200 shares into 1000 board lot, 200 odd lot
    assert len(orders) == 2
    assert orders[0]["action"] == "close_long"
    assert orders[0]["quantity"] == 1000
    assert orders[0]["is_odd_lot"] is False
    
    assert orders[1]["action"] == "close_long"
    assert orders[1]["quantity"] == 200
    assert orders[1]["is_odd_lot"] is True

def test_order_planner_sell_without_position():
    signal = SignalItem(
        signal_id="sig-5",
        symbol="2454",
        action="SELL",
        reference_price=900.0,
        reason_code="EXIT"
    )
    portfolio = PortfolioState(available_cash=100000, positions={}, daily_buy_value_spent=0)
    limits = LimitsInfo(currency="TWD", max_order_value=20000, max_daily_buy_value=40000, max_open_positions=2)
    
    orders = OrderPlanner.plan_order(signal, portfolio, 30000, limits)
    # Exits without position are ignored
    assert len(orders) == 0
