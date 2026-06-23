"""P1-T3：四條 benchmark（0050 原始買進持有/同曝險/同波動目標、等權 universe），
用合成 equity_curve + first/last close 斷言（依計畫「驗證」一節：統計類指標用合成序列驗證）。"""
import statistics

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import BacktestRunner


@pytest.fixture
def runner(tmp_path):
    db_file = tmp_path / "test_benchmarks.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    yield r
    conn.close()


def test_benchmark_0050_buy_hold_from_first_to_last_known_close(runner):
    equity_curve = [
        {"equity": 100000, "position_value": 0, "benchmark_close": None},  # 0050 窗初尚未掛牌資料
        {"equity": 100000, "position_value": 0, "benchmark_close": 1000000},
        {"equity": 100000, "position_value": 0, "benchmark_close": 1100000},
    ]
    benchmarks = runner._calculate_benchmarks(equity_curve, {}, {})
    assert benchmarks["benchmark_0050_buy_hold"] == pytest.approx(0.1)


def test_benchmark_0050_buy_hold_none_when_no_benchmark_data(runner):
    equity_curve = [{"equity": 100000, "position_value": 0, "benchmark_close": None}]
    benchmarks = runner._calculate_benchmarks(equity_curve, {}, {})
    assert benchmarks["benchmark_0050_buy_hold"] is None
    assert benchmarks["benchmark_0050_same_exposure"] is None
    assert benchmarks["benchmark_0050_vol_matched"] is None


def test_benchmark_same_exposure_scales_by_average_position_ratio(runner):
    # 平均曝險＝(0.5+0.5)/2=0.5；0050 報酬 10% → 同曝險 benchmark = 5%
    equity_curve = [
        {"equity": 100000, "position_value": 50000, "benchmark_close": 1000000},
        {"equity": 100000, "position_value": 50000, "benchmark_close": 1100000},
    ]
    benchmarks = runner._calculate_benchmarks(equity_curve, {}, {})
    assert benchmarks["benchmark_0050_same_exposure"] == pytest.approx(0.05)


def test_benchmark_vol_matched_scales_0050_return_by_volatility_ratio(runner):
    strategy_equities = [100000, 110000, 99000, 108900]
    bench_closes = [1000000, 1010000, 1000000, 1010000]
    equity_curve = [
        {"equity": e, "position_value": 0, "benchmark_close": b}
        for e, b in zip(strategy_equities, bench_closes)
    ]
    benchmarks = runner._calculate_benchmarks(equity_curve, {}, {})

    strat_returns = [strategy_equities[i] / strategy_equities[i - 1] - 1.0 for i in range(1, 4)]
    bench_returns = [bench_closes[i] / bench_closes[i - 1] - 1.0 for i in range(1, 4)]
    expected_leverage = statistics.stdev(strat_returns) / statistics.stdev(bench_returns)
    raw_0050_return = bench_closes[-1] / bench_closes[0] - 1.0

    assert benchmarks["benchmark_0050_vol_matched"] == pytest.approx(expected_leverage * raw_0050_return)


def test_benchmark_equal_weight_universe_averages_per_symbol_buy_hold_return(runner):
    equity_curve = [{"equity": 100000, "position_value": 0, "benchmark_close": None}]
    first_close = {"2330": 1000000, "2317": 500000}
    last_close = {"2330": 1100000, "2317": 550000}  # 兩檔都 +10%

    benchmarks = runner._calculate_benchmarks(equity_curve, first_close, last_close)
    assert benchmarks["benchmark_equal_weight_universe"] == pytest.approx(0.1)


def test_benchmark_equal_weight_universe_skips_symbols_missing_either_end(runner):
    equity_curve = [{"equity": 100000, "position_value": 0, "benchmark_close": None}]
    # 3231 整窗無資料（只在 first_close 或 last_close 其中一邊出現屬不一致狀態，這裡模擬完全缺資料：兩邊都沒有該檔）
    first_close = {"2330": 1000000}
    last_close = {"2330": 1100000}

    benchmarks = runner._calculate_benchmarks(equity_curve, first_close, last_close)
    assert benchmarks["benchmark_equal_weight_universe"] == pytest.approx(0.1)
