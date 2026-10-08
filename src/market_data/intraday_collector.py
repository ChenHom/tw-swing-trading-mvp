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
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

from .intraday_tick import (
    LOT_TYPES, MarketTick, RawTickStore, normalize_raw_tick, raw_event,
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
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.api = api
        self.subscriptions = tuple(subscriptions)
        if len(set(self.subscriptions)) != len(self.subscriptions):
            raise ValueError("duplicate subscriptions")
        self.scope = set(self.subscriptions)
        self.quote_type = quote_type
        self.store = store
        self.sink = sink
        self.session_id = session_id or str(uuid4())
        self.queue: Queue[tuple[Any, Any, datetime]] = Queue(maxsize=queue_capacity)
        self.stop_requested = threading.Event()
        self.worker: threading.Thread | None = None
        self.active: list[tuple[Any, Subscription]] = []
        self.seq = 0
        self.state = "DISABLED"
        self.counters: Counter[str] = Counter()
        self.last_error = ""
        self.last_received_at: str | None = None
        self.last_event_at: str | None = None
        self._fatal = False

    def _on_tick(self, exchange: Any, tick: Any) -> None:
        """SDK callback: bounded, nonblocking, no disk writes."""
        received_at = datetime.now(timezone.utc)
        try:
            self.queue.put_nowait((exchange, tick, received_at))
        except Full:
            self.counters["queue_dropped"] += 1

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
        self.state = "CONNECTING"
        try:
            self.api.on_tick_stk_v1()(self._on_tick)
            for sub in self.subscriptions:
                contract = self._contract(sub.symbol)
                self.api.subscribe(
                    contract, quote_type=self.quote_type,
                    intraday_odd=(sub.lot_type == "ODD"),
                )
                self.active.append((contract, sub))
            self.stop_requested.clear()
            self.state = "HEALTHY"
            self.worker = threading.Thread(target=self._worker_loop, name="intraday-tick-collector", daemon=True)
            self.worker.start()
        except Exception:
            self._unsubscribe_all()
            self.state = "FAILED"
            raise

    def _unsubscribe_all(self) -> None:
        for contract, sub in reversed(self.active):
            try:
                self.api.unsubscribe(
                    contract, quote_type=self.quote_type,
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
                    exchange, tick, received_at = self.queue.get(timeout=0.15)
                except Empty:
                    continue
                try:
                    self.process(exchange, tick, received_at=received_at)
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
            if (symbol, lot) not in {(s.symbol, s.lot_type) for s in self.scope}:
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

    def health(self) -> dict[str, Any]:
        state = self.state
        if self._fatal:
            state = "FAILED"
        elif state == "HEALTHY" and any(self.counters[k] for k in (
            "queue_dropped", "raw_write_error", "sink_error", "unsubscribe_error"
        )):
            state = "DEGRADED"
        return {
            "state": state,
            "session_id": self.session_id,
            "subscriptions": len(self.subscriptions),
            "queue_depth": self.queue.qsize(),
            "counters": dict(self.counters),
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_received_at": self.last_received_at,
        }

    def stop(self, *, join_timeout: float = 10.0) -> dict[str, Any]:
        if self.state == "DISABLED":
            return self.health()
        self._unsubscribe_all()
        self.stop_requested.set()
        if self.worker is not None:
            self.worker.join(timeout=join_timeout)
            if self.worker.is_alive():
                self._fatal = True
                self.counters["worker_stop_timeout"] += 1
                self.last_error = "worker_stop_timeout"
            else:
                self.worker = None
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
