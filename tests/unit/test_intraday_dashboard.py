"""PR-4: local read-only feed snapshots, no accounts, credentials or SDK."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.services.intraday_dashboard import (
    find_symbol, load_snapshot, make_snapshot, write_snapshot,
)
from src.market_data.intraday_book import MarketBook
from src.market_data.intraday_observations import ObservationEvent
from src.market_data.intraday_tick import MarketTick

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 9, 15, tzinfo=TZ)


def book(odd=False):
    return MarketBook(
        1, "2327", "TSE", "ODD" if odd else "BOARD", NOW.isoformat(), NOW.isoformat(),
        "snapshot-session", 2,
        (6370000, 6360000, 6350000, None, None),
        (3, 2, 1, None, None) if odd else (3000, 2000, 1000, None, None),
        (6380000, 6390000, 6400000, None, None),
        (2, 2, 1, None, None) if odd else (2000, 2000, 1000, None, None),
    )


def tick(odd=False):
    return MarketTick(
        1, "2327", "TSE", "ODD" if odd else "BOARD", NOW.isoformat(), NOW.isoformat(),
        6375000, 27 if odd else 2000, 100 if odd else 100000, 1, "snapshot-session", 1,
    )


def event():
    return ObservationEvent(
        "e1", "intraday-observation-v1", "plan", "2327", "TSE", "BOARD",
        "SUPPORT_TESTED", "OBSERVED", NOW.isoformat(), NOW.isoformat(),
        "snapshot-session", 1, (), {"price_x10000": 6375000},
        data_health="HEALTHY", plan_digest="predeclared",
    )


def health(**updates):
    v = {"state": "HEALTHY", "last_heartbeat_at": NOW.isoformat(),
         "trading_session_verified": True, "session_id": "snapshot-session"}
    v.update(updates)
    return v


def publish(tmp_path, *, ticks=None, books=None, health_state=None, at=NOW, evts=None):
    snap = make_snapshot(
        ticks=ticks if ticks is not None else [tick()],
        books=books if books is not None else [book()],
        observations=evts if evts is not None else [event()],
        collector_health=health_state if health_state is not None else health(),
        generated_at=at.isoformat(),
    )
    path = tmp_path / "dashboard.json"
    write_snapshot(path, snap)
    return path


def test_disabled_never_reads_snapshot_even_when_file_exists(tmp_path):
    p = publish(tmp_path)
    r = load_snapshot(p, enabled=False, now=NOW)
    assert r["status"] == "DISABLED" and r["symbols"] == []


def test_missing_and_corrupt_snapshot_fail_closed(tmp_path):
    path = tmp_path / "missing.json"
    assert load_snapshot(path, enabled=True, now=NOW)["status"] == "NO_DATA"
    path.write_text("{broken", encoding="utf-8")
    assert load_snapshot(path, enabled=True, now=NOW)["status"] == "NO_DATA"


def test_trusted_session_renders_board_lot_and_not_trade_signal(tmp_path):
    p = publish(tmp_path)
    r = load_snapshot(p, enabled=True, now=NOW)
    assert r["status"] == "OBSERVING"
    assert r["not_trade_signal"] is True
    row = find_symbol(r, "2327", "BOARD")
    assert row["tick"]["price_x10000"] == 6375000
    assert row["book"]["bid_volumes_shares"][0] == 3000
    assert len(row["observations"]) == 1
    assert find_symbol(r, "2327", "ODD") is None


def test_unknown_heartbeat_never_displays_old_prices(tmp_path):
    p = publish(tmp_path, health_state=health(last_heartbeat_at=None))
    r = load_snapshot(p, enabled=True, now=NOW)
    assert r["status"] == "UNVERIFIED"
    assert r["symbols"][0]["tick"] is None and r["symbols"][0]["book"] is None
    assert r["symbols"][0]["observations"][0]["kind"] == "SUPPORT_TESTED"


@pytest.mark.parametrize("state,expected", [
    ("CLOSED", "CLOSED"), ("DEGRADED", "DEGRADED"), ("FAILED", "DEGRADED")
])
def test_stopped_or_failed_collector_hides_prices(tmp_path, state, expected):
    p = publish(tmp_path, health_state=health(state=state))
    r = load_snapshot(p, enabled=True, now=NOW)
    assert r["status"] == expected
    assert all(x["tick"] is None for x in r["symbols"])


def test_stale_snapshot_and_stale_quote_hides_independently(tmp_path):
    old = NOW - timedelta(minutes=10)
    p = publish(tmp_path, at=old)
    r = load_snapshot(p, enabled=True, now=NOW)
    assert r["status"] == "STALE"
    assert r["symbols"][0]["tick"] is None
    p2 = publish(tmp_path, ticks=[tick()], books=[book()], at=NOW)
    r2 = load_snapshot(p2, enabled=True, now=NOW + timedelta(seconds=31))
    assert r2["status"] == "STALE"


def test_board_and_odd_held_separately_and_correct_shares(tmp_path):
    p = publish(tmp_path, ticks=[tick(), tick(True)], books=[book(), book(True)])
    r = load_snapshot(p, enabled=True, now=NOW)
    assert len(r["symbols"]) == 2
    assert find_symbol(r, "2327", "ODD")["book"]["bid_volumes_shares"][0] == 3
    assert find_symbol(r, "2327", "BOARD")["book"]["bid_volumes_shares"][0] == 3000


def test_nontrading_time_not_marked_observing(tmp_path):
    p = publish(tmp_path)
    saturday = datetime(2026, 10, 10, 9, 15, tzinfo=TZ)
    r = load_snapshot(p, enabled=True, now=saturday)
    assert r["status"] != "OBSERVING"
    assert r["market_session"] == "MARKET_CLOSED"


def test_unexpected_future_generation_rejected(tmp_path):
    p = publish(tmp_path, at=NOW + timedelta(seconds=30))
    assert load_snapshot(p, enabled=True, now=NOW)["status"] == "NO_DATA"


def test_wrong_market_lot_and_invalid_symbols_not_exposed(tmp_path):
    p = publish(tmp_path)
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    data["symbols"].append(dict(data["symbols"][0], symbol="../etc/passwd"))
    data["symbols"].append(dict(data["symbols"][0], lot_type="INVALID"))
    write_snapshot(p, data)
    r = load_snapshot(p, enabled=True, now=NOW)
    assert len(r["symbols"]) == 1
    assert find_symbol(r, "../etc/passwd", "BOARD") is None


def test_write_snapshot_is_atomic_and_utf8(tmp_path):
    p = publish(tmp_path)
    assert p.exists()
    assert not p.with_suffix(".json.tmp").exists()
    assert load_snapshot(p, enabled=True, now=NOW)["schema_version"] == 1


def test_untrusted_snapshot_prices_and_arrays_fail_closed(tmp_path):
    import json
    p = publish(tmp_path)
    data = json.loads(p.read_text())
    data["symbols"][0]["tick"]["price_x10000"] = "not-an-int"
    data["symbols"][0]["book"]["ask_prices_x10000"] = ["broken"]
    write_snapshot(p, data)
    status = load_snapshot(p, enabled=True, now=NOW)
    assert status["status"] == "OBSERVING"
    assert status["symbols"][0]["tick"] is None
    assert status["symbols"][0]["book"] is None


def test_future_received_tick_is_never_displayed_as_live(tmp_path):
    import json
    p = publish(tmp_path)
    data = json.loads(p.read_text())
    data["symbols"][0]["tick"]["received_at"] = (NOW+timedelta(seconds=10)).isoformat()
    write_snapshot(p, data)
    status = load_snapshot(p, enabled=True, now=NOW)
    assert status["symbols"][0]["tick"] is None
