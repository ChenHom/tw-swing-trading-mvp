"""UniversePolicy 單元測試（P0-T5）：D 日有效成分股查詢正確、diagnostic-only 標記不可繞過。"""
import pytest
from datetime import date

from src.portfolio.db import init_db, get_db_connection
from src.market_data.universe_policy import UniversePolicy


@pytest.fixture
def policy(tmp_path):
    db_path = str(tmp_path / "research.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    return UniversePolicy(conn), conn


def test_seed_diagnostic_policy_requires_diagnostic_in_name(policy):
    up, conn = policy
    with pytest.raises(ValueError, match="diagnostic"):
        up.seed_diagnostic_policy("v1-prod", ["2330"], date(2026, 1, 1))
    conn.close()


def test_seed_and_query_diagnostic_policy(policy):
    up, conn = policy
    up.seed_diagnostic_policy("universe-2026-06-diagnostic", ["2330", "2317"], date(2026, 1, 1))

    constituents = up.constituents_as_of("universe-2026-06-diagnostic", date(2026, 6, 1))
    assert constituents == ["2317", "2330"]
    assert up.is_diagnostic_only("universe-2026-06-diagnostic") is True
    conn.close()


def test_constituents_respects_effective_window(policy):
    up, conn = policy
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO universe_policy
        (policy_version, symbol, effective_from, effective_to, known_at, created_at)
        VALUES ('v-pit', '6805', '2024-01-02', NULL, '2024-01-02', datetime('now'))
        """
    )
    conn.commit()

    # 2024 上市前不應出現（effective_from 之前）
    assert "6805" not in up.constituents_as_of("v-pit", date(2022, 1, 1))
    # 上市後應出現
    assert "6805" in up.constituents_as_of("v-pit", date(2024, 6, 1))
    conn.close()


def test_constituents_respects_known_at_pit_gate(policy):
    """known_at > as_of：不得在 D 日被讀到（防後見之明）。"""
    up, conn = policy
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO universe_policy
        (policy_version, symbol, effective_from, effective_to, known_at, created_at)
        VALUES ('v-pit', '2330', '2020-01-01', NULL, '2026-06-01', datetime('now'))
        """
    )
    conn.commit()

    assert "2330" not in up.constituents_as_of("v-pit", date(2022, 1, 1))  # known_at 在未來
    assert "2330" in up.constituents_as_of("v-pit", date(2026, 6, 15))
    conn.close()


def test_exclusion_via_effective_to(policy):
    up, conn = policy
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO universe_policy
        (policy_version, symbol, effective_from, effective_to, known_at,
         exclusion_reason, created_at)
        VALUES ('v-pit', '1234', '2010-01-01', '2022-12-31', '2010-01-01', 'DELISTED', datetime('now'))
        """
    )
    conn.commit()

    assert "1234" in up.constituents_as_of("v-pit", date(2022, 6, 1))
    assert "1234" not in up.constituents_as_of("v-pit", date(2023, 1, 1))
    conn.close()
