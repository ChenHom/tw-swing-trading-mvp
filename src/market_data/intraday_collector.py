"""Candidate-scoped, read-only Shioaji Tick collector.

No SDK import, login or trade API here: the caller owns a quote-only SDK session.
The SDK callback only queues; the worker owns raw persistence and normalization.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

from .intraday_connection import QuoteTransportAudit, register_quote_events
from .intraday_storage import collector_lease

from .intraday_book import (
    BOOK_FIELDS, MarketBook, RawBookStore, normalize_raw_book, raw_book_event,
)
from .intraday_tick import (
    FIELDS, LOT_TYPES, MarketTick, RawTickStore, normalize_raw_tick, raw_event,
)

@dataclass(frozen=True)
class Subscription:
    symbol: str
    lot_type: str = "BOARD"

    def __post_init__(self) -> None:
        if self.lot_type not in LOT_TYPES:
            raise ValueError("invalid lot type")
        if not self.symbol or not self.symbol.isascii() or not self.symbol.isalnum() or len(self.symbol) > 12:
            raise ValueError("invalid symbol")


def build_subscriptions(
    *,
    positions: Iterable[str] = (),
    candidates: Iterable[str] = (),
    watchlist: Iterable[str] = (),
    lot_types: Sequence[str] = ("BOARD", "ODD"),
    max_symbols: int = 20,
) -> tuple[list[Subscription], dict[str, list[str]]]:
    """Return deterministic subscriptions + source audit. Never scans full universe."""
    if max_symbols < 1 or max_symbols > 100:
        raise ValueError("max_symbols must be 1..100")
    sources: dict[str, set[str]] = {}
    for label, collection in (("position", positions), ("candidate", candidates), ("watchlist", watchlist)):
        for raw in collection:
            symbol = str(raw).strip()
            Subscription(symbol)
            sources.setdefault(symbol, set()).add(label)
    if len(sources) > max_symbols:
        raise ValueError(f"subscription scope exceeds {max_symbols} symbols")
    specs = [Subscription(symbol, lot) for symbol in sorted(sources) for lot in lot_types]
    # Fail early on unknown lot or duplicate configured lot type.
    if len(set(specs)) != len(specs):
        raise ValueError("duplicate lot type")
    return specs, {symbol: sorted(labels) for symbol, labels in sorted(sources.items())}


class ShioajiTickCollector:
    """Only live callbacks, raw audit, and canonical Tick sink; no trading actions."""

    def __init__(
        self,
        api: Any,
        *,
        subscriptions: Sequence[Subscription],
        quote_type: Any,
        store: RawTickStore,
        sink: Callable[[MarketTick], None] | None = None,
        queue_capacity: int = 5000,
        session_id: str | None = None,
        book_quote_type: Any | None = None,
        book_store: RawBookStore | None = None,
        book_sink: Callable[[MarketBook], None] | None = None,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.api = api
        self.subscriptions = tuple(subscriptions)
        if len(set(self.subscriptions)) != len(self.subscriptions):
            raise ValueError("duplicate subscriptions")
        self.scope = set(self.subscriptions)
        self.scope_keys = {(sub.symbol, sub.lot_type) for sub in self.subscriptions}
        self.quote_type = quote_type
        self.store = store
        self.sink = sink
        self.book_quote_type = book_quote_type
        self.book_store = book_store
        self.book_sink = book_sink
        if (book_quote_type is None) != (book_store is None):
            raise ValueError("BidAsk quote type and raw store must both be provided")
        self.session_id = session_id or str(uuid4())
        self.queue: Queue[tuple[str, Any, Any, datetime]] = Queue(maxsize=queue_capacity)
        self.stop_requested = threading.Event()
        self.worker: threading.Thread | None = None
        self.active: list[tuple[Any, Subscription, Any]] = []
        self.seq = 0
        self.state = "DISABLED"
        self.counters: Counter[str] = Counter()
        self.last_error = ""
        self.last_received_at: str | None = None
        self.last_event_at: str | None = None
        self._fatal = False
        self.transport = QuoteTransportAudit(self.store.root, self.session_id)
        self.quote_event_callback_registered = False
        self._lease_context = None

    def _on_tick(self, exchange: Any, tick: Any) -> None:
        """SDK callback: bounded, nonblocking, no disk writes."""
        if self.stop_requested.is_set() or self.state in ("CLOSED", "FAILED"):
            self.counters["late_callback"] += 1
            return
        received_at = datetime.now(timezone.utc)
        # Provider memory can be reused after returning from a callback; snapshot
        # just the fields required for raw audit, then enqueue without blocking.
        snapshot = SimpleNamespace(**{key: getattr(tick, key, None) for key in FIELDS})
        try:
            self.queue.put_nowait(("tick", exchange, snapshot, received_at))
        except Full:
            self.counters["queue_dropped"] += 1

    def _on_bidask(self, exchange: Any, book: Any) -> None:
        """Same bounded worker and SDK session as Tick; freeze provider level arrays."""
        if self.stop_requested.is_set() or self.state in ("CLOSED", "FAILED"):
            self.counters["late_callback"] += 1
            return
        stamp = datetime.now(timezone.utc)
        snapshot = {}
        for field in BOOK_FIELDS:
            value = getattr(book, field, None)
            if field in ("bid_price", "bid_volume", "ask_price", "ask_volume",
                         "diff_bid_vol", "diff_ask_vol") and value is not None:
                value = tuple(value)
            snapshot[field] = value
        try:
            self.queue.put_nowait(("bidask", exchange, SimpleNamespace(**snapshot), stamp))
        except Full:
            self.counters["queue_dropped"] += 1
            self.counters["book_queue_dropped"] += 1

    def _on_quote_event(self, response_code: int, event_code: int, info: str, event: str) -> None:
        """SDK event callback does not resubscribe, block or persist on callback thread."""
        if self.stop_requested.is_set() or self.state in ("FAILED", "CLOSED"):
            return
        try:
            code = int(event_code)
            response = int(response_code)
            if code not in (0, 1, 12, 13):
                return
            self._enqueue_transport_event(response, code, datetime.now(timezone.utc))
        except (ValueError, TypeError):
            self.counters["malformed_transport_event"] += 1
            self.state = "DEGRADED"

    def _enqueue_transport_event(self, response: int, code: int, received: datetime) -> None:
        try:
            self.queue.put_nowait(self._transport_queue_item(response, code, received))
        except Full:
            self.counters["transport_event_dropped"] += 1
            self.state = "DEGRADED"

    def _handle_transport_event(self, response: int, code: int, received: datetime) -> None:
        try:
            should_recover = self.transport.apply(response, code, received)
        except (OSError, ValueError) as exc:
            self.counters["transport_audit_error"] += 1
            self.last_error = f"transport_audit: {type(exc).__name__}"
            self.state = "DEGRADED"
            return
        if code in (1, 12):
            self.state = "DEGRADED"
            self.counters["transport_gap_events"] += 1
        if should_recover and not self.stop_requested.is_set():
            ok = True
            for contract, sub, quote_type in list(self._active_topics()):
                try:
                    # No login/CA/order. SDK already handles transport reconnect;
                    # we refresh subscriptions ONLY after event 13.
                    self.api.subscribe(contract, quote_type=quote_type,
                                       intraday_odd=(sub.lot_type == "ODD"))
                except Exception as exc:
                    ok = False
                    self.counters["resubscribe_error"] += 1
                    self.last_error = f"resubscribe: {type(exc).__name__}"
                    break
            self.transport.recovered(ok)
            self.counters["resubscribe_success" if ok else "resubscribe_failed"] += 1
            # Reconnected data still has an unfillable gap until separately reviewed.
            self.state = "DEGRADED"

    def _active_topics(self):
        raise NotImplementedError

    def _transport_queue_item(self, response: int, code: int, received: datetime):
        raise NotImplementedError

    def _contract(self, symbol: str) -> Any:
        contracts = getattr(self.api, "contracts", None)
        getter = getattr(contracts, "get", None)
        if callable(getter):
            contract = getter(symbol)
        else:
            contract = self.api.Contracts.Stocks[symbol]
        if contract is None:
            raise ValueError(f"missing contract: {symbol}")
        return contract

    def start(self) -> None:
        if self.state != "DISABLED":
            raise RuntimeError("collector is not disabled")
        if not self.subscriptions:
            raise ValueError("empty subscriptions")
        if not callable(getattr(self.api, "on_tick_stk_v1", None)):
            raise TypeError("Tick callback API unavailable")
        if self.book_quote_type is not None and not callable(getattr(self.api, "on_bidask_stk_v1", None)):
            raise TypeError("BidAsk callback API unavailable")
        self.state = "CONNECTING"
        try:
            self._lease_context = collector_lease(self.store.root)
            self._lease_context.__enter__()
            self.quote_event_callback_registered = register_quote_events(self.api, self._on_quote_event)
            self.api.on_tick_stk_v1()(self._on_tick)
            if self.book_quote_type is not None:
                self.api.on_bidask_stk_v1()(self._on_bidask)
            for sub in self.subscriptions:
                contract = self._contract(sub.symbol)
                self.api.subscribe(
                    contract, quote_type=self.quote_type,
                    intraday_odd=(sub.lot_type == "ODD"),
                )
                self.active.append((contract, sub, self.quote_type))
                if self.book_quote_type is not None:
                    self.api.subscribe(
                        contract, quote_type=self.book_quote_type,
                        intraday_odd=(sub.lot_type == "ODD"),
                    )
                    self.active.append((contract, sub, self.book_quote_type))
            self.stop_requested.clear()
            self.state = "HEALTHY"
            self.worker = threading.Thread(target=self._worker_loop, name="intraday-tick-collector", daemon=True)
            self.worker.start()
        except Exception:
            self._unsubscribe_all()
            self._release_lease()
            self.state = "FAILED"
            raise

    def _active_topics(self):
        return list(self.active)

    def _transport_queue_item(self, response: int, code: int, received: datetime):
        return ("transport", response, code, received)

    def _release_lease(self) -> None:
        if self._lease_context is not None:
            self._lease_context.__exit__(None, None, None)
            self._lease_context = None

    def _unsubscribe_all(self) -> None:
        for contract, sub, quote_type in reversed(self.active):
            try:
                self.api.unsubscribe(
                    contract, quote_type=quote_type,
                    intraday_odd=(sub.lot_type == "ODD"),
                )
            except Exception as exc:
                self.counters["unsubscribe_error"] += 1
                self.last_error = f"unsubscribe: {type(exc).__name__}"
        self.active.clear()

    def _worker_loop(self) -> None:
        try:
            while not self.stop_requested.is_set() or not self.queue.empty():
                try:
                    stream, exchange, payload, received_at = self.queue.get(timeout=0.15)
                except Empty:
                    continue
                try:
                    if stream == "transport":
                        self._handle_transport_event(exchange, payload, received_at)
                    elif stream == "tick":
                        self.process(exchange, payload, received_at=received_at)
                    else:
                        self.process_book(exchange, payload, received_at=received_at)
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self._fatal = True
            self.state = "FAILED"
            self.counters["worker_crashed"] += 1
            self.last_error = f"worker: {type(exc).__name__}"

    def process(self, exchange: Any, tick: Any, *, received_at: datetime) -> MarketTick | None:
        """Public for deterministic fake-SDK testing. Never called by SDK directly."""
        symbol = str(getattr(tick, "code", "") or "")
        lot = "ODD" if getattr(tick, "intraday_odd", False) is True else "BOARD"
        # Unparseable SDK events still go to a raw audit bucket; ignore only
        # well-formed but unsubscribed symbols (another listener can share the callback).
        if symbol and symbol.isascii() and symbol.isalnum() and len(symbol) <= 12:
            if (symbol, lot) not in self.scope_keys:
                self.counters["outside_scope"] += 1
                return None
        try:
            record = raw_event(
                exchange, tick, session_id=self.session_id,
                seq=self.seq, received_at=received_at,
            )
            self.store.append(record)
        except Exception as exc:
            self.counters["raw_write_error"] += 1
            self.last_error = f"raw: {type(exc).__name__}"
            self.state = "DEGRADED"
            return None
        self.seq += 1
        self.counters["raw_written"] += 1
        self.last_received_at = record["received_at"]
        normalized, reason = normalize_raw_tick(record)
        if normalized is None:
            self.counters[f"rejected_{reason}"] += 1
            return None
        self.last_event_at = normalized.event_time
        self.counters["accepted"] += 1
        if self.sink is not None:
            try:
                self.sink(normalized)
            except Exception as exc:
                self.counters["sink_error"] += 1
                self.last_error = f"sink: {type(exc).__name__}"
                self.state = "DEGRADED"
        return normalized

    def process_book(self, exchange: Any, bidask: Any, *, received_at: datetime) -> MarketBook | None:
        """Handle one captured book; raw-first, no book inference or order actions."""
        if self.book_store is None:
            raise RuntimeError("BidAsk collection not configured")
        symbol = str(getattr(bidask, "code", "") or "")
        lot = "ODD" if getattr(bidask, "intraday_odd", False) is True else "BOARD"
        if symbol and symbol.isascii() and symbol.isalnum() and len(symbol) <= 12:
            if (symbol, lot) not in self.scope_keys:
                self.counters["book_outside_scope"] += 1
                return None
        try:
            record = raw_book_event(exchange, bidask, session_id=self.session_id,
                                    seq=self.seq, received_at=received_at)
            self.book_store.append(record)
        except Exception as exc:
            self.counters["book_raw_write_error"] += 1
            self.last_error = f"book_raw: {type(exc).__name__}"
            self.state = "DEGRADED"
            return None
        self.seq += 1
        self.counters["book_raw_written"] += 1
        self.last_received_at = record["received_at"]
        book, reason = normalize_raw_book(record)
        if book is None:
            self.counters[f"book_rejected_{reason}"] += 1
            return None
        self.last_event_at = book.event_time
        self.counters["book_accepted"] += 1
        if self.book_sink is not None:
            try:
                self.book_sink(book)
            except Exception as exc:
                self.counters["book_sink_error"] += 1
                self.last_error = f"book_sink: {type(exc).__name__}"
                self.state = "DEGRADED"
        return book

    def health(self) -> dict[str, Any]:
        state = self.state
        if self._fatal:
            state = "FAILED"
        elif state == "HEALTHY" and any(self.counters[k] for k in (
            "queue_dropped", "raw_write_error", "sink_error", "unsubscribe_error",
            "book_raw_write_error", "book_sink_error", "book_queue_dropped"
        )):
            state = "DEGRADED"
        if self.transport.gap_unresolved or self.counters["transport_event_dropped"]:
            if state == "HEALTHY":
                state = "DEGRADED"
        return {
            "state": state,
            "quote_event_callback_registered": self.quote_event_callback_registered,
            **self.transport.health(),
            "session_id": self.session_id,
            "subscriptions": len(self.subscriptions) * (2 if self.book_quote_type is not None else 1),
            "bidask_enabled": self.book_quote_type is not None,
            "queue_depth": self.queue.qsize(),
            "counters": dict(self.counters),
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_received_at": self.last_received_at,
        }

    def stop(self, *, join_timeout: float = 10.0) -> dict[str, Any]:
        if self.state == "DISABLED":
            return self.health()
        self.stop_requested.set()
        self._unsubscribe_all()
        if self.worker is not None:
            self.worker.join(timeout=join_timeout)
            if self.worker.is_alive():
                self._fatal = True
                self.counters["worker_stop_timeout"] += 1
                self.last_error = "worker_stop_timeout"
            else:
                self.worker = None
        # Keep lease held if shutdown timed out: rotating raw while its worker
        # is still active could silently truncate a tail.
        if self.worker is None:
            self._release_lease()
        result = self.health()
        if result["state"] not in ("FAILED", "DEGRADED"):
            self.state = "CLOSED"
        return self.health()


def write_health(path: Path, health: dict[str, Any]) -> None:
    """Atomic snapshot for a future read-only dashboard."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(health, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
