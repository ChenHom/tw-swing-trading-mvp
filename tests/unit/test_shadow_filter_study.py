"""breakout_shadow_filter 訊號層級研究：出場判斷與 RiskExitEngine 同源、單筆模擬、統計判定。"""
import random
from datetime import date, timedelta

from src.application.research.shadow_filter_study import (
    TradeOutcome, exit_reason, simulate_candidate, summarize, InMemoryBars, enumerate_outcomes,
)
from src.contracts.models import ExitParams, MarketBar, TrendBreakoutParams
from src.strategy.risk_exit import RiskExitEngine
from src.portfolio.db import init_db, get_db_connection

EXIT = ExitParams(fixed_stop_loss_bps=700, trailing_stop_bps=800, ma_break_period=20, ma_break_buffer_bps=0,
                  ma_break_confirm_days=2, time_stop_days=20, time_stop_min_return_bps=500)


def bar(d, close, open_=None, high=None, low=None, volume=1000, symbol="2330", instrument="STOCK"):
    open_ = close if open_ is None else open_
    return MarketBar(symbol=symbol, exchange="TSE", instrument_type=instrument, trade_date=d, open=open_,
                     high=max(open_, close) if high is None else high,
                     low=min(open_, close) if low is None else low, close=close, volume=volume, amount=0,
                     source="test", source_fetched_at="now", raw_payload_checksum="chk")


def days(n, start=date(2026, 1, 5)):
    return [start + timedelta(days=i) for i in range(n)]


class FakeProjection:
    def __init__(self, high): self.high = high
    def get_position_high(self, account_id, strategy_id, symbol, since_date): return self.high


class FakeCalendar:
    def __init__(self, sessions): self.sessions = sessions
    def sessions_between(self, a, b): return [d for d in self.sessions if a <= d <= b]


class FakePIT:
    def __init__(self, bars): self.bars = bars
    def latest(self, symbol): return self.bars[-1]
    def history(self, symbol, limit): return self.bars[-limit:]


def test_exit_reason_matches_risk_exit_engine_on_random_paths():
    rng = random.Random(7)
    sessions = days(80)
    for _ in range(200):
        price = 1000000
        bars = []
        for d in sessions:
            price = max(10000, int(price * (1 + rng.gauss(0, 0.03))))
            bars.append(bar(d, price))
        entry = rng.randrange(0, 40)
        wavg = bars[entry].open
        for t in range(entry, len(bars)):
            high = max(b.close for b in bars[entry:t + 1])
            holding = t - entry
            mine = exit_reason(bars, t, wavg, high, holding, EXIT)
            engine = RiskExitEngine({}, FakeProjection(high), FakeCalendar(sessions))
            pos = {"symbol": "2330", "strategy_id": "x", "wavg_price": wavg,
                   "first_acquired_at": bars[entry].trade_date.isoformat()}
            theirs = engine.explain_exit(bars[t].trade_date, "acct", pos, EXIT, FakePIT(bars[:t + 1]))["reason"]
            assert mine == theirs


def test_fixed_stop_sells_next_open_with_odd_lot_costs():
    d = days(6)
    bars = [bar(d[0], 1000000), bar(d[1], 1000000, open_=1000000),   # 訊號日、進場日（開 100）
            bar(d[2], 920000),                                       # 收 92 ≤ 100.3×0.93 → 停損
            bar(d[3], 900000, open_=910000), bar(d[4], 900000), bar(d[5], 900000)]
    r = simulate_candidate(bars, 0, EXIT, 10, d, d[-1], 20000)
    assert r["status"] == "CLOSED" and r["exit_reason"] == "FIXED_STOP_EXIT"
    assert r["exit_date"] == d[3].isoformat()
    # 200 股零股：買 100×1.003=100.3 → 20,060 + 費 29；賣 91×0.997=90.727 → 18,145 − 費 26 − 稅 54
    assert abs(r["net_return"] - ((18145 - 26 - 54) / (20060 + 29) - 1)) < 1e-12


def test_limit_up_locked_entry_is_unfilled_and_open_trade_marked_at_end():
    d = days(4)
    locked = [bar(d[0], 1000000), bar(d[1], 1100000, open_=1100000, high=1100000, low=1100000)]
    assert simulate_candidate(locked, 0, EXIT, 10, d, d[-1], 20000)["status"] == "UNFILLED_ENTRY"
    calm = [bar(d[0], 1000000), bar(d[1], 1000000), bar(d[2], 1010000), bar(d[3], 1020000)]
    r = simulate_candidate(calm, 0, EXIT, 10, d, d[-1], 20000)
    assert r["status"] == "OPEN_AT_END" and r["exit_date"] == d[3].isoformat()
    expensive = [bar(d[0], 300000000), bar(d[1], 300000000)]  # 3 萬元一股 > 2 萬預算
    assert simulate_candidate(expensive, 0, EXIT, 10, d, d[-1], 20000)["status"] == "BUDGET_TOO_SMALL"


def _outcomes(kept_returns, removed_returns):
    out = []
    for i, r in enumerate(kept_returns):
        out.append(TradeOutcome("K", f"2020-01-{i % 28 + 1:02d}", False, False, "CLOSED", net_return=r))
    for i, r in enumerate(removed_returns):
        out.append(TradeOutcome("R", f"2020-02-{i % 28 + 1:02d}", True, False, "CLOSED", net_return=r))
    return out


def test_summarize_effective_filter_passes_all_checks():
    rng = random.Random(1)
    kept = [0.02 + rng.gauss(0, 0.03) for _ in range(300)]
    removed = [-0.03 + rng.gauss(0, 0.03) for _ in range(100)]
    s = summarize(_outcomes(kept, removed))
    assert s["verdict"] == "FILTER_EFFECTIVE", s["checks"]
    assert abs(s["rejection_rate"] - 0.25) < 1e-9
    assert summarize(_outcomes(kept, removed)) == s  # 固定 seed，可重現


def test_summarize_random_filter_is_no_increment():
    rng = random.Random(2)
    returns = [0.005 + rng.gauss(0, 0.05) for _ in range(400)]
    s = summarize(_outcomes(returns[:300], returns[300:]))
    assert s["verdict"] == "NO_INCREMENT"
    assert not s["checks"]["P2_placebo_p_lt_0_05"]


def test_summarize_rejection_rate_out_of_band_fails():
    rng = random.Random(3)
    kept = [0.02 + rng.gauss(0, 0.01) for _ in range(100)]
    removed = [-0.05 + rng.gauss(0, 0.01) for _ in range(300)]  # 剔除 75% > 60%
    s = summarize(_outcomes(kept, removed))
    assert not s["checks"]["P3_rejection_rate_5_to_60pct"]
    assert s["verdict"] == "NO_INCREMENT"


class ListUniverse:
    def __init__(self, symbols): self.symbols = symbols
    def symbols_as_of(self, d): return self.symbols


def test_enumerate_outcomes_end_to_end_on_sqlite(tmp_path):
    db = str(tmp_path / "r.db")
    init_db(db)
    conn = get_db_connection(db)
    d = days(20)
    rows = []
    for i, day in enumerate(d):
        rows.append(bar(day, 2000000 + i * 10000, symbol="TSE", instrument="INDEX"))
        if i < 15:
            rows.append(bar(day, 1000000, symbol="AAA"))
            rows.append(bar(day, 1000000, symbol="BBB"))
        elif i == 15:  # 兩檔同日帶量突破；BBB 長上影（U > L）、AAA 長下影
            rows.append(bar(day, 1100000, open_=1010000, high=1105000, low=900000, volume=9000, symbol="AAA"))
            rows.append(bar(day, 1100000, open_=1010000, high=1400000, low=1005000, volume=9000, symbol="BBB"))
        else:
            rows.append(bar(day, 1100000, symbol="AAA"))
            rows.append(bar(day, 1100000, symbol="BBB"))
    for b in rows:
        conn.execute(
            "INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume,"
            " amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, price_basis,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'raw',datetime('now'),datetime('now'))",
            (b.symbol, b.exchange, b.instrument_type, b.trade_date.isoformat(), b.open, b.high, b.low, b.close,
             b.volume, 0, "t", "Asia/Taipei", 1, "now", "chk"))
    conn.commit()
    store = InMemoryBars(conn, ["AAA", "BBB", "TSE"])
    params = TrendBreakoutParams(breakout_lookback_days=5, volume_avg_days=5, volume_multiple_pct=150,
                                 ma_trend_period=5, index_ma_period=5, order_budget_twd=20000)
    outcomes = enumerate_outcomes(store, ListUniverse(["AAA", "BBB"]), d, d, "TSE", params, EXIT, 7, 10, d[-1])
    assert {(o.symbol, o.signal_date, o.filtered) for o in outcomes} == {
        ("AAA", d[15].isoformat(), False), ("BBB", d[15].isoformat(), True)}
    assert all(o.status == "OPEN_AT_END" for o in outcomes)
