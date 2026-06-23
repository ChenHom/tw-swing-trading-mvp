"""P2-T3：四種資料分離 + 家族級 Final lockbox（開封記錄、只開一次）。"""
import sqlite3
from datetime import date

import pytest

from src.portfolio.db import init_db, get_db_connection
from src.application.runners.lockbox import (
    set_data_partition_policy, get_data_partition_policy, classify_window,
    record_lockbox_opening, has_lockbox_been_opened,
)

POLICY = {
    "training_end_date": date(2022, 12, 31),
    "walkforward_end_date": date(2024, 6, 30),
    "lockbox_end_date": date(2025, 6, 30),
}


def _conn(tmp_path):
    db_file = tmp_path / "test_lockbox.db"
    init_db(str(db_file))
    return get_db_connection(str(db_file))


def test_get_policy_none_when_not_set(tmp_path):
    conn = _conn(tmp_path)
    assert get_data_partition_policy(conn, "trend_breakout") is None
    conn.close()


def test_set_and_get_policy_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    set_data_partition_policy(conn, "trend_breakout", **POLICY)
    assert get_data_partition_policy(conn, "trend_breakout") == POLICY
    conn.close()


def test_policy_cannot_be_redrawn_after_first_write(tmp_path):
    conn = _conn(tmp_path)
    set_data_partition_policy(conn, "trend_breakout", **POLICY)
    set_data_partition_policy(
        conn, "trend_breakout", training_end_date=date(2020, 1, 1),
        walkforward_end_date=date(2020, 6, 1), lockbox_end_date=date(2020, 12, 1),
    )
    assert get_data_partition_policy(conn, "trend_breakout") == POLICY  # 第一次寫入的切界不可被事後改寫
    conn.close()


def test_classify_window_entirely_within_training():
    result = classify_window(POLICY, date(2021, 1, 1), date(2022, 1, 1))
    assert result == {"partitions_touched": ["training"], "touches_lockbox": False, "touches_live_forward": False}


def test_classify_window_spanning_training_and_walkforward():
    result = classify_window(POLICY, date(2022, 1, 1), date(2023, 1, 1))
    assert result["partitions_touched"] == ["training", "walkforward"]
    assert result["touches_lockbox"] is False


def test_classify_window_touching_lockbox():
    result = classify_window(POLICY, date(2024, 1, 1), date(2025, 1, 1))
    assert result["touches_lockbox"] is True
    assert result["touches_live_forward"] is False


def test_classify_window_touching_live_forward():
    result = classify_window(POLICY, date(2025, 1, 1), date(2026, 1, 1))
    assert result["touches_lockbox"] is True
    assert result["touches_live_forward"] is True


def test_lockbox_not_opened_by_default(tmp_path):
    conn = _conn(tmp_path)
    assert has_lockbox_been_opened(conn, "trend_breakout") is False
    conn.close()


def test_record_lockbox_opening_then_reflected_in_has_opened(tmp_path):
    conn = _conn(tmp_path)
    record_lockbox_opening(conn, "trend_breakout", opened_by_strategy_version="1.2.0", opened_by_run_id="bt-99")
    assert has_lockbox_been_opened(conn, "trend_breakout") is True
    conn.close()


def test_opening_lockbox_twice_for_same_family_raises(tmp_path):
    conn = _conn(tmp_path)
    record_lockbox_opening(conn, "trend_breakout", opened_by_strategy_version="1.2.0", opened_by_run_id="bt-99")
    with pytest.raises(sqlite3.IntegrityError):
        record_lockbox_opening(conn, "trend_breakout", opened_by_strategy_version="1.3.0", opened_by_run_id="bt-100")
    conn.close()


def test_different_families_can_each_open_their_own_lockbox_independently(tmp_path):
    conn = _conn(tmp_path)
    record_lockbox_opening(conn, "trend_breakout", opened_by_strategy_version="1.2.0", opened_by_run_id="bt-99")
    record_lockbox_opening(conn, "pullback_rebound", opened_by_strategy_version="1.0.0", opened_by_run_id="bt-100")
    assert has_lockbox_been_opened(conn, "trend_breakout") is True
    assert has_lockbox_been_opened(conn, "pullback_rebound") is True
    conn.close()
