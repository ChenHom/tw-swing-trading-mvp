"""P0 raw quality / health and P1 broker lifecycle regression, all offline."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.market_data.intraday_audit import audit_intraday_raw
from src.market_data.intraday_book import RawBookStore, raw_book_event
from src.market_data.intraday_collector import ShioajiTickCollector, Subscription
from src.market_data.intraday_storage import compress_raw
from src.market_data.intraday_tick import RawTickStore, raw_event

AT = datetime(2026, 10, 12, 10, 51, 30, tzinfo=timezone(timedelta(hours=8)))


def raw_tick(*, odd=False, seq=1):
    item = SimpleNamespace(code="2327", datetime=AT, date=AT.date(), time=AT.time(),
                           close=638.5, volume=2, total_volume=12,
                           intraday_odd=odd, simtrade=False, suspend=False, tick_type=1)
    return raw_event("TSE", item, session_id="smoke-s1", seq=seq, received_at=AT)


def raw_book(*, odd=False, seq=2):
    item = SimpleNamespace(
        code="2327", datetime=AT, date=AT.date(), time=AT.time(),
        bid_price=[638, 637, 636, 635, 634], bid_volume=[3, 2, 1, 2, 1],
        ask_price=[639, 640, 641, 642, 643], ask_volume=[2, 2, 1, 1, 2],
        diff_bid_vol=[1, 0, 0, 0, 0], diff_ask_vol=[0, 0, 0, 0, 0],
        intraday_odd=odd, simtrade=False, suspend=False,
    )
    return raw_book_event("TSE", item, session_id="smoke-s1", seq=seq, received_at=AT)


def four_fixture_files(tmp_path):
    for odd in (False, True):
        RawTickStore(tmp_path).append(raw_tick(odd=odd))
        RawBookStore(tmp_path).append(raw_book(odd=odd))
    return tmp_path


def audit(tmp_path, **kwargs):
    return audit_intraday_raw(tmp_path, symbol="2327", trading_date="2026-10-12", **kwargs)


def test_p0_all_four_raw_files_are_checked_independently_and_in_shares(tmp_path):
    four_fixture_files(tmp_path)
    result = audit(tmp_path, expected={
        "ticks_BOARD": 1, "ticks_ODD": 1,
        "bidask_BOARD": 1, "bidask_ODD": 1,
    })
    assert result["status"] == "PASS_RAW_ONLY"
    assert result["market_completeness"] == "UNVERIFIED"
    assert result["total_accepted"] == result["total_raw_lines"] == 4
    rows = result["streams"]
    assert rows["ticks_BOARD"]["sample_volume_shares"] == 2000
    assert rows["ticks_ODD"]["sample_volume_shares"] == 2
    assert rows["bidask_BOARD"]["sample_volume_shares"] == 3000
    assert rows["bidask_ODD"]["sample_volume_shares"] == 3
    assert all(v["sha256"] and not v["errors"] for v in rows.values())
    assert result["no_broker_login"] and result["read_only"]


def test_p0_missing_or_wrong_expected_counts_cannot_be_called_pass(tmp_path):
    four_fixture_files(tmp_path)
    (tmp_path/"shioaji"/"ticks"/"2026-10-12"/"ODD"/"2327.jsonl").unlink()
    assert audit(tmp_path)["status"] == "INCOMPLETE"
    four_fixture_files(tmp_path)
    result = audit(tmp_path, expected={
        "ticks_BOARD": 9, "ticks_ODD": 6,
        "bidask_BOARD": 60, "bidask_ODD": 6,
    })
    assert result["status"] == "FAIL"
    assert "expected_9_observed_1" in result["streams"]["ticks_BOARD"]["errors"]


def test_p0_wrong_identity_and_broken_trailing_raw_are_fail_closed(tmp_path):
    four_fixture_files(tmp_path)
    path = tmp_path/"shioaji"/"ticks"/"2026-10-12"/"BOARD"/"2327.jsonl"
    line = json.loads(path.read_text())
    line["payload"]["code"] = "2330"
    path.write_text(json.dumps(line) + "\n")
    assert "line_1_envelope_mismatch" in audit(tmp_path)["streams"]["ticks_BOARD"]["errors"]
    path.write_bytes(b'{"bad":1}')  # partial JSONL tail without final newline
    result = audit(tmp_path)
    assert result["status"] == "FAIL"
    assert any("line_1_truncated" in e for e in result["streams"]["ticks_BOARD"]["errors"])


def test_p0_rejected_raw_preserved_but_requires_review(tmp_path):
    four_fixture_files(tmp_path)
    path = tmp_path/"shioaji"/"bidask"/"2026-10-12"/"ODD"/"2327.jsonl"
    line = json.loads(path.read_text())
    line["payload"]["simtrade"] = True
    path.write_text(json.dumps(line) + "\n")
    result = audit(tmp_path)
    assert result["status"] == "REVIEW"
    assert result["streams"]["bidask_ODD"]["rejected"]["simtrade"] == 1
    assert result["total_raw_lines"] == 4
    assert result["total_accepted"] == 3


def test_p0_rejects_reused_sequence_and_duplicate_archive(tmp_path):
    four_fixture_files(tmp_path)
    path = tmp_path/"shioaji"/"ticks"/"2026-10-12"/"BOARD"/"2327.jsonl"
    with path.open("ab") as output:
        output.write(path.read_bytes())
    assert "line_2_sequence_not_increasing" in audit(tmp_path)["streams"]["ticks_BOARD"]["errors"]
    compress_raw(path)
    assert audit(tmp_path)["status"] == "FAIL"  # sequence still invalid
    path.write_text("{}\n")  # both raw and gz exist; never choose one silently
    assert "conflicting_raw_and_archive" in audit(tmp_path)["streams"]["ticks_BOARD"]["errors"]


def test_p0_cli_fails_on_missing_stream_and_creates_offline_report(tmp_path, capsys):
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "src" / "cli" / "intraday.py"
    spec = importlib.util.spec_from_file_location("p0_test_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    four_fixture_files(tmp_path)
    output = tmp_path/"artifacts"/"quality.json"
    args = SimpleNamespace(
        cache_dir=str(tmp_path), symbol="2327", date="2026-10-12",
        expected_tick_board=1, expected_tick_odd=1,
        expected_bidask_board=1, expected_bidask_odd=1,
        output=str(output),
    )
    cli.cmd_intraday_audit(args)
    assert json.loads(output.read_text())["status"] == "PASS_RAW_ONLY"
    (tmp_path/"shioaji"/"bidask"/"2026-10-12"/"ODD"/"2327.jsonl").unlink()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_intraday_audit(args)
    assert exc.value.code == 1
    assert json.loads(output.read_text())["status"] == "INCOMPLETE"


class SDK:
    def __init__(self):
        self.contracts = self
        self.events = None
        self.tick_callback = None
        self.calls = []
        self.fail_subscribes = False

        self.quote = SimpleNamespace(set_event_callback=self.register)

    def register(self, fn):
        self.events = fn

    def get(self, symbol):
        return symbol

    def on_tick_stk_v1(self):
        return lambda fn: setattr(self, "tick_callback", fn)

    def subscribe(self, contract, *, quote_type, intraday_odd):
        if self.fail_subscribes:
            raise RuntimeError("simulated SDK quote subscription failure")
        self.calls.append(("subscribe", contract, quote_type, intraday_odd))

    def unsubscribe(self, contract, *, quote_type, intraday_odd):
        self.calls.append(("unsubscribe", contract, quote_type, intraday_odd))


def collector(tmp_path):
    api = SDK()
    c = ShioajiTickCollector(
        api, subscriptions=[Subscription("2327")],
        quote_type="Tick", store=RawTickStore(tmp_path),
        session_id="sdk-p1",
    )
    return c, api


def test_p0_health_expresses_capture_without_inventing_transport_verification(tmp_path):
    c, api = collector(tmp_path)
    c.start()
    assert c.health()["collection_status"] == "NO_RAW_OBSERVED"
    assert c.health()["transport_verification_status"] == "UNVERIFIED"
    assert c.health()["lifecycle_status"] == "RUNNING"
    c.process("TSE", SimpleNamespace(
        code="2327", datetime=AT, date=AT.date(), time=AT.time(),
        close=638.5, volume=2, total_volume=12, tick_type=1,
        intraday_odd=False, simtrade=False, suspend=False,
    ), received_at=AT)
    h = c.health()
    assert h["collection_status"] == "RAW_CAPTURED"
    assert h["transport_verification_status"] == "UNVERIFIED"
    assert h["data_quality_status"] == "UNVERIFIED"
    assert h["market_completeness_status"] == "UNVERIFIED"
    c.stop()
    assert c.health()["lifecycle_status"] == "STOPPED"


def test_p1_two_disconnect_epochs_are_distinct_and_gap_is_not_cleared(tmp_path):
    c, api = collector(tmp_path)
    c.start()
    for index in (1, 2):
        api.events(200, 1, "", "")
        api.events(200, 13, "", "")
        c.queue.join()
        h = c.health()
        assert h["gap_count"] == index
        assert h["transport_verification_status"] == "GAP_UNRESOLVED"
        assert c.session_id == f"sdk-p1-e{index}"
        assert h["state"] == "DEGRADED"
    assert sum(1 for x in api.calls if x[0] == "subscribe") == 3
    c.stop()


def test_p1_exhausted_reconnect_fails_closed_and_releases_lease_on_stop(tmp_path):
    c, api = collector(tmp_path)
    c.start()
    api.fail_subscribes = True
    api.events(200, 1, "", "")
    api.events(200, 13, "", "")
    c.queue.join()
    h = c.health()
    assert h["state"] == "FAILED"
    assert h["collection_status"] == "LOSS_DETECTED"
    assert h["counters"]["resubscribe_failed"] == 1
    assert h["counters"]["resubscribe_error"] == 3
    assert c.stop_requested.is_set()
    assert any(x[0] == "unsubscribe" for x in api.calls)
    c.stop()
    # Lease is available again: worker exited and no leaked raw owner.
    c2, _ = collector(tmp_path)
    c2.start()
    c2.stop()


def test_p1_unexpected_worker_exception_unsubscribes_and_stops(tmp_path):
    c, api = collector(tmp_path)
    c.start()
    def boom(*args, **kwargs):
        raise RuntimeError("worker boom")
    c.process = boom
    api.tick_callback("TSE", SimpleNamespace(code="2327"))
    c.queue.join()
    assert c.health()["state"] == "FAILED"
    assert c.stop_requested.is_set()
    assert any(x[0] == "unsubscribe" for x in api.calls)
    c.stop()
