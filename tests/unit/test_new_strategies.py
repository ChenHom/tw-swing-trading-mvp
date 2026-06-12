"""trend_breakout 與 pullback_rebound 進場條件 + 大盤濾網測試。"""
from datetime import date, timedelta
from src.contracts.models import MarketBar, TrendBreakoutParams, PullbackReboundParams
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.trend_breakout import TrendBreakoutStrategy
from src.strategy.pullback_rebound import PullbackReboundStrategy


def make_bar(symbol, trade_date, close, open_p=None, low=None, volume=1000, instrument="STOCK"):
    open_p = open_p if open_p is not None else close
    low = low if low is not None else min(open_p, close)
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type=instrument,
        trade_date=trade_date,
        open=open_p, high=max(open_p, close), low=low, close=close,
        volume=volume, amount=0,
        source="test", source_fetched_at="now", raw_payload_checksum="chk"
    )


class MockPIT:
    def __init__(self, bars_by_symbol, as_of):
        self.bars = bars_by_symbol
        self._as_of = as_of

    @property
    def as_of_date(self):
        return self._as_of

    def history(self, symbol, limit):
        return self.bars.get(symbol, [])[-limit:]

    def latest(self, symbol):
        bars = self.bars.get(symbol, [])
        return bars[-1] if bars else None


def _dates(n, start=date(2026, 5, 1)):
    return [start + timedelta(days=i) for i in range(n)]


CTX = SignalGenerationContext(
    as_of_date=date(2026, 6, 10),
    strategy_id="trend_breakout",
    strategy_version="1.0.0",
    run_id="run-1",
    approval_id="app-1",
    params_hash="sha256:x"
)

EMPTY_PORTFOLIO = PortfolioSnapshot(available_cash=100000, positions={})


def _breakout_data(index_bullish=True, volume_spike=True, breakout=True):
    days = _dates(7)
    closes = [1000000] * 6 + [1060000 if breakout else 1000000]
    volumes = [1000] * 6 + [5000 if volume_spike else 1000]
    stock_bars = [make_bar("2330", d, c, volume=v) for d, c, v in zip(days, closes, volumes)]
    idx_base = 2000000
    idx_closes = [idx_base + i * 10000 for i in range(7)] if index_bullish else [idx_base - i * 10000 for i in range(7)]
    index_bars = [make_bar("TSE", d, c, instrument="INDEX", volume=0) for d, c in zip(days, idx_closes)]
    return MockPIT({"2330": stock_bars, "TSE": index_bars}, date(2026, 6, 10))


def _breakout_strategy():
    params = TrendBreakoutParams(
        breakout_lookback_days=5, volume_avg_days=5, volume_multiple_pct=150,
        ma_trend_period=5, index_ma_period=5, order_budget_twd=20000
    )
    return TrendBreakoutStrategy(params, ["2330"], "TSE")


def test_breakout_entry_triggers():
    bundle = _breakout_strategy().generate(CTX, _breakout_data(), EMPTY_PORTFOLIO)
    assert len(bundle.signals) == 1
    sig = bundle.signals[0]
    assert sig.action == "BUY"
    assert sig.reason_code == "TREND_BREAKOUT_ENTRY"
    assert sig.strategy_id == "trend_breakout"
    assert "trend_breakout" in sig.signal_id  # 防跨策略 signal_id 撞鍵
    assert "trend_breakout" in bundle.bundle_id


def test_breakout_blocked_by_index_filter():
    bundle = _breakout_strategy().generate(CTX, _breakout_data(index_bullish=False), EMPTY_PORTFOLIO)
    assert bundle.signals == []


def test_breakout_requires_volume_confirmation():
    bundle = _breakout_strategy().generate(CTX, _breakout_data(volume_spike=False), EMPTY_PORTFOLIO)
    assert bundle.signals == []


def test_breakout_skips_already_held_symbol():
    """已持有即不再進場（v3：不自動加碼）。"""
    portfolio = PortfolioSnapshot(
        available_cash=100000,
        positions={"2330": PositionSnapshot(symbol="2330", quantity=100, entry_price=1000000)}
    )
    bundle = _breakout_strategy().generate(CTX, _breakout_data(), portfolio)
    assert bundle.signals == []


def _pullback_data(index_bullish=True, red_candle=True, touch_support=True):
    n = 7
    days = _dates(n)
    # 多頭結構：價格自 100 緩漲至 ~112，sma5(短) > sma7(長)
    closes = [1000000 + i * 20000 for i in range(n)]
    bars = []
    for d, c in zip(days[:-1], closes[:-1]):
        bars.append(make_bar("2330", d, c))
    # 最後一日：回踩短均線後收紅
    last_close = closes[-1]
    last_open = last_close - 15000 if red_candle else last_close + 5000
    sma_short_approx = sum(closes[-5:]) / 5
    last_low = int(sma_short_approx * 0.99) if touch_support else last_close - 5000
    bars.append(make_bar("2330", days[-1], last_close, open_p=last_open, low=last_low))

    idx_base = 2000000
    idx_closes = [idx_base + i * 10000 for i in range(n)] if index_bullish else [idx_base - i * 10000 for i in range(n)]
    index_bars = [make_bar("TSE", d, c, instrument="INDEX", volume=0) for d, c in zip(days, idx_closes)]
    return MockPIT({"2330": bars, "TSE": index_bars}, date(2026, 6, 10))


def _pullback_strategy():
    params = PullbackReboundParams(
        ma_short=5, ma_long=6, pullback_touch_buffer_bps=200,
        index_ma_period=5, order_budget_twd=20000
    )
    return PullbackReboundStrategy(params, ["2330"], "TSE")


PB_CTX = CTX.model_copy(update={"strategy_id": "pullback_rebound"})


def test_pullback_entry_triggers():
    bundle = _pullback_strategy().generate(PB_CTX, _pullback_data(), EMPTY_PORTFOLIO)
    assert len(bundle.signals) == 1
    sig = bundle.signals[0]
    assert sig.reason_code == "PULLBACK_REBOUND_ENTRY"
    assert sig.strategy_id == "pullback_rebound"


def test_pullback_blocked_by_index_filter():
    bundle = _pullback_strategy().generate(PB_CTX, _pullback_data(index_bullish=False), EMPTY_PORTFOLIO)
    assert bundle.signals == []


def test_pullback_requires_strong_candle():
    bundle = _pullback_strategy().generate(PB_CTX, _pullback_data(red_candle=False), EMPTY_PORTFOLIO)
    assert bundle.signals == []


def test_pullback_requires_support_touch():
    bundle = _pullback_strategy().generate(PB_CTX, _pullback_data(touch_support=False), EMPTY_PORTFOLIO)
    assert bundle.signals == []
