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
    # Only the synthetic verified fixture may expose prices. The actual 2327
    # collector has no validated heartbeat/session/completeness and stays hidden.
    v = {
        "state": "HEALTHY", "last_heartbeat_at": NOW.isoformat(),
        "trading_session_verified": True, "session_id": "snapshot-session",
        "transport_verification_status": "EVENT_VERIFIED",
        "connection_verified": True, "gap_unresolved": False,
        "data_quality_status": "VERIFIED",
        "market_completeness_status": "VERIFIED",
    }
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


@pytest.mark.parametrize("missing_evidence", [
    {"state": "UNVERIFIED"},
    {"state": "UNKNOWN"},
    {"transport_verification_status": "UNVERIFIED"},
    {"transport_verification_status": "GAP_UNRESOLVED"},
    {"connection_verified": False},
    {"gap_unresolved": True},
    {"data_quality_status": "UNVERIFIED"},
    {"market_completeness_status": "UNVERIFIED"},
    {"trading_session_verified": False},
    {"session_id": None},
    {"last_heartbeat_at": None},
])
def test_fake_fresh_heartbeat_cannot_bypass_evidence_gates(tmp_path, missing_evidence):
    p = publish(tmp_path, health_state=health(**missing_evidence))
    result = load_snapshot(p, enabled=True, now=NOW)
    assert result["status"] != "OBSERVING"
    assert result["symbols"][0]["tick"] is None
    assert result["symbols"][0]["book"] is None


def test_real_shioaji_132_capture_health_never_appears_live(tmp_path):
    """The actual host smoke produced data, not a verified continuous market feed."""
    real = {
        "state": "CLOSED", "collection_status": "RAW_CAPTURED",
        "lifecycle_status": "STOPPED",
        "transport_verification_status": "UNVERIFIED",
        "connection_verified": False, "gap_unresolved": False,
        "data_quality_status": "UNVERIFIED",
        "market_completeness_status": "UNVERIFIED",
        "last_heartbeat_at": None, "trading_session_verified": False,
        "session_id": "actual-session",
    }
    path = publish(tmp_path, health_state=real)
    result = load_snapshot(path, enabled=True, now=NOW)
    assert result["status"] == "CLOSED"
    assert all(r["tick"] is None and r["book"] is None for r in result["symbols"])


def test_fresh_quote_from_different_session_or_market_not_exposed(tmp_path):
    import json
    path = publish(tmp_path)
    source = json.loads(path.read_text())
    source["symbols"][0]["tick"]["collector_session_id"] = "other-session"
    source["symbols"][0]["book"]["exchange"] = "OTC"
    write_snapshot(path, source)
    result = load_snapshot(path, enabled=True, now=NOW)
    assert result["status"] == "OBSERVING"  # verified synthetic session
    assert result["symbols"][0]["tick"] is None
    assert result["symbols"][0]["book"] is None


def test_crossed_best_bid_ask_is_never_displayed(tmp_path):
    import json
    path = publish(tmp_path)
    source = json.loads(path.read_text())
    source["symbols"][0]["book"]["ask_prices_x10000"][0] = 6000000
    write_snapshot(path, source)
    result = load_snapshot(path, enabled=True, now=NOW)
    assert result["symbols"][0]["book"] is None
    assert result["symbols"][0]["tick"] is not None


def test_offline_raw_to_publisher_to_web_fails_closed_with_actual_collector_health(tmp_path):
    """End to end: PR1/2 raw journals -> PR4 CLI -> snapshot reader, no broker."""
    import json
    from types import SimpleNamespace
    from src.market_data.intraday_tick import RawTickStore, raw_event
    from src.market_data.intraday_book import RawBookStore, raw_book_event
    from scripts.build_intraday_dashboard import run

    ts = datetime(2026, 10, 12, 10, 51, tzinfo=TZ)
    tick_sdk = SimpleNamespace(
        code="2327", datetime=ts, date=ts.date(), time=ts.time(),
        close=635, volume=1, total_volume=1, tick_type=1,
        intraday_odd=False, simtrade=False, suspend=False,
    )
    book_sdk = SimpleNamespace(
        code="2327", datetime=ts, date=ts.date(), time=ts.time(),
        bid_price=[634, 633, 632, 631, 630],
        ask_price=[635, 636, 637, 638, 639],
        bid_volume=[3, 2, 1, 1, 1], ask_volume=[2, 1, 1, 1, 1],
        diff_bid_vol=[0] * 5, diff_ask_vol=[0] * 5,
        intraday_odd=False, simtrade=False, suspend=False,
    )
    root = tmp_path / "raw"
    RawTickStore(root).append(raw_event(
        "TSE", tick_sdk, session_id="host-unverified", seq=1, received_at=ts,
    ))
    RawBookStore(root).append(raw_book_event(
        "TSE", book_sdk, session_id="host-unverified", seq=2, received_at=ts,
    ))
    date_folder = "2026-10-12"
    raw_tick_path = root / "shioaji" / "ticks" / date_folder / "BOARD" / "2327.jsonl"
    raw_book_path = root / "shioaji" / "bidask" / date_folder / "BOARD" / "2327.jsonl"

    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps({
        "state": "CLOSED", "collection_status": "RAW_CAPTURED",
        "lifecycle_status": "STOPPED", "session_id": "host-unverified",
        "transport_verification_status": "UNVERIFIED",
        "connection_verified": False, "gap_unresolved": False,
        "data_quality_status": "UNVERIFIED",
        "market_completeness_status": "UNVERIFIED",
        "last_heartbeat_at": None, "trading_session_verified": False,
    }), encoding="utf-8")
    output = tmp_path / "dashboard.json"
    result = run(ticks_file=raw_tick_path, books_file=raw_book_path,
                 health_file=health_path, output=output)
    assert result["mode"] == "OFFLINE_PUBLICATION_ONLY"
    assert result["symbols"] == 1
    displayed = load_snapshot(output, enabled=True, now=datetime.now(timezone.utc))
    assert displayed["status"] == "CLOSED"
    assert displayed["not_trade_signal"] is True
    assert displayed["symbols"][0]["tick"] is None
    assert displayed["symbols"][0]["book"] is None
    assert displayed["symbols"][0]["observations"] == []


def test_opt_in_historical_reference_is_not_a_live_quote(tmp_path):
    path = publish(tmp_path, health_state=health(
        state="CLOSED", transport_verification_status="UNVERIFIED",
        connection_verified=False, data_quality_status="UNVERIFIED",
        market_completeness_status="UNVERIFIED",
        last_heartbeat_at=None, trading_session_verified=False,
    ))
    hidden = load_snapshot(path, enabled=True, now=NOW)
    assert hidden["status"] == "CLOSED"
    assert hidden["symbols"][0]["reference_tick"] is None
    visible = load_snapshot(path, enabled=True, include_history=True,
                            now=NOW + timedelta(days=2))
    assert visible["status"] == "CLOSED"
    assert visible["display_mode"] == "HISTORICAL_REFERENCE"
    row = visible["symbols"][0]
    assert row["tick"] is None and row["book"] is None
    assert row["reference_tick"]["price_x10000"] == 6375000
    assert row["reference_book"]["bid_volumes_shares"][0] == 3000
    assert row["reference_label"] == "HISTORICAL_UNVERIFIED_NOT_LIVE"
    assert visible["not_trade_signal"] is True


def test_snapshot_publisher_throttles_atomic_writes_and_final_closed_state(tmp_path):
    import json
    from src.application.services.intraday_snapshot_publisher import IntradaySnapshotPublisher

    path = tmp_path / "projected.json"
    pub = IntradaySnapshotPublisher(path, interval_seconds=5)
    pub.on_tick(tick())
    pub.on_book(book())
    assert pub.publish(health(), at=NOW)
    before = path.read_bytes()
    assert pub.publish(health(state="CLOSED"), at=NOW, force=False) is False
    assert path.read_bytes() == before

    assert pub.publish(health(state="CLOSED"), at=NOW, force=True)
    doc = json.loads(path.read_text())
    assert doc["collector_health"]["state"] == "CLOSED"
    assert doc["capture_verified_for_live_trading"] is False
    assert doc["snapshot_mode"] == "READ_ONLY_CAPTURE"
    assert not path.with_suffix(".json.tmp").exists()
    screen = load_snapshot(path, enabled=True, now=NOW, include_history=True)
    assert screen["status"] == "CLOSED"
    assert screen["symbols"][0]["tick"] is None
    assert screen["symbols"][0]["reference_book"] is not None


def test_snapshot_publisher_reconnect_epoch_never_reuses_previous_session(tmp_path):
    import json
    from src.application.services.intraday_snapshot_publisher import IntradaySnapshotPublisher

    path = tmp_path / "projected.json"
    pub = IntradaySnapshotPublisher(path, interval_seconds=1)
    pub.on_tick(tick())
    pub.on_book(book())
    pub.publish(health(session_id="new-epoch", state="DEGRADED"), force=True, at=NOW)
    document = json.loads(path.read_text())
    assert document["symbols"] == []
    assert document["collector_health"]["session_id"] == "new-epoch"

    from dataclasses import replace
    pub.on_tick(replace(tick(), collector_session_id="new-epoch", collection_seq=22))
    pub.publish(health(session_id="new-epoch", state="DEGRADED"), force=True, at=NOW)
    document = json.loads(path.read_text())
    assert len(document["symbols"]) == 1
    assert document["symbols"][0]["book"] is None
    assert document["symbols"][0]["tick"]["collector_session_id"] == "new-epoch"


def test_historical_reference_rejects_future_wrong_session_and_crossed_book(tmp_path):
    import json
    path = publish(tmp_path, health_state=health(state="CLOSED"))
    data = json.loads(path.read_text())
    data["symbols"][0]["tick"]["collector_session_id"] = "wrong-session"
    data["symbols"][0]["book"]["ask_prices_x10000"][0] = 6000000
    write_snapshot(path, data)
    row = load_snapshot(path, enabled=True, now=NOW,
                        include_history=True)["symbols"][0]
    assert row["reference_tick"] is None
    assert row["reference_book"] is None
    data["symbols"][0]["tick"]["collector_session_id"] = "snapshot-session"
    data["symbols"][0]["tick"]["received_at"] = (NOW + timedelta(minutes=1)).isoformat()
    write_snapshot(path, data)
    row = load_snapshot(path, enabled=True, now=NOW,
                        include_history=True)["symbols"][0]
    assert row["reference_tick"] is None


def test_snapshot_publisher_rejects_invalid_interval_and_non_json_output(tmp_path):
    from src.application.services.intraday_snapshot_publisher import IntradaySnapshotPublisher
    with pytest.raises(ValueError):
        IntradaySnapshotPublisher(tmp_path / "state.json", interval_seconds=0)
    with pytest.raises(ValueError):
        IntradaySnapshotPublisher(tmp_path / "state.jsonl", interval_seconds=5)


def test_offline_builder_combines_four_board_odd_raw_files(tmp_path):
    import json
    from scripts.build_intraday_dashboard import run
    from tests.unit.test_intraday_p0_p1 import four_fixture_files

    root = tmp_path / "raw"
    four_fixture_files(root)
    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps({
        "state": "CLOSED", "session_id": "smoke-s1",
        "transport_verification_status": "UNVERIFIED",
        "data_quality_status": "UNVERIFIED",
        "market_completeness_status": "UNVERIFIED",
    }), encoding="utf-8")
    date_part = "2026-10-12"
    ticks = [root / "shioaji" / "ticks" / date_part / lot / "2327.jsonl"
             for lot in ("BOARD", "ODD")]
    books = [root / "shioaji" / "bidask" / date_part / lot / "2327.jsonl"
             for lot in ("BOARD", "ODD")]
    dest = tmp_path / "preview.json"
    result = run(ticks_file=ticks, books_file=books,
                 health_file=health_path, output=dest)
    assert result["symbols"] == 2
    rows = load_snapshot(dest, enabled=True, include_history=True,
                         now=datetime(2026, 10, 13, 10, 0, tzinfo=TZ))
    assert rows["status"] == "CLOSED"
    assert {r["lot_type"] for r in rows["symbols"]} == {"BOARD", "ODD"}
    assert all(r["tick"] is None and r["book"] is None for r in rows["symbols"])
    assert all(r["reference_tick"] and r["reference_book"] for r in rows["symbols"])
