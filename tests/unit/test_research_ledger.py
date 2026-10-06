"""P2-T2：Research Ledger（append-only，餵 DSR num_trials）。"""
from src.portfolio.db import init_db, get_db_connection
from src.application.runners.research_ledger import record_research_attempt, count_research_trials


def _conn(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    init_db(str(db_file))
    return get_db_connection(str(db_file))


def test_count_research_trials_zero_when_no_attempts_recorded(tmp_path):
    conn = _conn(tmp_path)
    assert count_research_trials(conn, "trend_breakout") == 0
    conn.close()


def test_count_research_trials_counts_distinct_version_params_combos(tmp_path):
    conn = _conn(tmp_path)
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashA", run_id="bt-1")
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashB", run_id="bt-2")
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.1.0", params_hash="hashA", run_id="bt-3")
    assert count_research_trials(conn, "s1") == 3
    conn.close()


def test_rerunning_same_version_params_does_not_inflate_distinct_trial_count(tmp_path):
    conn = _conn(tmp_path)
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashA", run_id="bt-1")
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashA", run_id="bt-2")
    assert count_research_trials(conn, "s1") == 1
    conn.close()


def test_failed_attempts_are_not_deleted_and_still_count(tmp_path):
    conn = _conn(tmp_path)
    record_research_attempt(
        conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashA", run_id="bt-1",
        status="REJECTED", notes="未過 regime gate",
    )
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM research_ledger WHERE strategy_id = 's1'")
    assert cursor.fetchone()["n"] == 1  # 失敗版本仍在帳上，非刪除
    assert count_research_trials(conn, "s1") == 1
    conn.close()


def test_different_strategies_counted_independently(tmp_path):
    conn = _conn(tmp_path)
    record_research_attempt(conn, strategy_id="s1", strategy_version="1.0.0", params_hash="hashA", run_id="bt-1")
    record_research_attempt(conn, strategy_id="s2", strategy_version="1.0.0", params_hash="hashA", run_id="bt-2")
    assert count_research_trials(conn, "s1") == 1
    assert count_research_trials(conn, "s2") == 1
    conn.close()


def test_family_member_trials_count_toward_parent_family(tmp_path):
    """breakout_shadow_filter 屬 trend_breakout 家族：換 strategy_id 不得重置試驗次數。"""
    conn = _conn(tmp_path)
    record_research_attempt(conn, strategy_id="trend_breakout", strategy_version="1.0.0", params_hash="h1", run_id="r1")
    record_research_attempt(conn, strategy_id="breakout_shadow_filter", strategy_version="1.0.0", params_hash="h2", run_id="r2")
    record_research_attempt(conn, strategy_id="pullback_rebound", strategy_version="1.0.0", params_hash="h3", run_id="r3")
    assert count_research_trials(conn, "breakout_shadow_filter") == 2
    assert count_research_trials(conn, "trend_breakout") == 2
    assert count_research_trials(conn, "pullback_rebound") == 1
