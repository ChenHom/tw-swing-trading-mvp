"""Offline BidAsk replay and evidence-only observation reporting.

No Shioaji import, credentials, trading database access, or live collection.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.market_data.intraday_book import book_metrics, replay_books
from src.market_data.intraday_observations import ObservationPlan, replay_observations
from src.market_data.intraday_tick import replay_ticks


def cmd_intraday_book_replay(args) -> None:
    books, rejected_books = replay_books(Path(args.books))
    ticks = []
    rejected_ticks: dict[str, int] = {}
    if args.ticks:
        ticks, rejected_ticks = replay_ticks(Path(args.ticks))
    plan = None
    if args.plan:
        payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("observation plan must be a JSON object")
        plan = ObservationPlan(**payload)
        if not args.ticks:
            raise ValueError("plan evaluation requires --ticks")
    events = replay_observations(
        ticks=ticks, books=books, plan=plan, data_health=args.data_health,
    )
    result = {
        "schema_version": 1, "mode": "OFFLINE_ONLY",
        "book_count": len(books), "tick_count": len(ticks),
        "rejected_books": rejected_books, "rejected_ticks": rejected_ticks,
        "metrics": [{"symbol": book.symbol, "lot_type": book.lot_type,
                     "event_time": book.event_time, "metrics": book_metrics(book)}
                    for book in books],
        "observations": [event.as_dict() for event in events],
    }
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
