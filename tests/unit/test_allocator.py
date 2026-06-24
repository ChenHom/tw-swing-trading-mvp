"""多策略 Allocator：netting、T+1 現金、雙層限額測試。"""
from src.contracts.models import SignalItem, LimitsInfo
from src.trading.allocator import MultiStrategyAllocator, MultiPortfolioState, GlobalLimits


def sig(signal_id, symbol, action, price, strategy_id, source="ENTRY", ranking=None):
    return SignalItem(
        signal_id=signal_id, symbol=symbol, action=action,
        reference_price=price, reason_code="TEST",
        strategy_id=strategy_id, signal_source=source, ranking_score=ranking
    )


def limits(max_order=50000, max_daily=100000, max_pos=5):
    return LimitsInfo(currency="TWD", max_order_value=max_order, max_daily_buy_value=max_daily, max_open_positions=max_pos)


GLOBAL = GlobalLimits(max_open_positions=8, max_daily_buy_value=200000, max_new_positions_per_day=2)


def test_same_day_netting_suppresses_entry():
    """A 策略出場 2330，B 策略同日進場 2330 → 抑制 BUY 並記事件。"""
    portfolio = MultiPortfolioState(
        available_cash=500000,
        strategy_positions={("trend_breakout", "2330"): 1000},
    )
    sells = [sig("s1", "2330", "SELL", 100.0, "trend_breakout", source="RISK_EXIT")]
    buys = [sig("b1", "2330", "BUY", 100.0, "pullback_rebound")]

    orders, results, events = MultiStrategyAllocator.plan(
        sells, buys, portfolio,
        strategy_budgets={"trend_breakout": 20000, "pullback_rebound": 20000},
        strategy_limits={"trend_breakout": limits(), "pullback_rebound": limits()},
        global_limits=GLOBAL,
    )

    assert any(o["action"] == "close_long" for o in orders)
    assert not any(o["action"] == "open_long" for o in orders)
    assert isinstance(results["b1"], str) and results["b1"].startswith("NETTING_SUPPRESSED")
    assert events[0]["event_type"] == "NETTING_SUPPRESSED"
    assert events[0]["symbol"] == "2330"


def test_t_plus_1_sell_proceeds_not_available_same_day():
    """T+1：當日賣出款不得用於當日買入。"""
    portfolio = MultiPortfolioState(
        available_cash=50,  # 幾乎沒現金
        strategy_positions={("trend_breakout", "2330"): 1000},
    )
    sells = [sig("s1", "2330", "SELL", 100.0, "trend_breakout", source="RISK_EXIT")]
    buys = [sig("b1", "2317", "BUY", 100.0, "pullback_rebound")]

    orders, results, events = MultiStrategyAllocator.plan(
        sells, buys, portfolio,
        strategy_budgets={"pullback_rebound": 20000},
        strategy_limits={"pullback_rebound": limits()},
        global_limits=GLOBAL,
    )

    # 賣出成立（釋放約 10 萬），但 BUY 仍因現金不足被擋
    assert any(o["action"] == "close_long" for o in orders)
    assert isinstance(results["b1"], str) and "INSUFFICIENT_CASH" in results["b1"]


def test_sell_quantity_scoped_to_strategy_bucket():
    """SELL 數量 = 該策略 bucket 全數，不含他策略同標的持股。"""
    portfolio = MultiPortfolioState(
        available_cash=0,
        strategy_positions={
            ("trend_breakout", "2330"): 1200,
            ("pullback_rebound", "2330"): 500,
        },
    )
    sells = [sig("s1", "2330", "SELL", 100.0, "trend_breakout", source="RISK_EXIT")]
    orders, results, _ = MultiStrategyAllocator.plan(
        sells, [], portfolio, {}, {}, GLOBAL,
    )
    total = sum(o["quantity"] for o in orders)
    assert total == 1200
    assert all(o["strategy_id"] == "trend_breakout" for o in orders)
    # 整張 + 零股拆單
    assert sorted(o["quantity"] for o in orders) == [200, 1000]


def test_per_strategy_and_global_limits():
    """策略限額與全局限額雙層生效。"""
    portfolio = MultiPortfolioState(
        available_cash=1000000,
        strategy_positions={("trend_breakout", "2317"): 100},  # A 已持 1 檔
    )
    buys = [
        sig("a1", "2330", "BUY", 100.0, "trend_breakout"),     # A max_pos=1 → 擋
        sig("b1", "2454", "BUY", 100.0, "pullback_rebound"),   # OK (新倉 1)
        sig("b2", "2603", "BUY", 100.0, "pullback_rebound"),   # OK (新倉 2)
        sig("b3", "2609", "BUY", 100.0, "pullback_rebound"),   # 全局每日新倉上限 2 → 擋
    ]
    orders, results, _ = MultiStrategyAllocator.plan(
        [], buys, portfolio,
        strategy_budgets={"trend_breakout": 20000, "pullback_rebound": 20000},
        strategy_limits={"trend_breakout": limits(max_pos=1), "pullback_rebound": limits()},
        global_limits=GLOBAL,
    )
    assert "MAX_OPEN_POSITIONS_EXCEEDED" in results["a1"]
    assert isinstance(results["b1"], list)
    assert isinstance(results["b2"], list)
    assert "DAILY_NEW_BUY_LIMIT_EXCEEDED" in results["b3"]


def test_missing_strategy_limits_blocks_buy():
    """無對應 manifest 限額（未授權）→ BUY 阻擋。"""
    portfolio = MultiPortfolioState(available_cash=100000, strategy_positions={})
    buys = [sig("b1", "2330", "BUY", 100.0, "trend_breakout")]
    _, results, _ = MultiStrategyAllocator.plan(
        [], buys, portfolio, {"trend_breakout": 20000}, {}, GLOBAL,
    )
    assert "APPROVAL_NOT_FOUND" in results["b1"]


def test_strategy_daily_buy_limit():
    """per-strategy 每日買入限額獨立計算。"""
    portfolio = MultiPortfolioState(
        available_cash=1000000,
        strategy_positions={},
        strategy_daily_buy_spent={"trend_breakout": 99950},
    )
    buys = [sig("b1", "2330", "BUY", 100.0, "trend_breakout")]
    _, results, _ = MultiStrategyAllocator.plan(
        [], buys, portfolio,
        {"trend_breakout": 20000},
        {"trend_breakout": limits(max_daily=100000)},
        GLOBAL,
    )
    # 剩餘額度 50 元買不起 1 股 100 元
    assert isinstance(results["b1"], str)


def test_no_add_blocks_buy_when_already_holding():
    """已持有的策略/標的收到 BUY → 擋下（ALREADY_HOLDING），不加碼。
    防守全域共用 bundle：別的帳號（無此持倉）產出的 BUY 流到本帳號時，
    本帳號 allocator 仍以自己活的持倉為準拒絕加碼（3090 日電貿事件根因）。"""
    portfolio = MultiPortfolioState(
        available_cash=500000,
        strategy_positions={("trend_breakout", "3090"): 4},  # 已持有 4 股
    )
    buys = [sig("b1", "3090", "BUY", 320.0, "trend_breakout")]

    orders, results, events = MultiStrategyAllocator.plan(
        [], buys, portfolio,
        strategy_budgets={"trend_breakout": 20000},
        strategy_limits={"trend_breakout": limits()},
        global_limits=GLOBAL,
    )

    assert isinstance(results["b1"], str) and results["b1"].startswith("ALREADY_HOLDING")
    assert orders == []  # 沒有任何加碼單


def test_no_add_still_allows_fresh_symbol():
    """同策略對「未持有」標的的 BUY 不受影響，照常開倉。"""
    portfolio = MultiPortfolioState(
        available_cash=500000,
        strategy_positions={("trend_breakout", "3090"): 4},
    )
    buys = [sig("b1", "2330", "BUY", 100.0, "trend_breakout")]  # 未持有 2330

    orders, results, events = MultiStrategyAllocator.plan(
        [], buys, portfolio,
        strategy_budgets={"trend_breakout": 20000},
        strategy_limits={"trend_breakout": limits()},
        global_limits=GLOBAL,
    )

    assert any(o["action"] == "open_long" and o["symbol"] == "2330" for o in orders)
