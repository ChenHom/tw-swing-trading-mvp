"""P1-T1：風險調整指標（CAGR/年化波動/Sharpe/Sortino/Calmar/Beta/Alpha）+ 三種回撤，
用合成 equity_curve 斷言（依計畫「驗證」一節：統計類指標用合成序列驗證，非端到端）。"""
import math
import statistics

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import BacktestRunner, TRADING_DAYS_PER_YEAR


@pytest.fixture
def runner(tmp_path):
    """只測 `_calculate_statistics`（純函式運算，只讀 self.db_conn 查 fifo_matches），
    跳過完整建構（manifest/calendar 等）以聚焦本次驗證範圍。"""
    db_file = tmp_path / "test_risk_metrics.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    yield r
    conn.close()


def test_close_to_close_and_intraday_bound_drawdowns_differ(runner):
    # equity 路徑：100000 → 105000 → 103000（close-to-close 回撤較小）
    # 但日內高低差更大，悲觀界線須明顯大於 close-to-close。
    equity_curve = [
        {"equity": 100000, "equity_high": 100000, "equity_low": 100000},
        {"equity": 105000, "equity_high": 108000, "equity_low": 102000},
        {"equity": 103000, "equity_high": 104000, "equity_low": 95000},
    ]
    stats = runner._calculate_statistics("acc-x", 100000, equity_curve)

    assert stats["close_to_close_maxdd"] == pytest.approx(2000 / 105000)
    assert stats["max_drawdown"] == stats["close_to_close_maxdd"]  # 舊欄名相容
    assert stats["worst_case_intraday_drawdown_bound"] == pytest.approx(13000 / 108000)
    assert stats["worst_case_intraday_drawdown_bound"] > stats["close_to_close_maxdd"]
    assert stats["timestamped_intraday_maxdd"] is None  # 無分鐘資料來源，不假裝精確


def test_cagr_sharpe_sortino_calmar_synthetic_series(runner):
    # 交替 +10%/-10%，4 個交易日報酬、5 個 equity 點；報酬均值為 0 → annualized_return=0
    # → Sharpe/Sortino 均為 0（非 inf，因波動 > 0）。
    equities = [100000, 110000, 99000, 108900, 98010]
    equity_curve = [{"equity": e} for e in equities]
    stats = runner._calculate_statistics("acc-x", 100000, equity_curve)

    n = len(equities)
    daily_returns = [equities[i] / equities[i - 1] - 1.0 for i in range(1, n)]
    expected_vol = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
    expected_cagr = (equities[-1] / equities[0]) ** (TRADING_DAYS_PER_YEAR / n) - 1.0

    assert stats["annualized_volatility"] == pytest.approx(expected_vol)
    assert stats["sharpe_ratio"] == pytest.approx(0.0)
    assert stats["sortino_ratio"] == pytest.approx(0.0)
    assert stats["cagr"] == pytest.approx(expected_cagr)
    assert stats["calmar_ratio"] == pytest.approx(expected_cagr / stats["close_to_close_maxdd"])


def test_sharpe_and_calmar_are_inf_when_returns_positive_with_zero_volatility(runner):
    # 每日固定 +10%（無波動）：annualized_volatility=0、annualized_return>0 → Sharpe=inf；
    # close_to_close_maxdd=0、cagr>0 → Calmar=inf（與 profit_factor 既有 inf 慣例一致）。
    equity_curve = [{"equity": e} for e in (100000, 110000, 121000, 133100)]
    stats = runner._calculate_statistics("acc-x", 100000, equity_curve)

    assert stats["annualized_volatility"] == pytest.approx(0.0)
    assert stats["sharpe_ratio"] == float("inf")
    assert stats["close_to_close_maxdd"] == 0.0
    assert stats["calmar_ratio"] == float("inf")


def test_beta_alpha_none_when_benchmark_data_unavailable(runner):
    equity_curve = [{"equity": e} for e in (100000, 105000, 103000)]  # 無 benchmark_close 欄
    stats = runner._calculate_statistics("acc-x", 100000, equity_curve)

    assert stats["beta_vs_benchmark"] is None
    assert stats["alpha_vs_benchmark"] is None


def test_beta_alpha_computed_from_synthetic_benchmark_series(runner):
    bench = [1000000, 1010000, 989800, 1019494, 1009299]
    strat_equity = [100000, 101000, 98000, 102000, 100500]
    equity_curve = [
        {"equity": e, "benchmark_close": b} for e, b in zip(strat_equity, bench)
    ]
    stats = runner._calculate_statistics("acc-x", 100000, equity_curve)

    strat_returns = [strat_equity[i] / strat_equity[i - 1] - 1.0 for i in range(1, len(strat_equity))]
    bench_returns = [bench[i] / bench[i - 1] - 1.0 for i in range(1, len(bench))]
    mean_s, mean_b = statistics.fmean(strat_returns), statistics.fmean(bench_returns)
    cov = statistics.fmean([(s - mean_s) * (b - mean_b) for s, b in zip(strat_returns, bench_returns)])
    var_b = statistics.pvariance(bench_returns)
    expected_beta = cov / var_b
    expected_alpha = (mean_s - expected_beta * mean_b) * TRADING_DAYS_PER_YEAR

    assert stats["beta_vs_benchmark"] == pytest.approx(expected_beta)
    assert stats["alpha_vs_benchmark"] == pytest.approx(expected_alpha)
