"""breakout_shadow_filter：trend_breakout 基礎條件 + 近 N 日影線濾網（U > L 不進場）。"""
from datetime import date, timedelta

from src.contracts.models import MarketBar, BreakoutShadowFilterParams, TrendBreakoutParams
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.breakout_shadow_filter import BreakoutShadowFilterStrategy, shadow_sums
from src.strategy.trend_breakout import TrendBreakoutStrategy
from src.strategy import registry
from src.cli import common


def bar(symbol, d, close, open_=None, high=None, low=None, volume=1000, instrument="STOCK"):
    open_ = close if open_ is None else open_
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type=instrument, trade_date=d,
        open=open_, high=max(open_, close) if high is None else high,
        low=min(open_, close) if low is None else low, close=close,
        volume=volume, amount=0, source="test", source_fetched_at="now", raw_payload_checksum="chk",
    )


CTX = SignalGenerationContext(
    as_of_date=date(2026, 6, 10), strategy_id="breakout_shadow_filter", strategy_version="1.0.0",
    run_id="run-1", approval_id="app-1", params_hash="sha256:x",
)
EMPTY = PortfolioSnapshot(available_cash=100000, positions={})
BASE_KW = dict(breakout_lookback_days=5, volume_avg_days=5, volume_multiple_pct=150,
               ma_trend_period=5, index_ma_period=5, order_budget_twd=20000)


class MockPIT:
    def __init__(self, bars):
        self.bars = bars
    @property
    def as_of_date(self): return date(2026, 6, 10)
    def history(self, symbol, limit): return self.bars.get(symbol, [])[-limit:]
    def latest(self, symbol):
        b = self.bars.get(symbol, []); return b[-1] if b else None


def _data(last_bars):
    """前段平盤低量，最後幾根由呼叫者給（最後一根須是帶量突破）；大盤上升。"""
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    flat = [bar("2330", d, 1000000) for d in days[:10 - len(last_bars)]]
    stock = flat + [b.model_copy(update={"trade_date": d}) for b, d in zip(last_bars, days[10 - len(last_bars):])]
    idx = [bar("TSE", d, 2000000 + i * 10000, instrument="INDEX") for i, d in enumerate(days)]
    return MockPIT({"2330": stock, "TSE": idx})


def _strategy(window=3):
    return BreakoutShadowFilterStrategy(BreakoutShadowFilterParams(**BASE_KW, shadow_window_days=window), ["2330"], "TSE")


D0 = date(2026, 1, 1)


def test_shadow_sums_integer_definition():
    b = bar("X", D0, close=1050000, open_=1000000, high=1100000, low=950000)
    # 上影 = 110-105 = 5，下影 = 100-95 = 5，振幅 15（皆 ×10000）
    assert shadow_sums([b]) == (50000, 50000, 150000)


def test_breakout_with_dominant_lower_shadows_is_kept():
    last = [
        bar("2330", D0, 1000000, open_=1000000, high=1005000, low=980000),            # 下影 2
        bar("2330", D0, 1000000, open_=1000000, high=1005000, low=980000),            # 下影 2
        bar("2330", D0, 1100000, open_=1010000, high=1105000, low=990000, volume=5000),  # 突破：上 0.5 下 2
    ]
    bundle = _strategy().generate(CTX, _data(last), EMPTY)
    assert [s.symbol for s in bundle.signals] == ["2330"]
    s = bundle.signals[0]
    assert s.reason_code == "BREAKOUT_SHADOW_FILTER_ENTRY"
    assert s.strategy_id == "breakout_shadow_filter"
    assert s.signal_id == "sig-20260610-breakout_shadow_filter-2330-buy"


def test_breakout_with_dominant_upper_shadows_is_filtered():
    last = [
        bar("2330", D0, 1000000, open_=1000000, high=1030000, low=995000),             # 上影 3
        bar("2330", D0, 1000000, open_=1000000, high=1030000, low=995000),             # 上影 3
        bar("2330", D0, 1100000, open_=1010000, high=1180000, low=1005000, volume=5000),  # 突破後長上影 8
    ]
    strat = _strategy()
    data = _data(last)
    # 基準 trend_breakout 會進場 → 證明是濾網剔除，而非基礎條件不成立
    base = TrendBreakoutStrategy(TrendBreakoutParams(**BASE_KW), ["2330"], "TSE")
    assert len(base.generate(CTX, data, EMPTY).signals) == 1
    assert strat.generate(CTX, data, EMPTY).signals == []


def test_equal_shadows_are_kept_and_zero_range_bars_do_not_filter():
    # 一價到底（漲停鎖死類）：U = L = 0 → 不剔除，退回基準行為
    last = [bar("2330", D0, 1000000), bar("2330", D0, 1000000), bar("2330", D0, 1100000, open_=1100000, volume=5000)]
    bundle = _strategy().generate(CTX, _data(last), EMPTY)
    assert len(bundle.signals) == 1


def test_no_breakout_no_signal_and_holding_suppresses():
    last = [bar("2330", D0, 1000000)] * 3
    assert _strategy().generate(CTX, _data(last), EMPTY).signals == []
    breakout = [bar("2330", D0, 1000000), bar("2330", D0, 1000000),
                bar("2330", D0, 1100000, open_=1010000, high=1100000, low=990000, volume=5000)]
    held = PortfolioSnapshot(available_cash=0, positions={
        "2330": PositionSnapshot(symbol="2330", quantity=1000, entry_price=1000000, is_long_term=False)})
    assert _strategy().generate(CTX, _data(breakout), held).signals == []


def test_yaml_matches_trend_breakout_entry_and_exit():
    """thesis 要求：除 shadow_window_days 外，進場參數與出場參數與 trend_breakout 1.0.0 逐字相同。"""
    settings = common.get_settings()
    mine = registry.load_strategy_definition(settings, "breakout_shadow_filter")
    base = registry.load_strategy_definition(settings, "trend_breakout")
    assert mine.params.model_dump(exclude={"shadow_window_days"}) == base.params.model_dump()
    assert mine.params.shadow_window_days == 7
    assert mine.exit_params == base.exit_params
    assert mine.strategy_version == "1.0.0"


def test_signals_equal_base_minus_upper_dominant_on_random_data():
    """等價性：新策略訊號 ＝ trend_breakout 訊號 − {近 N 日 U > L}，逐日逐檔比對。"""
    import random
    rng = random.Random(11)
    symbols = [f"S{i}" for i in range(12)]
    n = 120
    ds = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    bars = {"TSE": [bar("TSE", d, 2000000 + i * 5000, instrument="INDEX") for i, d in enumerate(ds)]}
    for s in symbols:
        p, seq = 1000000, []
        for d in ds:
            o = p
            p = max(10000, int(p * (1 + rng.gauss(0.002, 0.03))))
            seq.append(bar(s, d, p, open_=o, high=max(o, p) + rng.randrange(0, 30000),
                           low=min(o, p) - rng.randrange(0, 30000), volume=int(rng.lognormvariate(7, 0.7))))
        bars[s] = seq
    base = TrendBreakoutStrategy(TrendBreakoutParams(**BASE_KW), symbols, "TSE")
    mine = BreakoutShadowFilterStrategy(BreakoutShadowFilterParams(**BASE_KW, shadow_window_days=7), symbols, "TSE")
    total_base = total_filtered = 0
    for i in range(10, n):
        view = MockPIT({k: v[:i + 1] for k, v in bars.items()})
        expected = []
        for s in base.generate(CTX, view, EMPTY).signals:
            total_base += 1
            u, l, _ = shadow_sums(view.history(s.symbol, 7))
            if u > l:
                total_filtered += 1
            else:
                expected.append(s.symbol)
        assert [s.symbol for s in mine.generate(CTX, view, EMPTY).signals] == expected
    assert total_base > 20 and 0 < total_filtered < total_base  # 兩種分支都有覆蓋到
