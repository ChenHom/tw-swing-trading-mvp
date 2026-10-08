#!/usr/bin/env python3
"""Offline, explicitly invoked publisher for the read-only /trading/intraday page.

No broker login. Does not start services or schedule any collection.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.application.services.intraday_dashboard import make_snapshot, write_snapshot
from src.market_data.intraday_book import replay_books
from src.market_data.intraday_observations import ObservationEvent
from src.market_data.intraday_tick import replay_ticks


def run(*, ticks_file: Path, books_file: Path, health_file: Path,
        output: Path, observations_file: Path | None = None) -> dict:
    ticks, tick_reject = replay_ticks(ticks_file)
    books, book_reject = replay_books(books_file)
    if tick_reject or book_reject:
        raise ValueError(f"unvalidated raw feed; tick={tick_reject}, book={book_reject}")
    health = json.loads(health_file.read_text(encoding="utf-8"))
    if not isinstance(health, dict):
        raise ValueError("health must be a JSON object")
    events = []
    if observations_file:
        source = json.loads(observations_file.read_text(encoding="utf-8"))
        if not isinstance(source, dict) or not isinstance(source.get("observations"), list):
            raise ValueError("invalid observation report")
        events = [ObservationEvent(**e) for e in source["observations"]]
    snapshot = make_snapshot(
        ticks=ticks, books=books, observations=events,
        collector_health=health, generated_at=datetime.now(timezone.utc).isoformat(),
    )
    write_snapshot(output, snapshot)
    return {"file": str(output), "symbols": len(snapshot["symbols"]),
            "collector_state": health.get("state", "UNKNOWN"),
            "mode": "OFFLINE_PUBLICATION_ONLY"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Build inert snapshot; never connect to broker")
    p.add_argument("--ticks", required=True, type=Path)
    p.add_argument("--books", required=True, type=Path)
    p.add_argument("--health", required=True, type=Path)
    p.add_argument("--observations", type=Path)
    p.add_argument("--output", type=Path, default=Path("data/intraday/dashboard.json"))
    a = p.parse_args()
    print(json.dumps(run(ticks_file=a.ticks, books_file=a.books,
                         health_file=a.health, observations_file=a.observations,
                         output=a.output), ensure_ascii=False))
