"""Fixed 2327 preflight: only synthetic quote subscriptions; NEVER logs into Shioaji."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.market_data.intraday_book import RawBookStore
from src.market_data.intraday_collector import ShioajiTickCollector, build_subscriptions
from src.market_data.intraday_tick import RawTickStore


class OfflineSDK:
    """No login(), activate_ca(), place_order() or network methods."""
    def __init__(self):
        self.contracts = self
        self.registered = {}
        self.calls = []
        self.quote = SimpleNamespace(set_event_callback=lambda fn: self.registered.update(event=fn))

    def get(self, symbol):
        return f"fixture:{symbol}"

    def on_tick_stk_v1(self):
        return lambda fn: self.registered.update(tick=fn)

    def on_bidask_stk_v1(self):
        return lambda fn: self.registered.update(book=fn)

    def subscribe(self, contract, *, quote_type, intraday_odd):
        self.calls.append(("subscribe", contract, quote_type, intraday_odd))

    def unsubscribe(self, contract, *, quote_type, intraday_odd):
        self.calls.append(("unsubscribe", contract, quote_type, intraday_odd))


def _offline_cli():
    # Bypass application-wide CLI imports; the preflight must need no broker SDK.
    path = Path(__file__).resolve().parents[2] / "src" / "cli" / "intraday.py"
    spec = importlib.util.spec_from_file_location("offline_2327_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_2327_plan_is_exactly_board_and_odd_and_never_logs_in(capsys):
    module = _offline_cli()
    module.cmd_intraday_scope(SimpleNamespace(
        positions_accounts=None, db="this-file-must-not-be-opened.db",
        candidates_file=None, watchlist_file=None, symbols="2327", max_symbols=1,
    ))
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY"
    assert output["live_login"] is False
    assert output["sdk_subscription_limit_verified"] is False
    assert output["symbols"] == {"2327": ["watchlist"]}
    assert output["subscription_count"] == 2
    assert output["subscriptions"] == [
        {"symbol": "2327", "lot_type": "BOARD"},
        {"symbol": "2327", "lot_type": "ODD"},
    ]


def test_2327_fake_sdk_registers_four_quote_topics_and_cleanly_unsubscribes(tmp_path):
    specs, origins = build_subscriptions(watchlist=["2327"], max_symbols=1)
    assert origins == {"2327": ["watchlist"]}
    sdk = OfflineSDK()
    collector = ShioajiTickCollector(
        sdk, subscriptions=specs, quote_type="Tick",
        store=RawTickStore(tmp_path), book_quote_type="BidAsk",
        book_store=RawBookStore(tmp_path), session_id="preflight-2327",
    )
    collector.start()
    assert collector.health()["subscriptions"] == 4
    assert collector.health()["quote_event_callback_registered"] is True
    # No official transport-UP event observed: cannot claim a verified feed.
    assert collector.health()["state"] == "UNVERIFIED"
    topics = {(kind, odd) for op, _, kind, odd in sdk.calls if op == "subscribe"}
    assert topics == {("Tick", False), ("Tick", True), ("BidAsk", False), ("BidAsk", True)}
    assert all(contract == "fixture:2327" for _, contract, _, _ in sdk.calls)
    assert collector.stop()["state"] == "CLOSED"
    assert len([x for x in sdk.calls if x[0] == "unsubscribe"]) == 4
    assert not hasattr(sdk, "login") and not hasattr(sdk, "place_order")


def test_real_2327_smoke_entry_remains_blocked_without_explicit_approval(monkeypatch):
    module = _offline_cli()
    monkeypatch.delenv("INTRADAY_LIVE_SMOKE_APPROVED", raising=False)
    with pytest.raises(RuntimeError, match="explicit flag and approval"):
        module.cmd_intraday_collect(SimpleNamespace(enable_live_smoke=False))
    with pytest.raises(RuntimeError, match="explicit flag and approval"):
        module.cmd_intraday_collect(SimpleNamespace(enable_live_smoke=True))


@pytest.mark.parametrize("fail_login", [False, True])
@pytest.mark.parametrize("bad_health", [None, "missing_tick", "missing_book", "unsub",
                                        "failed_worker", "pending_queue"])
def test_2327_smoke_is_always_sdk_simulation_and_never_calls_trading_api(
    monkeypatch, tmp_path, capsys, fail_login, bad_health,
):
    import sys
    import types
    from datetime import datetime
    from src.market_data.intraday_collector import Subscription

    module = _offline_cli()
    monkeypatch.setenv("INTRADAY_LIVE_SMOKE_APPROVED", "yes")

    # Fake calendar/credentials/SDK: this test has no broker or network access.
    fake_calendar = types.ModuleType("src.calendar.calendar")
    fake_calendar.ExchangeCalendarsTradingCalendar = lambda: SimpleNamespace(
        is_trading_day=lambda _: True
    )
    monkeypatch.setitem(sys.modules, "src.calendar.calendar", fake_calendar)

    fake_settings = types.ModuleType("src.config")
    fake_settings.AppSettings = lambda: SimpleNamespace(
        shioaji_api_key="fake-key", shioaji_secret_key="fake-secret"
    )
    monkeypatch.setitem(sys.modules, "src.config", fake_settings)

    observed = {}
    class FakeShioaji:
        def __init__(self, *, simulation):
            observed["simulation"] = simulation

        def login(self, **kwargs):
            observed["login"] = kwargs
            if fail_login:
                raise RuntimeError("simulated broker login failure: id=N000000000")

        def logout(self):
            observed["logout"] = True

    sdk_module = types.ModuleType("shioaji")
    sdk_module.Shioaji = FakeShioaji
    # Match the legacy installed SDK: no shioaji.QuoteType at top level.
    sdk_module.constant = SimpleNamespace(
        QuoteType=SimpleNamespace(Tick="enum-tick", BidAsk="enum-book"),
        QuoteVersion=SimpleNamespace(v1="enum-v1"),
    )
    monkeypatch.setitem(sys.modules, "shioaji", sdk_module)

    class FakeCollector:
        def __init__(self, api, *, subscriptions, quote_type, store,
                     book_quote_type, book_store, legacy_quote_version):
            observed["legacy_quote_version"] = legacy_quote_version
            observed["subscriptions"] = subscriptions
            observed["tick"] = quote_type
            observed["bidask"] = book_quote_type

        def start(self):
            observed["started"] = True

        def stop(self):
            observed["stopped"] = True

        def health(self):
            status = {
                "state": "CLOSED",
                "active_subscription_count": 0,
                "queue_depth": 0,
                "collection_status": "RAW_CAPTURED",
                "counters": {"raw_written": 3, "book_raw_written": 7},
            }
            if bad_health == "missing_tick":
                status["counters"]["raw_written"] = 0
            elif bad_health == "missing_book":
                status["counters"]["book_raw_written"] = 0
            elif bad_health == "unsub":
                status["active_subscription_count"] = 1
            elif bad_health == "failed_worker":
                status["state"] = "FAILED"
            elif bad_health == "pending_queue":
                status["queue_depth"] = 1
            return status

    class DateStub:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 12, 9, 15, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", DateStub)
    monkeypatch.setattr(module, "ShioajiTickCollector", FakeCollector)
    monkeypatch.setattr(module, "write_health", lambda *_: None)
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        module, "scope_for_args",
        lambda _: ([Subscription("2327", "BOARD"), Subscription("2327", "ODD")],
                   {"2327": ["watchlist"]}),
    )
    args = SimpleNamespace(
        enable_live_smoke=True, duration_seconds=1,
        cache_dir=str(tmp_path), stop_at_disk_pct=75, with_bidask=True,
    )
    if fail_login:
        with pytest.raises(RuntimeError, match="Shioaji simulation login failed") as caught:
            module.cmd_intraday_collect(args)
        assert "N000000000" not in str(caught.value)
        assert observed["logout"] is True
        assert observed["simulation"] is True
        assert "started" not in observed
        return
    if bad_health:
        with pytest.raises(SystemExit) as result_exit:
            module.cmd_intraday_collect(args)
        assert result_exit.value.code == 1
    else:
        module.cmd_intraday_collect(args)
    result = json.loads(capsys.readouterr().out)
    assert result["smoke_status"] == ("FAIL" if bad_health else "PASS_CAPTURE_ONLY")
    assert all(result["smoke_checks"].values()) is (bad_health is None)
    assert observed["simulation"] is True
    assert observed["started"] and observed["stopped"] and observed["logout"]
    assert observed["login"]["subscribe_trade"] is False
    assert [x.symbol for x in observed["subscriptions"]] == ["2327", "2327"]
    assert observed["tick"] == "enum-tick"
    assert observed["bidask"] == "enum-book"
    assert observed["legacy_quote_version"] == "enum-v1"
    assert result["simulation"] is True
    assert result["mode"] == "ONE_OFF_SIMULATION_QUOTE_SMOKE"
    assert result["real_market_verified"] is False
    assert not hasattr(FakeShioaji, "place_order")
    assert not hasattr(FakeShioaji, "activate_ca")


def test_quote_type_resolver_supports_modern_exports_and_documented_strings(monkeypatch):
    import sys
    import types
    module = _offline_cli()
    enum = SimpleNamespace(Tick="stock-tick", BidAsk="stock-bidask")
    assert module._quote_types(SimpleNamespace(QuoteType=enum)) == (
        "stock-tick", "stock-bidask"
    )
    assert module._quote_types(SimpleNamespace(constant=SimpleNamespace(QuoteType=enum))) == (
        "stock-tick", "stock-bidask"
    )
    # If this SDK exposes no QuoteType enum, official string quote types
    # are an explicit compatibility fallback.
    sdk = types.ModuleType("shioaji")
    monkeypatch.setitem(sys.modules, "shioaji", sdk)
    assert module._quote_types(sdk) == ("tick", "bid_ask")


class LegacyQuoteOnlySDK(OfflineSDK):
    """Old installed Shioaji shape: subscribe/unsubscribe on api.quote."""
    subscribe = None
    unsubscribe = None

    def __init__(self, *, fail_topic=None):
        super().__init__()
        self.contracts = None
        self.Contracts = SimpleNamespace(Stocks={"2327": "legacy-contract:2327"})
        self.fail_topic = fail_topic
        self.quote = SimpleNamespace(
            set_event_callback=lambda fn: self.registered.update(event=fn),
            subscribe=self._subscribe_quote,
            unsubscribe=self._unsubscribe_quote,
        )

    def _subscribe_quote(self, contract, *, quote_type, intraday_odd, version):
        topic = (quote_type, intraday_odd)
        if topic == self.fail_topic:
            raise RuntimeError("legacy SDK subscription error")
        self.calls.append(("subscribe", contract, quote_type, intraday_odd, version))

    def _unsubscribe_quote(self, contract, *, quote_type, intraday_odd, version):
        self.calls.append(("unsubscribe", contract, quote_type, intraday_odd, version))


def _legacy_collector(tmp_path, api, *, version="v1"):
    specs, _ = build_subscriptions(watchlist=["2327"], max_symbols=1)
    return ShioajiTickCollector(
        api, subscriptions=specs, quote_type="Tick", store=RawTickStore(tmp_path),
        book_quote_type="BidAsk", book_store=RawBookStore(tmp_path),
        legacy_quote_version=version,
        session_id="legacy-test",
    )


def test_legacy_sdk_uses_quote_subscribe_v1_and_quote_unsubscribe_v1(tmp_path):
    api = LegacyQuoteOnlySDK()
    c = _legacy_collector(tmp_path, api)
    c.start()
    topics = {(item[2], item[3]) for item in api.calls if item[0] == "subscribe"}
    assert topics == {
        ("Tick", False), ("Tick", True), ("BidAsk", False), ("BidAsk", True),
    }
    assert len([x for x in api.calls if x[0] == "subscribe"]) == 4
    assert all(x[1] == "legacy-contract:2327" and x[4] == "v1" for x in api.calls)
    assert c.health()["quote_event_callback_registered"] is True
    c.stop()
    assert len([x for x in api.calls if x[0] == "unsubscribe"]) == 4
    assert all(x[4] == "v1" for x in api.calls)
    assert not hasattr(api, "login") and not hasattr(api, "place_order")


def test_legacy_sdk_reconnect_resubscribes_all_four_topics_using_v1(tmp_path):
    api = LegacyQuoteOnlySDK()
    c = _legacy_collector(tmp_path, api)
    c.start()
    api.registered["event"](200, 0, "connected", "up")
    c.queue.join()
    api.registered["event"](200, 1, "disconnect", "down")
    api.registered["event"](200, 13, "reconnected", "up")
    c.queue.join()
    h = c.health()
    assert h["gap_unresolved"] is True
    assert h["counters"]["resubscribe_success"] == 1
    assert len([x for x in api.calls if x[0] == "subscribe"]) == 8
    assert all(x[4] == "v1" for x in api.calls)
    c.stop()


def test_legacy_sdk_rejects_missing_v1_before_subscribe(tmp_path):
    api = LegacyQuoteOnlySDK()
    c = _legacy_collector(tmp_path, api, version=None)
    with pytest.raises(TypeError, match="explicit QuoteVersion.v1"):
        c.start()
    assert api.calls == []


def test_legacy_sdk_partial_subscription_failure_rolls_back_with_v1(tmp_path):
    api = LegacyQuoteOnlySDK(fail_topic=("BidAsk", True))
    c = _legacy_collector(tmp_path, api)
    with pytest.raises(RuntimeError, match="legacy SDK subscription error"):
        c.start()
    assert c.state == "FAILED"
    assert len([x for x in api.calls if x[0] == "subscribe"]) == 3
    assert len([x for x in api.calls if x[0] == "unsubscribe"]) == 3
    assert all(x[4] == "v1" for x in api.calls)
