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
