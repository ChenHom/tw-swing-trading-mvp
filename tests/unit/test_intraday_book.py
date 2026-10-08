"""PR-2: five-level book, shared quote session and replayable observations.

No Shioaji login or order submission; fixtures are entirely synthetic.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.market_data.intraday_book import (
    RawBookStore, book_metrics, book_path, normalize_raw_book,
    raw_book_event, replay_books,
)
from src.market_data.intraday_collector import ShioajiTickCollector, Subscription
from src.market_data.intraday_observations import (
    ObservationPlan, observe_book, replay_observations,
)
from src.market_data.intraday_tick import RawTickStore, compress_raw, normalize_raw_tick, raw_event

ZONE = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 8, 9, 5, 0, tzinfo=ZONE)


def sdk_book(*, code="2327", odd=False, at=BASE, bid=None, ask=None,
             bid_qty=None, ask_qty=None, simtrade=False, suspend=False):
    return SimpleNamespace(
        code=code, datetime=at, date=at.date(), time=at.time(),
        bid_price=bid if bid is not None else [637, 636, 635, 634, 633],
        ask_price=ask if ask is not None else [638, 639, 640, 641, 642],
        bid_volume=bid_qty if bid_qty is not None else [3, 2, 1, 2, 2],
        ask_volume=ask_qty if ask_qty is not None else [2, 1, 1, 1, 1],
        diff_bid_vol=[1, 0, -1, 0, 0],
        diff_ask_vol=[0, 0, 0, 0, 0],
        intraday_odd=odd, simtrade=simtrade, suspend=suspend,
    )


def sdk_tick(*, code="2327", at=BASE, close=636, odd=False):
    return SimpleNamespace(
        code=code, datetime=at, close=close, volume=2, total_volume=10,
        tick_type=1, intraday_odd=odd, suspend=False, simtrade=False,
        date=at.date(), time=at.time(),
    )


def book_record(**kwargs):
    book = sdk_book(**kwargs)
    return raw_book_event("TSE", book, session_id="session1", seq=1, received_at=book.datetime)


def canonical_book(**kwargs):
    result, reason = normalize_raw_book(book_record(**kwargs))
    assert reason == ""
    return result


def canonical_tick(*, at=BASE, close=636, seq=1, received=None, session="session1"):
    raw = raw_event("TSE", sdk_tick(at=at, close=close),
                    session_id=session, seq=seq, received_at=received or at)
    normalized, reason = normalize_raw_tick(raw)
    assert reason == ""
    return normalized


def test_book_units_level_order_spread_imbalance_and_delta_is_not_execution():
    book = canonical_book()
    assert book.lot_type == "BOARD"
    assert book.bid_prices_x10000 == (6370000, 6360000, 6350000, 6340000, 6330000)
    assert book.bid_volumes_shares == (3000, 2000, 1000, 2000, 2000)
    assert book.ask_volumes_shares[0] == 2000
    metrics = book_metrics(book)
    assert metrics["spread_x10000"] == 10000
    assert metrics["bid_visible_shares"] == 10000
    assert metrics["ask_visible_shares"] == 6000
    assert metrics["imbalance_bps"] == 6250
    assert metrics["coverage_bid_levels"] == 5
    assert "execution" not in metrics


def test_odd_lot_volumes_are_shares_and_raw_deltas_remain_only_raw():
    raw = book_record(odd=True, bid_qty=[31, 2, 1, 2, 2])
    book, reason = normalize_raw_book(raw)
    assert reason == ""
    assert book.bid_volumes_shares[0] == 31
    assert raw["payload"]["diff_bid_vol"] == [1, 0, -1, 0, 0]
    assert not hasattr(book, "diff_bid_vol")


@pytest.mark.parametrize("kwargs,reason", [
    ({"bid": [638, 637, 636, 635, 634]}, "crossed_book"),
    ({"bid": [637, 638, 635, 634, 633]}, "unsorted_levels"),
    ({"ask": [638, 638, 640, 641, 642]}, "duplicate_level_price"),
    ({"ask": [638] * 6, "ask_qty": [1] * 6}, "side_length"),
    ({"bid": [637, 636], "bid_qty": [1]}, "side_length"),
    ({"bid": [637, None, 635], "bid_qty": [2, None, 1]}, "noncontiguous_levels"),
    ({"bid_qty": [-1, 2, 3, 4, 5]}, "needs_review"),
    ({"simtrade": True}, "simtrade"),
    ({"suspend": True}, "suspend"),
])
def test_bad_book_rejected_with_reason(kwargs, reason):
    assert normalize_raw_book(book_record(**kwargs))[1] == reason


def test_missing_top_is_inconclusive_not_a_fake_bid():
    book = canonical_book(bid=[], bid_qty=[])
    assert book.bid_prices_x10000 == (None,) * 5
    result = observe_book(book)
    assert result[0].kind == "SPREAD_OBSERVED"
    assert result[0].status == "INCONCLUSIVE"
    assert "MISSING_TOP_LEVEL" in result[0].reason_codes
    assert book_metrics(book)["imbalance_bps"] == 0


def test_empty_both_sides_not_100_percent_buying():
    book = canonical_book(bid=[], bid_qty=[], ask=[], ask_qty=[])
    metrics = book_metrics(book)
    assert metrics["imbalance_bps"] is None
    assert all(event.status == "INCONCLUSIVE" for event in observe_book(book))


def test_stale_or_future_book_is_inconclusive():
    b = canonical_book()
    stale = replace(b, received_at=(BASE + timedelta(seconds=45)).isoformat())
    ev = observe_book(stale, max_age_seconds=30)
    assert all(x.status == "INCONCLUSIVE" for x in ev)
    assert "STALE_BOOK" in ev[0].reason_codes
    ahead = replace(b, received_at=(BASE - timedelta(seconds=10)).isoformat())
    assert "PROVIDER_CLOCK_AHEAD" in observe_book(ahead)[0].reason_codes


def test_replay_stores_raw_without_mix_with_tick_and_supports_gzip(tmp_path):
    store = RawBookStore(tmp_path)
    raw = book_record()
    path = store.append(raw)
    tickpath = RawTickStore(tmp_path).append(
        raw_event("TSE", sdk_tick(), session_id="session1", seq=1, received_at=BASE)
    )
    assert path == book_path(tmp_path, "2026-10-08", "BOARD", "2327")
    assert path != tickpath
    books, rejected = replay_books(path)
    assert len(books) == 1 and rejected == {}
    summary = compress_raw(path)
    gzpath = Path(summary["archive"])
    assert replay_books(gzpath) == (books, {})
    assert not path.exists()


def test_board_and_odd_book_paths_and_market_are_isolated(tmp_path):
    board_path = RawBookStore(tmp_path).append(book_record())
    odd_path = RawBookStore(tmp_path).append(book_record(odd=True))
    assert board_path != odd_path
    books_board, _ = replay_books(board_path)
    books_odd, _ = replay_books(odd_path)
    assert books_board[0].bid_volumes_shares[0] == 3000
    assert books_odd[0].bid_volumes_shares[0] == 3


def test_plan_is_predeclared_and_cannot_use_future_prices():
    with pytest.raises(ValueError):
        ObservationPlan("x", "2327", "TSE", "BOARD",
                        BASE.isoformat(), support_low_x10000=6350000)
    with pytest.raises(ValueError):
        ObservationPlan("x", "2327", "TSE", "BOARD",
                        BASE.isoformat(), min_confirmation_ticks=1)
    plan = ObservationPlan(
        "fixed", "2327", "TSE", "BOARD",
        (BASE - timedelta(days=1)).isoformat(),
        support_low_x10000=6350000, support_high_x10000=6360000,
        resistance_x10000=6400000, max_book_age_seconds=30,
    )
    tick1 = canonical_tick(at=BASE + timedelta(seconds=2), close=635.5)
    tick2 = canonical_tick(at=BASE + timedelta(seconds=12), close=636, seq=2)
    tick3 = canonical_tick(at=BASE + timedelta(seconds=22), close=638, seq=3)
    b2 = canonical_book(at=BASE + timedelta(seconds=24))
    tick4 = canonical_tick(at=BASE + timedelta(seconds=26), close=641, seq=4)
    tick5 = canonical_tick(at=BASE + timedelta(seconds=38), close=643, seq=5)
    tick6 = canonical_tick(at=BASE + timedelta(seconds=42), close=639, seq=6)
    outcome = replay_observations(
        ticks=[tick1, tick2, tick3, tick4, tick5, tick6],
        books=[canonical_book(), b2], plan=plan,
    )
    triggered = [x.kind for x in outcome if x.kind not in ("SPREAD_OBSERVED", "BOOK_IMBALANCE_OBSERVED")]
    assert triggered == ["SUPPORT_TESTED", "SUPPORT_HELD", "BREAKOUT_OBSERVED", "BREAKOUT_RETESTED"]
    assert all(event.status == "OBSERVED" for event in outcome)
    assert [x.as_dict() for x in outcome] == [x.as_dict() for x in replay_observations(
        ticks=[tick6, tick4, tick1, tick5, tick3, tick2], books=[b2, canonical_book()], plan=plan,
    )]


def test_no_plan_no_support_events_and_missing_book_blocks_confirmation():
    ticks = [canonical_tick(at=BASE + timedelta(seconds=x), close=p, seq=i)
             for i, (x, p) in enumerate(((1, 635.5), (12, 636), (25, 638)), 1)]
    assert replay_observations(ticks=ticks, books=[]) == []
    plan = ObservationPlan(
        "fixed", "2327", "TSE", "BOARD", (BASE - timedelta(days=1)).isoformat(),
        support_low_x10000=6350000, support_high_x10000=6360000,
    )
    result = replay_observations(ticks=ticks, books=[], plan=plan)
    assert all(x.status == "INCONCLUSIVE" and x.kind == "PRICE_LEVEL_CHECK" for x in result)
    result2 = replay_observations(ticks=ticks, books=[canonical_book()], plan=plan,
                                   data_health="DEGRADED")
    assert "SUPPORT_HELD" not in [x.kind for x in result2]
    assert any("DATA_HEALTH_NOT_HEALTHY" in x.reason_codes for x in result2)


def test_not_using_a_quote_that_arrives_after_the_tick():
    plan = ObservationPlan(
        "fixed", "2327", "TSE", "BOARD", (BASE - timedelta(days=1)).isoformat(),
        resistance_x10000=6400000,
    )
    tick = canonical_tick(at=BASE + timedelta(seconds=5), close=643, seq=1)
    book = replace(canonical_book(), received_at=(BASE + timedelta(seconds=6)).isoformat())
    result = replay_observations(ticks=[tick], books=[book], plan=plan)
    assert result[0].kind == "PRICE_LEVEL_CHECK"
    assert "NO_BOOK" in result[0].reason_codes


class FakeAPI:
    def __init__(self, fail_type=None):
        self.contracts = self
        self.callbacks = {}
        self.calls = []
        self.fail_type = fail_type

    def get(self, symbol):
        return symbol

    def on_tick_stk_v1(self):
        return lambda fn: self.callbacks.update(tick=fn)

    def on_bidask_stk_v1(self):
        return lambda fn: self.callbacks.update(bidask=fn)

    def subscribe(self, contract, *, quote_type, intraday_odd):
        if quote_type == self.fail_type:
            raise ValueError("subscription failed")
        self.calls.append(("subscribe", contract, quote_type, intraday_odd))

    def unsubscribe(self, contract, *, quote_type, intraday_odd):
        self.calls.append(("unsubscribe", contract, quote_type, intraday_odd))


def test_shared_collector_subscribes_tick_and_bidask_and_shuts_both_down(tmp_path):
    sdk = FakeAPI()
    ticks, books = [], []
    collector = ShioajiTickCollector(
        sdk, subscriptions=[Subscription("2327", "BOARD")],
        quote_type="Tick", store=RawTickStore(tmp_path), sink=ticks.append,
        book_quote_type="BidAsk", book_store=RawBookStore(tmp_path),
        book_sink=books.append, session_id="session1",
    )
    collector.start()
    sdk.callbacks["tick"]("TSE", sdk_tick())
    sdk.callbacks["bidask"]("TSE", sdk_book())
    collector.queue.join()
    assert len(ticks) == len(books) == 1
    assert collector.health()["bidask_enabled"] is True
    assert collector.health()["subscriptions"] == 2
    assert collector.health()["counters"]["book_raw_written"] == 1
    collector.stop()
    assert collector.health()["state"] == "CLOSED"
    assert [c[2] for c in sdk.calls] == ["Tick", "BidAsk", "BidAsk", "Tick"]
    assert book_path(tmp_path, "2026-10-08", "BOARD", "2327").exists()


def test_failure_second_subscription_rolls_back_tick_and_never_trades(tmp_path):
    sdk = FakeAPI(fail_type="BidAsk")
    collector = ShioajiTickCollector(
        sdk, subscriptions=[Subscription("2327")], quote_type="Tick",
        store=RawTickStore(tmp_path), book_quote_type="BidAsk",
        book_store=RawBookStore(tmp_path),
    )
    with pytest.raises(ValueError, match="subscription failed"):
        collector.start()
    assert [c[0] for c in sdk.calls] == ["subscribe", "unsubscribe"]
    assert collector.state == "FAILED"


def test_book_raw_write_failure_degrades_health(tmp_path):
    sdk = FakeAPI()
    coll = ShioajiTickCollector(
        sdk, subscriptions=[Subscription("2327")], quote_type="Tick",
        store=RawTickStore(tmp_path), book_quote_type="BidAsk",
        book_store=RawBookStore(tmp_path, stop_at_disk_pct=0.00001),
    )
    assert coll.process_book("TSE", sdk_book(), received_at=BASE) is None
    assert coll.health()["state"] == "DEGRADED"
    assert coll.health()["counters"]["book_raw_write_error"] == 1


def test_book_metrics_degrade_together_with_collector_health():
    b = canonical_book()
    result = replay_observations(ticks=[], books=[b], data_health="DEGRADED")
    assert len(result) == 2
    assert all(x.status == "INCONCLUSIVE" and x.data_health == "DEGRADED" for x in result)
    assert all("DATA_HEALTH_NOT_HEALTHY" in x.reason_codes for x in result)


def test_trade_predating_plan_is_not_evaluated_even_if_received_later():
    locked_at = BASE + timedelta(seconds=4)
    plan = ObservationPlan(
        "late-plan", "2327", "TSE", "BOARD", locked_at.isoformat(),
        support_low_x10000=6350000, support_high_x10000=6360000,
    )
    early = canonical_tick(
        at=BASE + timedelta(seconds=2), close=635.5,
        received=BASE + timedelta(seconds=8),
    )
    result = replay_observations(ticks=[early], books=[canonical_book()], plan=plan)
    assert not any(x.kind.startswith("SUPPORT") or x.kind == "PRICE_LEVEL_CHECK" for x in result)


def test_support_can_break_after_it_was_held():
    plan = ObservationPlan(
        "support", "2327", "TSE", "BOARD", (BASE - timedelta(days=1)).isoformat(),
        support_low_x10000=6350000, support_high_x10000=6360000,
    )
    ticks = [canonical_tick(at=BASE + timedelta(seconds=sec), close=price, seq=idx)
             for idx, (sec, price) in enumerate(
                 [(2, 635.5), (12, 636), (22, 638), (25, 634)], 1)]
    kinds = [x.kind for x in replay_observations(
        ticks=ticks, books=[canonical_book()], plan=plan
    )]
    assert kinds.count("SUPPORT_BROKEN") == 1
    assert kinds.index("SUPPORT_HELD") < kinds.index("SUPPORT_BROKEN")


def test_odd_lot_tick_cannot_use_board_lot_quote_for_support():
    plan = ObservationPlan(
        "odd", "2327", "TSE", "ODD", (BASE - timedelta(days=1)).isoformat(),
        support_low_x10000=6350000, support_high_x10000=6360000,
    )
    raw = raw_event(
        "TSE", sdk_tick(odd=True, at=BASE + timedelta(seconds=2), close=635.5),
        seq=1, session_id="session1", received_at=BASE + timedelta(seconds=2),
    )
    odd_tick, reason = normalize_raw_tick(raw)
    assert reason == ""
    result = replay_observations(ticks=[odd_tick], books=[canonical_book()], plan=plan)
    assert "NO_BOOK" in result[-1].reason_codes
    assert result[-1].status == "INCONCLUSIVE"
