"""risk_exit 引擎四條件、MANUAL/長期排除、SELL 訊號歸屬測試。"""
import pytest
from datetime import date
from src.contracts.models import MarketBar, ExitParams, TrendBreakoutParams
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.strategy.registry import StrategyDefinition
from src.strategy.risk_exit import RiskExitEngine


def make_bar(symbol, trade_date, close, open_p=None, low=None, volume=1000):
    open_p = open_p if open_p is not None else close
    low = low if low is not None else min(open_p, close)
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK",
        trade_date=trade_date,
        open=open_p, high=max(open_p, close), low=low, close=close,
        volume=volume, amount=1000,
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


def make_defn(strategy_id="trend_breakout", **exit_overrides):
    exit_kwargs = dict(
        fixed_stop_loss_bps=700,
        trailing_stop_bps=800,
        ma_break_period=5,
        ma_break_buffer_bps=0,
        ma_break_confirm_days=2,
        time_stop_days=20,
        time_stop_min_return_bps=500,
    )
    exit_kwargs.update(exit_overrides)
    return StrategyDefinition(
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        params=TrendBreakoutParams(),
        exit_params=ExitParams(**exit_kwargs),
        params_hash="sha256:test",
        order_budget_twd=20000
    )


@pytest.fixture
def setup(tmp_path):
    db_file = tmp_path / "test_risk_exit.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger("acc-1")
    calendar = ExchangeCalendarsTradingCalendar()
    yield conn, projection, calendar
    conn.close()


def _buy(projection, symbol, qty, price, strategy_id, filled_at, is_long_term=0):
    projection.apply_fill_transaction({
        "fill_id": f"f-{symbol}-{strategy_id}-{filled_at[:10]}",
        "account_id": "acc-1",
        "run_id": "run-1",
        "order_id": "ord-1",
        "execution_key": f"k-{symbol}-{strategy_id}-{filled_at}",
        "symbol": symbol,
        "side": "BUY",
        "quantity": qty,
        "price": price,
        "filled_at": filled_at,
        "strategy_id": strategy_id,
        "is_long_term": is_long_term
    })


def test_fixed_stop_exit(setup):
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-06-08T09:00:00+08:00")

    # 收盤 92.9 < 100 x 0.93 → 固定停損
    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 929000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    bundles = engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x")

    assert len(bundles) == 1
    b = bundles[0]
    assert b.strategy.strategy_id == "trend_breakout"
    # Exit bundle id is account-scoped so two accounts never collide on the
    # idempotent _save_bundle (each owns its own exit facts).
    assert b.bundle_id == "bundle-20260610-trend_breakout-acc-1-exit"
    assert len(b.signals) == 1
    sig = b.signals[0]
    assert sig.action == "SELL"
    assert sig.reason_code == "FIXED_STOP_EXIT"
    assert sig.strategy_id == "trend_breakout"  # 歸屬原持倉策略，非 'risk_exit'
    assert sig.signal_source == "RISK_EXIT"


def test_explain_exit_breakdown(setup):
    """explain_exit 回傳逐條明細，reason 與 generate_exit_bundles 一致（固定停損案例）。"""
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-06-08T09:00:00+08:00")
    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 929000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    pos = projection.get_strategy_positions("acc-1", include_long_term=True)[("trend_breakout", "2330")]

    detail = engine.explain_exit(date(2026, 6, 10), "acc-1", pos, make_defn().exit_params, pit)
    assert detail["evaluable"] is True
    assert detail["close"] == 92.9
    assert detail["wavg"] == 100.0
    assert detail["fixed_stop"]["hit"] is True
    assert detail["fixed_stop"]["level"] == 93.0  # 100 × (1 - 0.07)
    assert detail["reason"] == "FIXED_STOP_EXIT"
    # 與 _evaluate_position 委派一致
    assert engine._evaluate_position(date(2026, 6, 10), "acc-1", pos, make_defn().exit_params, pit) == "FIXED_STOP_EXIT"


def test_explain_exit_no_trigger(setup):
    """價格平穩 → 各條件皆未觸發，reason=None。"""
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-06-08T09:00:00+08:00")
    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 1010000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    pos = projection.get_strategy_positions("acc-1", include_long_term=True)[("trend_breakout", "2330")]

    detail = engine.explain_exit(date(2026, 6, 10), "acc-1", pos, make_defn().exit_params, pit)
    assert detail["reason"] is None
    assert detail["fixed_stop"]["hit"] is False
    assert detail["trailing"]["hit"] is False
    # 無 watermark → high 由 max(均價, 收盤) 保守初始化
    assert detail["trailing"]["high_from_watermark"] is False
    assert detail["trailing"]["high"] == 101.0


def test_trailing_stop_exit_uses_watermark(setup):
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-06-08T09:00:00+08:00")
    # 持有期間最高收盤 120
    projection.upsert_high_watermark("acc-1", "trend_breakout", "2330", "2026-06-09", 1200000)

    # 收盤 110：未觸固定停損（>93），但 110 <= 120 x 0.92 → 移動停利
    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 1100000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    bundles = engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x")

    assert len(bundles) == 1
    assert bundles[0].signals[0].reason_code == "TRAILING_STOP_EXIT"


def test_ma_break_exit_with_confirm_days(setup):
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 800000, "trend_breakout", "2026-06-08T09:00:00+08:00")

    closes = [1000000, 1000000, 1000000, 1000000, 800000, 790000]
    days = [date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    bars = [make_bar("2330", d, c) for d, c in zip(days, closes)]

    pit = MockPIT({"2330": bars}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    bundles = engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x")

    assert len(bundles) == 1
    assert bundles[0].signals[0].reason_code == "MA_BREAK_EXIT"


def test_ma_break_buffer_prevents_noise_exit(setup):
    """buffer 200bps：收盤僅小幅跌破均線時不出場（回檔策略防互咬）。"""
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 990000, "pullback_rebound", "2026-06-08T09:00:00+08:00")

    # 兩日收盤都僅低於 sma5 約 1%，buffer 2% 內 → 不觸發
    closes = [1000000, 1000000, 1000000, 1000000, 990000, 985000]
    days = [date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    bars = [make_bar("2330", d, c) for d, c in zip(days, closes)]

    defn = make_defn(strategy_id="pullback_rebound", ma_break_buffer_bps=200)
    pit = MockPIT({"2330": bars}, date(2026, 6, 10))
    engine = RiskExitEngine({"pullback_rebound": defn}, projection, calendar)
    bundles = engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x")
    assert bundles == []


def test_time_stop_exit(setup):
    conn, projection, calendar = setup
    # 持有自 4/1 起（> 20 個交易日），報酬 0 < +5% 門檻
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-04-01T09:00:00+08:00")

    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 1000000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    bundles = engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x")

    assert len(bundles) == 1
    assert bundles[0].signals[0].reason_code == "TIME_STOP_EXIT"


def test_time_stop_not_triggered_when_profitable(setup):
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "trend_breakout", "2026-04-01T09:00:00+08:00")
    projection.upsert_high_watermark("acc-1", "trend_breakout", "2330", "2026-06-09", 1100000)

    # +10% 報酬 → 時間停損不觸發；其他條件也不觸發
    pit = MockPIT({"2330": [make_bar("2330", date(2026, 6, 10), 1100000)]}, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    assert engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x") == []


def test_manual_and_long_term_positions_excluded(setup):
    """MANUAL 部位與長期持倉不受 risk_exit 監控（v3 決策）。"""
    conn, projection, calendar = setup
    _buy(projection, "2330", 100, 1000000, "MANUAL", "2026-06-08T09:00:00+08:00")
    _buy(projection, "2317", 100, 1000000, "MANUAL", "2026-06-08T09:00:00+08:00", is_long_term=1)

    # 大跌也不得產生 SELL
    pit = MockPIT({
        "2330": [make_bar("2330", date(2026, 6, 10), 500000)],
        "2317": [make_bar("2317", date(2026, 6, 10), 500000)],
    }, date(2026, 6, 10))
    engine = RiskExitEngine({"trend_breakout": make_defn()}, projection, calendar)
    assert engine.generate_exit_bundles(date(2026, 6, 10), "acc-1", pit, "run-x") == []
