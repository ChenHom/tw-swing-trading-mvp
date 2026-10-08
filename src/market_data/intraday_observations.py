"""Deterministic, PIT-safe read-only market observations.

Only predeclared support/resistance levels can generate price-level events.
Visible five-level quotes are liquidity observations, never buy/sell instructions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Sequence

from .intraday_book import MarketBook, book_metrics
from .intraday_tick import EXCHANGES, LOT_TYPES, MarketTick

RULE_VERSION = "intraday-observation-v1"


def _instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return dt


@dataclass(frozen=True)
class ObservationPlan:
    plan_id: str
    symbol: str
    exchange: str
    lot_type: str
    known_at: str
    support_low_x10000: int | None = None
    support_high_x10000: int | None = None
    resistance_x10000: int | None = None
    min_confirmation_ticks: int = 2
    min_confirmation_seconds: int = 10
    max_book_age_seconds: int = 30
    max_tick_gap_seconds: int = 90

    def __post_init__(self) -> None:
        if not self.plan_id or not self.symbol:
            raise ValueError("plan identity required")
        if self.exchange not in EXCHANGES or self.lot_type not in LOT_TYPES:
            raise ValueError("invalid plan market / lot type")
        _instant(self.known_at)
        if (self.support_low_x10000 is None) != (self.support_high_x10000 is None):
            raise ValueError("support interval must have both bounds")
        if self.support_low_x10000 is not None and not (0 < self.support_low_x10000 <= self.support_high_x10000):
            raise ValueError("invalid support range")
        if self.resistance_x10000 is not None and self.resistance_x10000 <= 0:
            raise ValueError("invalid resistance")
        if self.min_confirmation_ticks < 2 or self.min_confirmation_seconds < 1:
            raise ValueError("a single tick is not a confirmation")
        if self.max_book_age_seconds < 1 or self.max_tick_gap_seconds < 1:
            raise ValueError("invalid freshness bounds")


@dataclass(frozen=True)
class ObservationEvent:
    event_id: str
    rule_version: str
    plan_id: str | None
    symbol: str
    exchange: str
    lot_type: str
    kind: str
    status: str
    event_time: str
    observed_at: str
    collector_session_id: str
    collection_seq: int
    reason_codes: tuple[str, ...]
    metrics: dict[str, Any]
    data_health: str = "HEALTHY"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return d


def _event(
    source: MarketBook | MarketTick, *, plan_id: str | None,
    kind: str, status: str, reasons: Sequence[str] = (),
    metrics: dict[str, Any] | None = None,
    data_health: str = "HEALTHY",
) -> ObservationEvent:
    identifier = {
        "rule": RULE_VERSION, "plan": plan_id, "symbol": source.symbol,
        "exchange": source.exchange, "lot": source.lot_type,
        "kind": kind, "session": source.collector_session_id,
        "seq": source.collection_seq, "source": source.source,
    }
    event_id = hashlib.sha256(json.dumps(identifier, sort_keys=True).encode()).hexdigest()[:24]
    return ObservationEvent(
        event_id=event_id, rule_version=RULE_VERSION, plan_id=plan_id,
        symbol=source.symbol, exchange=source.exchange, lot_type=source.lot_type,
        kind=kind, status=status, event_time=source.event_time,
        observed_at=source.received_at,
        collector_session_id=source.collector_session_id,
        collection_seq=source.collection_seq,
        reason_codes=tuple(reasons), metrics=metrics or {}, data_health=data_health,
    )


def observe_book(book: MarketBook, *, max_age_seconds: int = 30) -> list[ObservationEvent]:
    age = (_instant(book.received_at) - _instant(book.event_time)).total_seconds()
    reason = ()
    if age < -2:
        reason = ("PROVIDER_CLOCK_AHEAD",)
    elif age > max_age_seconds:
        reason = ("STALE_BOOK",)
    metrics = book_metrics(book)
    metrics["age_ms"] = int(round(age * 1000))
    metrics["visible_levels_only"] = True
    metrics["is_trade_execution"] = False
    status = "INCONCLUSIVE" if reason else "OBSERVED"
    events = [
        _event(book, plan_id=None, kind="SPREAD_OBSERVED", status=status, reasons=reason,
               metrics={**metrics, "spread_x10000": metrics["spread_x10000"]}),
        _event(book, plan_id=None, kind="BOOK_IMBALANCE_OBSERVED", status=status,
               reasons=reason + (("EMPTY_VISIBLE_BOOK",) if metrics["imbalance_bps"] is None else ()),
               metrics=metrics),
    ]
    if metrics["spread_x10000"] is None:
        events[0] = _event(book, plan_id=None, kind="SPREAD_OBSERVED", status="INCONCLUSIVE",
                           reasons=reason + ("MISSING_TOP_LEVEL",), metrics=metrics)
    if metrics["imbalance_bps"] is None:
        events[1] = _event(book, plan_id=None, kind="BOOK_IMBALANCE_OBSERVED",
                           status="INCONCLUSIVE", reasons=reason + ("EMPTY_VISIBLE_BOOK",),
                           metrics=metrics)
    return events


def replay_observations(
    *,
    ticks: Iterable[MarketTick],
    books: Iterable[MarketBook],
    plan: ObservationPlan | None = None,
    data_health: str = "HEALTHY",
) -> list[ObservationEvent]:
    """Consume only events already received; sort by reception, then stable source sequence.

    A plan must have been created before receipt time. Confirmations never use
    future quotes, next day's price action, or gap/stale market data.
    """
    if data_health not in ("HEALTHY", "DEGRADED", "FAILED"):
        raise ValueError("unknown data health")
    incoming: list[tuple[datetime, int, int, MarketBook | MarketTick]] = []
    for b in books:
        incoming.append((_instant(b.received_at), 0, b.collection_seq, b))
    for t in ticks:
        incoming.append((_instant(t.received_at), 1, t.collection_seq, t))
    incoming.sort(key=lambda item: (item[0], item[1], item[2],
                                     item[3].symbol, item[3].lot_type))
    out: list[ObservationEvent] = []
    books_by_symbol: dict[tuple[str, str, str], MarketBook] = {}
    last_tick_time: dict[tuple[str, str, str], datetime] = {}
    support_test: datetime | None = None
    support_confirm_count = 0
    support_state = "NOT_TESTED"
    breakout_start: datetime | None = None
    breakout_count = 0
    breakout_state = "NOT_OBSERVED"

    for received_at, _priority, _seq, item in incoming:
        key = (item.exchange, item.symbol, item.lot_type)
        if isinstance(item, MarketBook):
            books_by_symbol[key] = item
            events = observe_book(item, max_age_seconds=plan.max_book_age_seconds if plan else 30)
            if data_health != "HEALTHY":
                events = [
                    replace(e, status="INCONCLUSIVE", data_health=data_health,
                            reason_codes=e.reason_codes + ("DATA_HEALTH_NOT_HEALTHY",))
                    for e in events
                ]
            out.extend(events)
            continue

        tick = item
        if plan is None or (tick.symbol, tick.exchange, tick.lot_type) != (
            plan.symbol, plan.exchange, plan.lot_type
        ):
            continue
        if received_at < _instant(plan.known_at):
            continue
        event_at = _instant(tick.event_time)
        # Late packet whose trade predates the locked plan cannot be evaluated
        # with a support/resistance level that had not been known then.
        if event_at < _instant(plan.known_at):
            continue
        previous = last_tick_time.get(key)
        last_tick_time[key] = event_at
        reasons = []
        if data_health != "HEALTHY":
            reasons.append("DATA_HEALTH_NOT_HEALTHY")
        if previous is not None:
            interval = (event_at - previous).total_seconds()
            if interval < 0:
                reasons.append("OUT_OF_ORDER_TICK")
            elif interval > plan.max_tick_gap_seconds:
                reasons.append("TICK_GAP")
        last_book = books_by_symbol.get(key)
        if last_book is None:
            reasons.append("NO_BOOK")
        else:
            book_when = _instant(last_book.event_time)
            # One cannot use a quote published after the trade it is confirming.
            if book_when > event_at:
                reasons.append("FUTURE_BOOK")
            elif last_book.collector_session_id != tick.collector_session_id:
                reasons.append("SESSION_MISMATCH")
            else:
                quote_age = (event_at - book_when).total_seconds()
                if quote_age > plan.max_book_age_seconds:
                    reasons.append("STALE_BOOK")
                age_at_receipt = (received_at - book_when).total_seconds()
                if age_at_receipt > plan.max_book_age_seconds:
                    reasons.append("DELAYED_BOOK")
        if (_instant(tick.received_at) - event_at).total_seconds() > plan.max_book_age_seconds:
            reasons.append("DELAYED_TICK")
        if reasons:
            support_test = None
            support_confirm_count = 0
            breakout_start = None
            breakout_count = 0
            # Require fresh observations to restart a confirmation window.
            if support_state == "TESTING":
                support_state = "NOT_TESTED"
            if breakout_state == "TESTING":
                breakout_state = "NOT_OBSERVED"
            out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="PRICE_LEVEL_CHECK",
                              status="INCONCLUSIVE", reasons=reasons,
                              metrics={"price_x10000": tick.price_x10000}))
            continue

        px = tick.price_x10000
        context = {"price_x10000": px, "trade_volume_shares": tick.trade_volume_shares}
        if plan.support_low_x10000 is not None:
            low, high = plan.support_low_x10000, plan.support_high_x10000
            if support_state == "NOT_TESTED" and low <= px <= high:
                support_state = "TESTING"
                support_test = event_at
                support_confirm_count = 0
                out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="SUPPORT_TESTED",
                                  status="OBSERVED", metrics={**context, "low": low, "high": high}))
            elif support_state == "TESTING":
                if px < low:
                    support_state = "BROKEN"
                    out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="SUPPORT_BROKEN",
                                      status="OBSERVED", metrics={**context, "low": low}))
                elif px >= high:
                    support_confirm_count += 1
                    if (support_confirm_count >= plan.min_confirmation_ticks and
                            (event_at - support_test).total_seconds() >= plan.min_confirmation_seconds):
                        support_state = "HELD"
                        out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="SUPPORT_HELD",
                                          status="OBSERVED", metrics={**context, "confirm_ticks": support_confirm_count}))
            elif support_state == "HELD" and px < low:
                support_state = "BROKEN"
                out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health,
                                  kind="SUPPORT_BROKEN", status="OBSERVED",
                                  metrics={**context, "low": low, "after_held": True}))
        if plan.resistance_x10000 is not None:
            resistance = plan.resistance_x10000
            if breakout_state == "NOT_OBSERVED" and px > resistance:
                breakout_state = "TESTING"
                breakout_start = event_at
                breakout_count = 1
            elif breakout_state == "TESTING":
                if px <= resistance:
                    breakout_state = "NOT_OBSERVED"
                    breakout_start = None
                    breakout_count = 0
                else:
                    breakout_count += 1
                    if (breakout_count >= plan.min_confirmation_ticks and
                            (event_at - breakout_start).total_seconds() >= plan.min_confirmation_seconds):
                        breakout_state = "OBSERVED"
                        out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="BREAKOUT_OBSERVED",
                                          status="OBSERVED", metrics={**context, "resistance": resistance,
                                                                       "confirm_ticks": breakout_count}))
            elif breakout_state == "OBSERVED" and px <= resistance:
                breakout_state = "RETESTED"
                out.append(_event(tick, plan_id=plan.plan_id, data_health=data_health, kind="BREAKOUT_RETESTED",
                                  status="OBSERVED", metrics={**context, "resistance": resistance}))
    return out
