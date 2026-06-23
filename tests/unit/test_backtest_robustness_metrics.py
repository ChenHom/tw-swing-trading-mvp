"""P1-T5：多重檢定/穩健（DSR/block bootstrap/進場日聚類有效N/去最佳交易或月/Herfindahl）。
用合成 fifo_matches/fills + equity_curve 直接寫入 db 斷言（沿用 __new__ 隔離測試手法）。"""
import math
import statistics

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.backtest import (
    BacktestRunner, TRADING_DAYS_PER_YEAR, BOOTSTRAP_RANDOM_SEED, BOOTSTRAP_ITERATIONS,
    _deflated_sharpe_ratio, _profit_herfindahl,
)


@pytest.fixture
def runner(tmp_path):
    db_file = tmp_path / "test_robustness.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    r = BacktestRunner.__new__(BacktestRunner)
    r.db_conn = conn
    yield r
    conn.close()


def _insert_fill(conn, fill_id, account_id, filled_at):
    conn.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side,
                            quantity, price, filled_at, created_at)
        VALUES (?, ?, 'run-x', 'order-x', ?, '2330', 'BUY', 1000, 100, ?, ?)
        """,
        (fill_id, account_id, fill_id, filled_at, filled_at)
    )


def _insert_match(conn, match_id, account_id, buy_fill_id, matched_at, realized_pnl):
    conn.execute(
        """
        INSERT INTO fifo_matches (match_id, account_id, symbol, buy_fill_id, sell_fill_id, quantity,
                                   buy_price, sell_price, matched_at, realized_pnl, created_at, strategy_id)
        VALUES (?, ?, '2330', ?, 's', 1000, 100, 100, ?, ?, ?, '')
        """,
        (match_id, account_id, buy_fill_id, matched_at, realized_pnl, matched_at)
    )


def test_effective_sample_size_counts_distinct_entry_dates_not_raw_trades(runner):
    conn = runner.db_conn
    # 3 筆交易，但只有 2 個不同進場日 → 同日視為相關，有效樣本數=2（非 3）
    _insert_fill(conn, "b1", "acc-x", "2026-06-01T09:00:00")
    _insert_fill(conn, "b2", "acc-x", "2026-06-01T09:30:00")
    _insert_fill(conn, "b3", "acc-x", "2026-06-02T09:00:00")
    _insert_match(conn, "m1", "acc-x", "b1", "2026-06-03T09:00:00", 100)
    _insert_match(conn, "m2", "acc-x", "b2", "2026-06-03T09:00:00", 200)
    _insert_match(conn, "m3", "acc-x", "b3", "2026-06-04T09:00:00", -50)
    conn.commit()

    robustness = runner._calculate_robustness_stats("acc-x", [{"equity": 100000}])
    assert robustness["trade_count_raw"] == 3
    assert robustness["effective_sample_size"] == 2


def test_profit_herfindahl_concentrated_when_one_trade_dominates(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2026-06-01T09:00:00")
    _insert_fill(conn, "b2", "acc-x", "2026-06-02T09:00:00")
    _insert_match(conn, "m1", "acc-x", "b1", "2026-06-03T09:00:00", 9000)
    _insert_match(conn, "m2", "acc-x", "b2", "2026-06-04T09:00:00", 1000)
    conn.commit()

    robustness = runner._calculate_robustness_stats("acc-x", [{"equity": 100000}])
    expected_hhi = (9000 / 10000) ** 2 + (1000 / 10000) ** 2
    assert robustness["profit_herfindahl_concentration"] == pytest.approx(expected_hhi)


def test_pnl_excluding_best_5_trades_and_best_month(runner):
    conn = runner.db_conn
    # 5 筆 6 月賺 100 各、1 筆 7 月賺 1000（最佳月）
    for i in range(5):
        _insert_fill(conn, f"b{i}", "acc-x", f"2026-06-0{i + 1}T09:00:00")
        _insert_match(conn, f"m{i}", "acc-x", f"b{i}", f"2026-06-0{i + 1}T09:00:00", 100)
    _insert_fill(conn, "b5", "acc-x", "2026-07-01T09:00:00")
    _insert_match(conn, "m5", "acc-x", "b5", "2026-07-01T09:00:00", 1000)
    conn.commit()

    robustness = runner._calculate_robustness_stats("acc-x", [{"equity": 100000}])
    # 總 pnl = 500 + 1000 = 1500；去掉最佳 5 筆（1000 + 4*100）剩 1 筆 100
    assert robustness["pnl_excluding_best_5_trades"] == pytest.approx(100)
    # 去掉最佳月（7月=1000）剩 6 月共 500
    assert robustness["pnl_excluding_best_month"] == pytest.approx(500)


def test_deflated_sharpe_ratio_high_for_stable_positive_returns_low_for_zero_mean(runner):
    stable_returns = [0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.008, 0.011, 0.01, 0.012]
    zero_mean_returns = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01]

    dsr_stable = _deflated_sharpe_ratio(stable_returns, num_trials=1)
    dsr_zero = _deflated_sharpe_ratio(zero_mean_returns, num_trials=1)

    assert dsr_stable > 0.9
    assert dsr_zero == pytest.approx(0.5, abs=0.05)  # SR_hat≈0 → Φ(0)=0.5


def test_block_bootstrap_ci_brackets_known_constant_return(runner):
    # 每日固定 +1% → 年化報酬點估計與 CI 應緊貼同一值（無真實波動）
    equity_curve = [{"equity": 100000 * (1.01 ** i)} for i in range(30)]
    robustness = runner._calculate_robustness_stats("acc-x", equity_curve)

    expected_annualized = 0.01 * TRADING_DAYS_PER_YEAR
    assert robustness["annualized_return_bootstrap_ci_lower"] == pytest.approx(expected_annualized)
    assert robustness["annualized_return_bootstrap_ci_upper"] == pytest.approx(expected_annualized)


def test_robustness_stats_deterministic_across_repeated_calls(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2026-06-01T09:00:00")
    _insert_match(conn, "m1", "acc-x", "b1", "2026-06-03T09:00:00", 100)
    conn.commit()
    equity_curve = [{"equity": e} for e in (100000, 101000, 99000, 102000, 100500)]

    first = runner._calculate_robustness_stats("acc-x", equity_curve)
    second = runner._calculate_robustness_stats("acc-x", equity_curve)
    assert first == second  # 固定種子（BOOTSTRAP_RANDOM_SEED）保證可重現


def test_expectancy_bootstrap_ci_lower_none_with_fewer_than_two_trades(runner):
    conn = runner.db_conn
    _insert_fill(conn, "b1", "acc-x", "2026-06-01T09:00:00")
    _insert_match(conn, "m1", "acc-x", "b1", "2026-06-03T09:00:00", 100)
    conn.commit()

    robustness = runner._calculate_robustness_stats("acc-x", [{"equity": 100000}])
    assert robustness["expectancy_bootstrap_ci_lower"] is None


def test_num_trials_param_passed_through_to_dsr_and_recorded(runner):
    # P2-T2：num_trials 來自 Research Ledger，非寫死 1——更高 num_trials 應拉低 DSR（更保守）。
    stable_returns = [0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.008, 0.011, 0.01, 0.012]
    equity_curve = [{"equity": 100000.0}]
    for r in stable_returns:
        equity_curve.append({"equity": equity_curve[-1]["equity"] * (1 + r)})

    one_trial = runner._calculate_robustness_stats("acc-x", equity_curve, num_trials=1)
    many_trials = runner._calculate_robustness_stats("acc-x", equity_curve, num_trials=20)

    assert one_trial["num_trials_assumed"] == 1
    assert many_trials["num_trials_assumed"] == 20
    assert many_trials["deflated_sharpe_ratio"] < one_trial["deflated_sharpe_ratio"]
