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

def test_order_planner_plan_all_sequential_cash_and_sorting(manifest_limits):
    # Setup signals: 2 BUY signals
    # 2330 with ranking_score 1.0 (should be processed first)
    # 2317 with ranking_score 0.5 (should be processed second)
    sig_1 = SignalItem(signal_id="sig-buy-2330", symbol="2330", action="BUY", reference_price=500.0, reason_code="ENTRY")
    # We set ranking_score dynamically
    sig_1.ranking_score = 1.0
    
    sig_2 = SignalItem(signal_id="sig-buy-2317", symbol="2317", action="BUY", reference_price=100.0, reason_code="ENTRY")
    sig_2.ranking_score = 0.5
    
    # Portfolio cash is 21,000 (enough for 2330 which needs 20000 + fee, but not enough for 2317 after 2330 is bought)
    portfolio = PortfolioState(available_cash=21000, positions={}, daily_buy_value_spent=0)
    
    planned_orders, results = OrderPlanner.plan_all(
        signals=[sig_2, sig_1], # Pass in reversed order to test sorting
        portfolio=portfolio,
        strategy_budget=30000,
        manifest_limits=manifest_limits
    )
    
    # 2330 should be processed first because of higher ranking_score.
    # 2330 budget capped by max_order_value (20000). Qty = 40. Cost = 20000 + 28 (fee) = 20028.
    # Remaining cash = 21000 - 20028 = 972.
    # 2317 budget is min(30000, 20000, 40000-20000, 972) = 972.
    # Price is 100.0. Qty = 9. Cost = 900 + 20 (fee) = 920.
    # Total planned orders should be for both, but 2317 is down-sized to fit cash.
    assert len(planned_orders) == 2
    
    orders_2330 = results["sig-buy-2330"]
    assert len(orders_2330) == 1
    assert orders_2330[0]["symbol"] == "2330"
    assert orders_2330[0]["quantity"] == 40
    
    orders_2317 = results["sig-buy-2317"]
    assert len(orders_2317) == 1
    assert orders_2317[0]["symbol"] == "2317"
    assert orders_2317[0]["quantity"] == 9 # down-sized from 200 to 9

def test_order_planner_plan_all_max_two_new_positions(manifest_limits):
    # Setup 3 BUY signals for new positions (none currently held)
    sig_1 = SignalItem(signal_id="sig-1", symbol="2330", action="BUY", reference_price=100.0, reason_code="ENTRY")
    sig_1.ranking_score = 3.0
    sig_2 = SignalItem(signal_id="sig-2", symbol="2317", action="BUY", reference_price=100.0, reason_code="ENTRY")
    sig_2.ranking_score = 2.0
    sig_3 = SignalItem(signal_id="sig-3", symbol="2303", action="BUY", reference_price=100.0, reason_code="ENTRY")
    sig_3.ranking_score = 1.0
    
    # Portfolio cash is large
    portfolio = PortfolioState(available_cash=100000, positions={}, daily_buy_value_spent=0)
    
    # Max open positions in manifest limits is 5, but daily new buys count cap is 2
    limits = LimitsInfo(currency="TWD", max_order_value=10000, max_daily_buy_value=50000, max_open_positions=5)
    
    planned_orders, results = OrderPlanner.plan_all(
        signals=[sig_3, sig_2, sig_1],
        portfolio=portfolio,
        strategy_budget=10000,
        manifest_limits=limits
    )
    
    # Only sig-1 and sig-2 (highest ranking scores) should be planned. sig-3 should be rejected.
    assert len(planned_orders) == 2
    assert "sig-1" in results and not isinstance(results["sig-1"], str)
    assert "sig-2" in results and not isinstance(results["sig-2"], str)
    assert "sig-3" in results and isinstance(results["sig-3"], str)
    assert "DAILY_NEW_BUY_LIMIT_EXCEEDED" in results["sig-3"]

def test_order_planner_plan_all_sell_priority_frees_cash(manifest_limits):
    # SELL signal for 2330: we hold 100 shares at price 100.0.
    # Estimated proceeds = 100 * 100 = 10,000. Fee = 20. Tax = 30. Net proceeds = 9950.
    sig_sell = SignalItem(signal_id="sig-sell", symbol="2330", action="SELL", reference_price=100.0, reason_code="EXIT")
    
    # BUY signal for 2317: requires cash.
    sig_buy = SignalItem(signal_id="sig-buy", symbol="2317", action="BUY", reference_price=100.0, reason_code="ENTRY")
    sig_buy.ranking_score = 1.0
    
    # Portfolio cash is initially 500 (not enough for 2317 BUY which needs at least 100 + 20 fee = 120 TWD).
    # But after SELL is processed, cash will be 500 + 9950 = 10450.
    # That is enough to buy 2317!
    portfolio = PortfolioState(
        available_cash=500,
        positions={"2330": 100},
        daily_buy_value_spent=0
    )
    
    planned_orders, results = OrderPlanner.plan_all(
        signals=[sig_buy, sig_sell],
        portfolio=portfolio,
        strategy_budget=10000,
        manifest_limits=manifest_limits
    )
    
    assert len(planned_orders) == 2
    assert "sig-sell" in results and not isinstance(results["sig-sell"], str)
    assert "sig-buy" in results and not isinstance(results["sig-buy"], str)
    # 2317 should successfully plan order for 100 shares (capped by order budget 10000)
    assert results["sig-buy"][0]["quantity"] == 100

