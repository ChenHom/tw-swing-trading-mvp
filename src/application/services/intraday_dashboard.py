"""Fail-closed, read-only intraday dashboard snapshots.

Web never imports Shioaji, streams raw broker callbacks or writes the trading DB.
The only accepted input is a versioned locally published snapshot.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from src.market_data.intraday_book import MarketBook, book_metrics
from src.market_data.intraday_observations import ObservationEvent
from src.market_data.intraday_tick import LOT_TYPES, MarketTick

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_PATH = Path("data/intraday/dashboard.json")
STATUS_VALUES = frozenset(("DISABLED", "NO_DATA", "STALE", "CLOSED", "DEGRADED", "UNVERIFIED", "OBSERVING"))


def _iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("offset-aware timestamp required")
    return dt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_snapshot(
    *,
    ticks: Iterable[MarketTick] = (),
    books: Iterable[MarketBook] = (),
    observations: Iterable[ObservationEvent] = (),
    collector_health: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """Convert immutable PR-1/2 events to bounded, same-lot, latest-only read models."""
    _iso(generated_at)
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for tick in ticks:
        key = tick.symbol, tick.exchange, tick.lot_type
        item = rows.setdefault(key, {"symbol": key[0], "exchange": key[1], "lot_type": key[2],
                                      "tick": None, "book": None, "metrics": None, "observations": []})
        if (item["tick"] is None or
                _iso(tick.received_at) >= _iso(item["tick"]["received_at"])):
            item["tick"] = tick.as_dict()
    for book in books:
        key = book.symbol, book.exchange, book.lot_type
        item = rows.setdefault(key, {"symbol": key[0], "exchange": key[1], "lot_type": key[2],
                                      "tick": None, "book": None, "metrics": None, "observations": []})
        if (item["book"] is None or
                _iso(book.received_at) >= _iso(item["book"]["received_at"])):
            item["book"] = book.as_dict()
            item["metrics"] = book_metrics(book)
    for event in observations:
        key = event.symbol, event.exchange, event.lot_type
        if key in rows:
            rows[key]["observations"].append(event.as_dict())
    for item in rows.values():
        item["observations"] = sorted(
            item["observations"], key=lambda e: (e["observed_at"], e["event_id"])
        )[-20:]
    return {
        "schema_version": 1, "generated_at": generated_at,
        "collector_health": dict(collector_health),
        "symbols": [rows[k] for k in sorted(rows)],
        "provenance": "read_only_local_snapshot",
        "not_trade_signal": True,
    }


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    """Atomic publication; only offline producer calls this, never web routes."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                               allow_nan=False), encoding="utf-8")
    temp.replace(output)


def _session_label(now: datetime) -> str:
    """Conservative time filter: holidays still require an independently verified session."""
    local = now.astimezone(TAIPEI)
    if local.weekday() >= 5 or not time(9, 0) <= local.time() < time(13, 30):
        return "MARKET_CLOSED"
    return "SESSION_UNVERIFIED"


def load_snapshot(path: Path = DEFAULT_PATH, *, enabled: bool = False,
                  now: datetime | None = None, max_age_seconds: int = 30) -> dict[str, Any]:
    """Never present cached/unverified/stale quote as a live actionable price."""
    now = now or _now()
    if now.tzinfo is None:
        raise ValueError("now must have timezone")
    skeleton: dict[str, Any] = {
        "schema_version": 1, "status": "DISABLED" if not enabled else "NO_DATA",
        "freshness": "UNAVAILABLE", "market_session": _session_label(now),
        "generated_at": None, "last_heartbeat_at": None,
        "symbols": [], "not_trade_signal": True,
    }
    if not enabled:
        return skeleton
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unexpected snapshot schema")
        items = data["symbols"]
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("invalid symbol list")
        generated = _iso(data["generated_at"])
        if generated > now:
            raise ValueError("snapshot timestamp is in the future")
        health = data["collector_health"]
        if not isinstance(health, dict):
            raise ValueError("missing collector health")
        state = health.get("state", "UNKNOWN")
        if state not in ("HEALTHY", "DEGRADED", "FAILED", "CLOSED"):
            state = "UNKNOWN"
        heartbeat = health.get("last_heartbeat_at")
        hb_valid = False
        if heartbeat:
            try:
                heartbeat_dt = _iso(heartbeat)
                hb_valid = 0 <= (now-heartbeat_dt).total_seconds() <= max_age_seconds
            except (TypeError, ValueError):
                pass
        status = "UNVERIFIED"
        if state == "CLOSED":
            status = "CLOSED"
        elif state in ("DEGRADED", "FAILED"):
            status = "DEGRADED"
        elif (now-generated).total_seconds() > max_age_seconds:
            status = "STALE"
        elif hb_valid and _session_label(now) != "MARKET_CLOSED" and health.get("trading_session_verified") is True:
            status = "OBSERVING"
        clean = []
        for item in items:
            if not isinstance(item, dict) or item.get("lot_type") not in LOT_TYPES:
                continue
            symbol = str(item.get("symbol", ""))
            if not symbol.isascii() or not symbol.isalnum() or len(symbol) > 12:
                continue
            lot = item["lot_type"]
            tick = item.get("tick")
            book = item.get("book")
            # Do not trust a stale Tick or wrong-lot entry just because the snapshot is new.
            if isinstance(tick, dict):
                try:
                    if tick.get("symbol") != symbol or tick.get("lot_type") != lot or (
                        now-_iso(tick["received_at"])).total_seconds() > max_age_seconds:
                        tick = None
                except (KeyError, TypeError, ValueError):
                    tick = None
            else:
                tick = None
            if isinstance(book, dict):
                try:
                    if book.get("symbol") != symbol or book.get("lot_type") != lot or (
                        now-_iso(book["received_at"])).total_seconds() > max_age_seconds:
                        book = None
                except (KeyError, TypeError, ValueError):
                    book = None
            else:
                book = None
            # Even apparently fresh values cannot be labeled live unless session verified.
            if status != "OBSERVING":
                tick = None
                book = None
            events = item.get("observations", [])
            if not isinstance(events, list):
                events = []
            # Keep text as data (Jinja escapes HTML; JavaScript uses textContent).
            clean.append({
                "symbol": symbol, "exchange": item.get("exchange"), "lot_type": lot,
                "tick": tick, "book": book, "metrics": item.get("metrics") if book else None,
                "observations": events[-20:], "freshness": (
                    "OBSERVING" if tick or book else "WAITING_FOR_TICK_OR_BOOK"
                ),
            })
        return {
            **skeleton, "status": status, "generated_at": data["generated_at"],
            "last_heartbeat_at": heartbeat, "symbols": clean,
            "freshness": "VERIFIED" if status == "OBSERVING" else "NOT_CURRENT",
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        skeleton["status"] = "NO_DATA"
        return skeleton


def find_symbol(snapshot: Mapping[str, Any], symbol: str, lot_type: str) -> dict[str, Any] | None:
    if lot_type not in LOT_TYPES or not symbol.isascii() or not symbol.isalnum() or not (1 <= len(symbol) <= 12):
        return None
    for row in snapshot.get("symbols", []):
        if row["symbol"] == symbol and row["lot_type"] == lot_type:
            return row
    return None
