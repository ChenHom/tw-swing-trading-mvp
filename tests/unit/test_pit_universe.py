"""PIT 流動性 universe（R-T4b Track 2）合成測試：

- build_liquidity_policy 的排序 / 月再平衡 / PIT 無前視 / known_at
- UniversePolicy.constituents_as_of 的 effective 視窗
- 策略 universe provider 的鴨子包裝
- fingerprint 的 policy 驅動 snapshot id（非 diagnostic → 逃出 INVALID 閘門）
"""
from datetime import date

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.market_data.liquidity_universe import build_liquidity_policy
from src.market_data.universe_policy import UniversePolicy
from src.strategy.universe import coerce_universe, FixedUniverseProvider, PolicyUniverseProvider
from src.application.runners.fingerprint import compute_fingerprint


def _bar(conn, symbol, trade_date, amount):
    conn.execute(
        """
        INSERT INTO market_bars
            (symbol, exchange, instrument_type, trade_date, open, high, low, close,
             volume, amount, source, source_timezone, is_complete, source_fetched_at,
             raw_payload_checksum, price_basis, adjustment_factor, created_at, updated_at)
        VALUES (?, 'TSE', 'STOCK', ?, 1000, 1000, 1000, 1000, ?, ?, 'test', 'Asia/Taipei', 1,
                '2020-01-01T00:00:00+08:00', 'ck', 'raw', 1.0, datetime('now'), datetime('now'))
        """,
        (symbol, trade_date, amount, amount),
    )


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "research.db")
    init_db(db)
    c = get_db_connection(db)
    # 三個月窗：Dec2019 暖身 / Jan2020 / Feb2020。A,B 高量穩定；C,D 低量；LATE 僅 Feb 有資料且爆量。
    schedule = {
        "A": {"2019-12-27": 1000, "2019-12-30": 1000, "2019-12-31": 1000,
              "2020-01-02": 1000, "2020-01-07": 1000, "2020-01-08": 1000, "2020-02-03": 1000},
        "B": {"2019-12-27": 900, "2019-12-30": 900, "2019-12-31": 900,
              "2020-01-02": 900, "2020-01-07": 900, "2020-01-08": 900, "2020-02-03": 900},
        "C": {"2019-12-27": 100, "2019-12-30": 100, "2019-12-31": 100,
              "2020-01-02": 100, "2020-01-07": 100, "2020-01-08": 100, "2020-02-03": 100},
        "D": {"2019-12-27": 50, "2019-12-30": 50, "2019-12-31": 50,
              "2020-01-02": 50, "2020-01-07": 50, "2020-01-08": 50, "2020-02-03": 50},
        "LATE": {"2020-02-03": 5000},  # 只在 Feb 出現 → Jan 前不得被選（無前視）
    }
    for sym, days in schedule.items():
        for d, amt in days.items():
            _bar(c, sym, d, amt)
    c.commit()
    return c


def test_ranking_and_monthly_rebalance(conn):
    build_liquidity_policy(conn, "liquidity-top2-test", date(2019, 12, 1), date(2020, 2, 28),
                           top_n=2, lookback_sessions=3, min_sessions=1)
    pol = UniversePolicy(conn)
    # Jan：A,B 量最高 → top2；LATE 此時無資料，不得入選（PIT 無前視）
    assert pol.constituents_as_of("liquidity-top2-test", date(2020, 1, 15)) == ["A", "B"]
    # Feb：LATE 爆量(5000) + A(1000) → top2；C/D 被擠掉，月再平衡生效
    assert pol.constituents_as_of("liquidity-top2-test", date(2020, 2, 10)) == ["A", "LATE"]


def test_pit_known_at_is_rebalance_date(conn):
    build_liquidity_policy(conn, "liquidity-top2-test", date(2019, 12, 1), date(2020, 2, 28),
                           top_n=2, lookback_sessions=3, min_sessions=1)
    # Jan 段成分的 known_at 必為 Jan 再平衡日(2020-01-02)，不可後見之明
    rows = conn.execute(
        "SELECT DISTINCT known_at FROM universe_policy WHERE policy_version=? AND effective_from='2020-01-02'",
        ("liquidity-top2-test",),
    ).fetchall()
    assert [r["known_at"] for r in rows] == ["2020-01-02"]


def test_late_listing_absent_before_it_exists(conn):
    build_liquidity_policy(conn, "liquidity-top2-test", date(2019, 12, 1), date(2020, 2, 28),
                           top_n=2, lookback_sessions=3, min_sessions=1)
    pol = UniversePolicy(conn)
    assert "LATE" not in pol.constituents_as_of("liquidity-top2-test", date(2020, 1, 31))
    assert "LATE" in pol.constituents_as_of("liquidity-top2-test", date(2020, 2, 10))


def test_diagnostic_policy_version_rejected(conn):
    with pytest.raises(ValueError):
        build_liquidity_policy(conn, "x-diagnostic", date(2020, 1, 1), date(2020, 2, 1))


def test_coerce_universe_wraps_list_and_passes_provider():
    fixed = coerce_universe(["2330", "2317"])
    assert isinstance(fixed, FixedUniverseProvider)
    # 固定 provider 不論日期回同一批
    assert fixed.symbols_as_of(date(2020, 1, 1)) == ["2330", "2317"]
    assert fixed.symbols_as_of(date(2025, 9, 9)) == ["2330", "2317"]
    # 已是 provider → 原樣返回（不重複包裝）
    assert coerce_universe(fixed) is fixed


def test_policy_provider_delegates_to_constituents(conn):
    build_liquidity_policy(conn, "liquidity-top2-test", date(2019, 12, 1), date(2020, 2, 28),
                           top_n=2, lookback_sessions=3, min_sessions=1)
    prov = PolicyUniverseProvider(conn, "liquidity-top2-test")
    assert prov.symbols_as_of(date(2020, 1, 15)) == ["A", "B"]


def test_fingerprint_policy_driven_snapshot_id(conn):
    kwargs = dict(
        conn=conn, run_id="r1", strategy_version="1.0.0", params_hash="ph",
        universe_symbols=["A"], index_symbols=["TSE"],
        start_date=date(2020, 1, 1), end_date=date(2020, 2, 1),
        slippage_bps=10, initial_cash=300000, manifest_digest="md",
    )
    # 無 policy（固定清單）→ diagnostic → 必 INVALID
    assert compute_fingerprint(**kwargs)["universe_snapshot_id"].startswith("diagnostic:")
    # 真 PIT policy → 非 diagnostic → 放行進入正式裁決
    real = compute_fingerprint(**kwargs, universe_policy_version="liquidity-top150-v1")
    assert real["universe_snapshot_id"].startswith("liquidity-top150-v1:")
    assert not real["universe_snapshot_id"].startswith("diagnostic:")
    # policy 名含 diagnostic → 仍 diagnostic（保留閘門語意）
    assert compute_fingerprint(**kwargs, universe_policy_version="foo-diagnostic")[
        "universe_snapshot_id"].startswith("diagnostic:")
