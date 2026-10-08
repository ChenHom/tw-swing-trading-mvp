"""PR-1: isolated read-only Tick collector tests; no Shioaji SDK import or live login."""
from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.market_data.intraday_tick import (
    RawTickStore, build_minute_bars, compress_raw, iter_raw, normalize_raw_tick,
    raw_event, replay_ticks, tick_path,
)
from src.market_data.intraday_collector import (
    ShioajiTickCollector, Subscription, build_subscriptions,
)


RECEIVED = datetime(2026, 10, 8, 1, 5, 7, tzinfo=timezone.utc)


def tick(*, code="2327", close="637.50", volume=2, total_volume=100, odd=False,
         timestamp="2026-10-08T09:05:07+08:00", simtrade=False, suspend=False):
    return SimpleNamespace(
        code=code, close=close, volume=volume, total_volume=total_volume,
        datetime=timestamp, intraday_odd=odd, simtrade=simtrade,
        suspend=suspend, tick_type=1,
    )


def record(**kwargs):
    return raw_event(
        "TSE", tick(**kwargs), session_id="fixed-session", seq=1,
        received_at=RECEIVED,
    )


def test_board_lot_is_shares_odd_lot_is_shares_and_price_is_scaled():
    board, reason = normalize_raw_tick(record())
    assert reason == ""
    assert board.symbol == "2327"
    assert board.lot_type == "BOARD"
    assert board.price_x10000 == 6375000
    assert board.trade_volume_shares == 2000
    assert board.cumulative_volume_shares == 100000
    assert board.event_time == "2026-10-08T09:05:07+08:00"
    assert board.received_at == "2026-10-08T01:05:07+00:00"
    odd, reason = normalize_raw_tick(record(odd=True, volume=27, total_volume=1002))
    assert reason == ""
    assert odd.lot_type == "ODD"
    assert odd.trade_volume_shares == 27
    assert odd.cumulative_volume_shares == 1002


@pytest.mark.parametrize("change,reason", [
    ({"simtrade": True}, "simtrade"),
    ({"suspend": True}, "suspend"),
    ({"close": "NaN"}, "needs_review"),
    ({"close": "-1"}, "needs_review"),
    ({"close": "1.12345"}, "needs_review"),
    ({"volume": -1}, "needs_review"),
    ({"timestamp": "2026-10-09T09:05:07+08:00"}, None),
])
def test_malformed_or_nontrade_events_never_become_real_ticks(change, reason):
    raw = record(**change)
    normalized, actual = normalize_raw_tick(raw)
    if reason is None:
        # A valid event with its correct trading date is permitted.
        assert normalized is not None
    else:
        assert normalized is None and actual == reason


def test_date_mismatch_and_invalid_received_timezone():
    raw = record()
    raw["trading_date"] = "2026-10-07"
    assert normalize_raw_tick(raw)[1] == "date_mismatch"
    raw = record()
    raw["received_at"] = "2026-10-08T01:05:07"
    assert normalize_raw_tick(raw)[1] == "received_at_missing_timezone"


def test_broken_timestamp_is_retained_in_unknown_audit_bucket(tmp_path):
    raw = record(timestamp="bogus-date", code="")
    assert raw["event_time"] is None
    path = RawTickStore(tmp_path).append(raw)
    assert path.name == "_invalid.jsonl"
    assert next(iter_raw(path))["payload"]["datetime"] == "bogus-date"
    ticks, rejected = replay_ticks(path)
    assert ticks == []
    assert rejected == {"symbol_invalid": 1}
    # When the code itself is valid, a corrupted timestamp gets its own review reason.
    good_code_bad_time = record(timestamp="bogus-date")
    separate_path = RawTickStore(tmp_path).append(good_code_bad_time)
    assert replay_ticks(separate_path)[1] == {"needs_review": 1}


def test_exact_timestamp_is_not_an_identity_and_replay_matches(tmp_path):
    store = RawTickStore(tmp_path)
    a, b = record(volume=1, total_volume=3), record(volume=2, total_volume=5)
    b["collection_seq"] = 2
    path = store.append(a)
    store.append(b)
    result, rejects = replay_ticks(path)
    assert rejects == {}
    assert [r.trade_volume_shares for r in result] == [1000, 2000]
    bars = build_minute_bars(result)
    assert len(bars) == 1
    assert bars[0]["volume_shares"] == 3000
    assert bars[0]["trade_count"] == 2


def test_missing_minutes_are_not_filled_and_lot_books_are_separate(tmp_path):
    store = RawTickStore(tmp_path)
    rows = [
        record(timestamp="2026-10-08T09:05:02+08:00", volume=2),
        record(timestamp="2026-10-08T09:07:02+08:00", volume=3),
    ]
    path = store.append(rows[0])
    store.append(rows[1])
    oddpath = store.append(record(odd=True, volume=13))
    bars = build_minute_bars(replay_ticks(path)[0] + replay_ticks(oddpath)[0])
    assert len(bars) == 3
    assert not any(bar["start_at"].startswith("2026-10-08T09:06") for bar in bars)
    assert {bar["lot_type"] for bar in bars} == {"BOARD", "ODD"}


def test_gzip_roundtrip_hash_and_never_overwrite(tmp_path):
    path = RawTickStore(tmp_path).append(record())
    before = path.read_bytes()
    summary = compress_raw(path)
    target = Path(summary["archive"])
    assert target.exists() and not path.exists()
    assert gzip.open(target, "rb").read() == before
    assert len(replay_ticks(target)[0]) == 1
    with pytest.raises(ValueError):
        compress_raw(target)


def test_scope_is_deterministic_audited_and_bounded():
    subs, audit = build_subscriptions(
        positions=["2327", "2330"], watchlist=["2327"], candidates=["2360"],
        max_symbols=3,
    )
    assert len(subs) == 6
    assert audit["2327"] == ["position", "watchlist"]
    assert subs[0] == Subscription("2327", "BOARD")
    assert subs[1] == Subscription("2327", "ODD")
    with pytest.raises(ValueError, match="exceeds"):
        build_subscriptions(positions=["2327", "2330"], max_symbols=1)
    with pytest.raises(ValueError, match="invalid symbol"):
        Subscription("../../secrets")


class FakeAPI:
    def __init__(self, *, fail_on=None):
        self.contracts = self
        self.calls = []
        self.callback = None
        self.fail_on = fail_on

    def get(self, symbol):
        return f"contract:{symbol}"

    def on_tick_stk_v1(self):
        def register(fn):
            self.callback = fn
        return register

    def subscribe(self, contract, *, quote_type, intraday_odd):
        if contract == self.fail_on:
            raise RuntimeError("test subscription failure")
        self.calls.append(("subscribe", contract, quote_type, intraday_odd))

    def unsubscribe(self, contract, *, quote_type, intraday_odd):
        self.calls.append(("unsubscribe", contract, quote_type, intraday_odd))


def test_fake_sdk_subscribe_callback_raw_and_shutdown(tmp_path):
    api = FakeAPI()
    received = []
    collector = ShioajiTickCollector(
        api, subscriptions=[Subscription("2327", "BOARD")], quote_type="Tick",
        store=RawTickStore(tmp_path), sink=received.append, session_id="test",
    )
    collector.start()
    api.callback("TSE", tick())
    collector.queue.join()
    result = collector.stop()
    assert len(received) == 1
    assert received[0].trade_volume_shares == 2000
    assert result["state"] == "CLOSED"
    assert result["counters"]["raw_written"] == 1
    assert [call[0] for call in api.calls] == ["subscribe", "unsubscribe"]
    rawpath = tick_path(tmp_path, "2026-10-08", "BOARD", "2327")
    assert rawpath.exists()


def test_failed_subscribe_rolls_back_already_subscribed(tmp_path):
    api = FakeAPI(fail_on="contract:2330")
    c = ShioajiTickCollector(
        api, subscriptions=[Subscription("2327"), Subscription("2330")],
        quote_type="Tick", store=RawTickStore(tmp_path),
    )
    with pytest.raises(RuntimeError, match="failure"):
        c.start()
    assert c.state == "FAILED"
    assert api.calls[0][0] == "subscribe"
    assert api.calls[-1][0] == "unsubscribe"


def test_outside_scope_has_no_raw_write_and_sink_failure_is_degraded(tmp_path):
    c = ShioajiTickCollector(
        FakeAPI(), subscriptions=[Subscription("2327")], quote_type="Tick",
        store=RawTickStore(tmp_path), sink=lambda t: (_ for _ in ()).throw(ValueError()),
    )
    assert c.process("TSE", tick(code="2330"), received_at=RECEIVED) is None
    assert c.counters["outside_scope"] == 1
    assert c.process("TSE", tick(), received_at=RECEIVED) is not None
    assert c.health()["state"] == "DEGRADED"
    assert c.counters["sink_error"] == 1


def test_disk_watermark_refuses_data_and_health_reflects_loss(tmp_path):
    c = ShioajiTickCollector(
        FakeAPI(), subscriptions=[Subscription("2327")],
        quote_type="Tick", store=RawTickStore(tmp_path, stop_at_disk_pct=0.00001),
    )
    assert c.process("TSE", tick(), received_at=RECEIVED) is None
    assert c.health()["state"] == "DEGRADED"
    assert c.counters["raw_write_error"] == 1


def test_queue_overflow_is_visible(tmp_path):
    c = ShioajiTickCollector(
        FakeAPI(), subscriptions=[Subscription("2327")],
        quote_type="Tick", store=RawTickStore(tmp_path), queue_capacity=1,
    )
    c.state = "HEALTHY"
    c._on_tick("TSE", tick())
    c._on_tick("TSE", tick())
    assert c.health()["state"] == "DEGRADED"
    assert c.counters["queue_dropped"] == 1
