"""trend_rider 進場條件（確立上升趨勢 + 大盤濾網）+ 讓贏家跑 exit config 測試。"""
from datetime import date, timedelta

from src.contracts.models import MarketBar, TrendRiderParams
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.trend_rider import TrendRiderStrategy


def make_bar(symbol, trade_date, close, instrument="STOCK"):
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type=instrument, trade_date=trade_date,
        open=close, high=close, low=close, close=close, volume=1000, amount=0,
        source="test", source_fetched_at="now", raw_payload_checksum="chk",
    )


def _dates(n, start=date(2026, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


CTX = SignalGenerationContext(
    as_of_date=date(2026, 6, 10), strategy_id="trend_rider", strategy_version="1.0.0",
    run_id="run-1", approval_id="app-1", params_hash="sha256:x",
)
EMPTY = PortfolioSnapshot(available_cash=100000, positions={})


class MockPIT:
    def __init__(self, bars, as_of):
        self.bars = bars; self._as_of = as_of
    @property
    def as_of_date(self): return self._as_of
    def history(self, symbol, limit): return self.bars.get(symbol, [])[-limit:]
    def latest(self, symbol):
        b = self.bars.get(symbol, []); return b[-1] if b else None


def _strategy():
    p = TrendRiderParams(trend_ma_period=5, breakout_lookback_days=10, index_ma_period=5, order_budget_twd=20000)
    return TrendRiderStrategy(p, ["2330"], "TSE")


def _data(stock_closes, index_rising=True):
    n = len(stock_closes)
    days = _dates(n)
    stock = [make_bar("2330", d, c) for d, c in zip(days, stock_closes)]
    idx = [make_bar("TSE", d, 2000000 + (i if index_rising else -i) * 10000, "INDEX") for i, d in enumerate(days)]
    return MockPIT({"2330": stock, "TSE": idx}, date(2026, 6, 10))


def test_established_uptrend_triggers_entry():
    # 穩定上升 16 天：MA 上升、創新高、close>MA、大盤多頭 → 進場
    closes = [1000000 + i * 20000 for i in range(16)]
    bundle = _strategy().generate(CTX, _data(closes, index_rising=True), EMPTY)
    assert len(bundle.signals) == 1
    assert bundle.signals[0].action == "BUY"
    assert bundle.signals[0].reason_code == "TREND_RIDER_ENTRY"


def test_flat_market_no_new_high_no_entry():
    # 全平盤：無新高、MA 不上升 → 不進場
    closes = [1000000] * 16
    bundle = _strategy().generate(CTX, _data(closes, index_rising=True), EMPTY)
    assert bundle.signals == []


def test_index_below_ma_blocks_entry_crash_defense():
    # 個股強勢上升，但大盤空頭（跌破濾網）→ 崩盤防守：不進場
    closes = [1000000 + i * 20000 for i in range(16)]
    bundle = _strategy().generate(CTX, _data(closes, index_rising=False), EMPTY)
    assert bundle.signals == []


def test_downtrend_with_late_bounce_no_entry():
    # 長下跌後小反彈：今日非 10 日新高、且中期 MA 仍向下 → 不進場（要求趨勢確立向上）
    closes = [2000000 - i * 30000 for i in range(12)] + [1640000 + i * 5000 for i in range(4)]
    bundle = _strategy().generate(CTX, _data(closes, index_rising=True), EMPTY)
    assert bundle.signals == []


def test_exit_config_disables_time_stop_and_widens_trailing():
    # 「讓贏家跑」的核心：YAML exit 載入後 time_stop 實質停用、移動停利寬。
    from src.cli import common
    from src.strategy import registry
    defn = registry.load_strategy_definition(common.get_settings(), "trend_rider")
    assert defn.exit_params.time_stop_days >= 999       # 時間停損實質停用
    assert defn.exit_params.trailing_stop_bps == 2500   # 寬移動停利 -25%
    assert defn.exit_params.ma_break_period == 60        # 長均線跌破才認趨勢真破
