"""Quote transport gaps and raw lifecycle tests; no real broker session."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.market_data.intraday_collector import ShioajiTickCollector, Subscription
from src.market_data.intraday_storage import (
    ArchiveBusyError, append_raw, archive_closed_sessions, compress_raw,
    disk_status, retention_audit,
)
from src.market_data.intraday_tick import RawTickStore, raw_event

AT = datetime(2026, 10, 8, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class FakeQuoteAPI:
    def __init__(self):
        self.contracts = self
        self.quote = SimpleNamespace(set_event_callback=self.set_event_callback)
        self.log = []
        self.event_handler = None

    def get(self, symbol):
        return f"contract:{symbol}"

    def set_event_callback(self, fn):
        self.event_handler = fn

    def on_tick_stk_v1(self):
        return lambda fn: None

    def subscribe(self, contract, *, quote_type, intraday_odd):
        self.log.append(("subscribe", contract, quote_type, intraday_odd))

    def unsubscribe(self, contract, *, quote_type, intraday_odd):
        self.log.append(("unsubscribe", contract, quote_type, intraday_odd))


def make_collector(tmp_path, api):
    return ShioajiTickCollector(
        api, subscriptions=[Subscription("2327")], quote_type="Tick",
        store=RawTickStore(tmp_path), session_id="test-session",
    )


def raw_tick():
    return raw_event("TSE", SimpleNamespace(
        code="2327", datetime=AT, close=638, volume=1, total_volume=1,
        intraday_odd=False, simtrade=False, suspend=False, tick_type=1,
        date=AT.date(), time=AT.time(),
    ), session_id="s", seq=1, received_at=AT)


def test_transport_disconnect_and_reconnect_are_audited_and_gap_never_disappears(tmp_path):
    api = FakeQuoteAPI()
    c = make_collector(tmp_path, api)
    c.start()
    assert c.health()["quote_event_callback_registered"]
    api.event_handler(200, 0, "connected", "session up")
    c.queue.join()
    assert c.health()["connection_verified"]
    api.event_handler(200, 1, "down", "session down")
    api.event_handler(200, 12, "reconnecting", "reconnecting")
    api.event_handler(200, 13, "reconnected", "reconnected")
    c.queue.join()
    h = c.health()
    assert h["transport_state"] == "UP"
    assert h["gap_count"] == 1
    assert h["gap_unresolved"] is True
    assert h["state"] == "DEGRADED"
    assert h["resubscribe_required"] is False
    assert sum(x[0] == "subscribe" for x in api.log) == 2
    files = list((tmp_path/"shioaji"/"session-events").rglob("*.jsonl"))
    assert len(files) == 1
    assert [json.loads(x)["event_code"] for x in files[0].read_text().splitlines()] == [0, 1, 12, 13]
    c.stop()


def test_maintenance_rejects_running_collector_even_when_tick_file_not_locked(tmp_path):
    api = FakeQuoteAPI()
    c = make_collector(tmp_path, api)
    RawTickStore(tmp_path).append(raw_tick())
    path = tmp_path/"shioaji"/"ticks"/"2026-10-08"/"BOARD"/"2327.jsonl"
    c.start()
    with pytest.raises(ArchiveBusyError):
        archive_closed_sessions(tmp_path, before_date=date(2026, 10, 9),
                                is_trading_day=lambda d: d.weekday() < 5)
    assert path.exists()
    c.stop()
    result = archive_closed_sessions(tmp_path, before_date=date(2026, 10, 9),
                                     is_trading_day=lambda d: d.weekday() < 5)
    assert len(result["archived"]) == 1
    assert path.with_suffix(".jsonl.gz").exists()
    assert not path.exists()
    with pytest.raises(FileExistsError):
        RawTickStore(tmp_path).append(raw_tick())


def test_gzip_has_sha_manifest_and_append_does_not_restart_archived_day(tmp_path):
    path = RawTickStore(tmp_path).append(raw_tick())
    raw = path.read_bytes()
    outcome = compress_raw(path)
    archive = Path(outcome["archive"])
    manifest = json.loads(Path(str(archive) + ".manifest.json").read_text())
    assert manifest["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert outcome["raw_sha256"] == manifest["raw_sha256"]
    assert outcome["lines"] == manifest["lines"] == 1
    assert not path.exists()


def test_bad_or_truncated_raw_never_deleted(tmp_path):
    raw = tmp_path/"2026-10-08"/"BOARD"/"bad.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b'{"payload":1}')  # no final newline
    with pytest.raises(ValueError):
        compress_raw(raw)
    assert raw.read_bytes() == b'{"payload":1}'
    assert not raw.with_suffix(".jsonl.gz").exists()
    assert not raw.with_suffix(".jsonl.gz.tmp").exists()


def test_crash_temp_requires_review_and_keeps_original(tmp_path):
    path = RawTickStore(tmp_path).append(raw_tick())
    target = Path(str(path)+".gz.tmp")
    target.write_text("incomplete")
    with pytest.raises(FileExistsError):
        compress_raw(path)
    assert path.exists() and target.exists()


def test_180_session_retention_audit_never_automatically_deletes(tmp_path):
    today = date(2026, 10, 8)
    older = tmp_path/"shioaji"/"ticks"/"2025-01-01"/"BOARD"
    older.mkdir(parents=True)
    archive = older/"2327.jsonl.gz"
    archive.write_bytes(b"fixture")
    result = retention_audit(tmp_path, as_of=today,
                             is_trading_day=lambda d: d.weekday()<5)
    assert result["minimum_trading_sessions"] == 180
    assert str(archive) in result["old_archives_review_only"]
    assert result["automatic_deletion"] is False
    assert archive.read_bytes() == b"fixture"


def test_calendar_failure_refuses_retention_classification(tmp_path):
    with pytest.raises(ValueError, match="calendar unavailable"):
        retention_audit(tmp_path, as_of=date(2026, 10, 8),
                        is_trading_day=lambda d: False)


def test_raw_archiving_strict_before_date_preserves_today(tmp_path):
    path = RawTickStore(tmp_path).append(raw_tick())
    out = archive_closed_sessions(tmp_path, before_date=date(2026, 10, 8),
                                  is_trading_day=lambda d: True)
    assert out["archived"] == [] and path.exists()
