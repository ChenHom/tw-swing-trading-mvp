"""Opt-in snapshot projector fed by an existing read-only Collector.

No broker login, new quote subscription, trade API, scheduler, or Web-side writes.
Collector callbacks normalize and persist first; this projector receives only
accepted canonical objects from the existing sink/book_sink on the worker.
The CLI's control thread publishes atomic snapshots at bounded intervals.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.market_data.intraday_book import MarketBook
from src.market_data.intraday_tick import MarketTick
from .intraday_dashboard import make_snapshot, write_snapshot


class IntradaySnapshotPublisher:
    def __init__(self, output: Path, *, interval_seconds: float = 5.0) -> None:
        if not 1 <= interval_seconds <= 30:
            raise ValueError("snapshot interval must be 1..30 seconds")
        output = Path(output)
        if output.suffix != ".json":
            raise ValueError("snapshot destination must have .json extension")
        self.output = output
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._ticks: dict[tuple[str, str, str], MarketTick] = {}
        self._books: dict[tuple[str, str, str], MarketBook] = {}
        self._last_published_monotonic: float | None = None

    def on_tick(self, tick: MarketTick) -> None:
        key = (tick.symbol, tick.exchange, tick.lot_type)
        with self._lock:
            previous = self._ticks.get(key)
            if previous is None or tick.received_at >= previous.received_at:
                self._ticks[key] = tick

    def on_book(self, book: MarketBook) -> None:
        key = (book.symbol, book.exchange, book.lot_type)
        with self._lock:
            previous = self._books.get(key)
            if previous is None or book.received_at >= previous.received_at:
                self._books[key] = book

    def publish(self, health: Mapping[str, Any], *, force: bool = False,
                at: datetime | None = None) -> bool:
        """Return False if throttled; no SDK or raw-store IO is performed."""
        now_monotonic = time.monotonic()
        if (not force and self._last_published_monotonic is not None and
                now_monotonic - self._last_published_monotonic < self.interval_seconds):
            return False
        session_id = health.get("session_id")
        # Snapshot only the active epoch. A gap never combines pre/post
        # reconnection quotes into one apparently consistent book.
        with self._lock:
            ticks = [v for v in self._ticks.values()
                     if v.collector_session_id == session_id]
            books = [v for v in self._books.values()
                     if v.collector_session_id == session_id]
        stamp = at or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise ValueError("snapshot publication needs timezone-aware time")
        document = make_snapshot(
            ticks=ticks, books=books, collector_health=dict(health),
            generated_at=stamp.isoformat(),
        )
        document["snapshot_mode"] = "READ_ONLY_CAPTURE"
        document["capture_verified_for_live_trading"] = False
        write_snapshot(self.output, document)
        self._last_published_monotonic = now_monotonic
        return True
