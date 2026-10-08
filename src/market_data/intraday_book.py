"""Read-only five-level BidAsk: immutable canonical snapshots, raw audit, replay.

Never treat visible orders as executions or infer cancellations from volume changes.
No SDK imports, credentials, trading API or live broker side effects.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .intraday_tick import (
    EXCHANGES, LOT_TYPES, TAIPEI, VERSION, _iso_timestamp, _price, _volume,
    iter_raw, tick_path,
)

BOOK_FIELDS = (
    "code", "datetime", "date", "time", "bid_price", "bid_volume",
    "ask_price", "ask_volume", "diff_bid_vol", "diff_ask_vol",
    "intraday_odd", "suspend", "simtrade",
)


@dataclass(frozen=True)
class MarketBook:
    schema_version: int
    symbol: str
    exchange: str
    lot_type: str
    event_time: str
    received_at: str
    collector_session_id: str
    collection_seq: int
    bid_prices_x10000: tuple[int | None, ...]
    bid_volumes_shares: tuple[int | None, ...]
    ask_prices_x10000: tuple[int | None, ...]
    ask_volumes_shares: tuple[int | None, ...]
    source: str = "shioaji_bidask"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def book_path(root: Path, trading_date: str, lot_type: str, symbol: str) -> Path:
    # Reuse PR-1 path validator; BidAsk must never share Tick raw files.
    tick = tick_path(root, trading_date, lot_type, symbol)
    return tick.parent.parent.parent.parent / "bidask" / trading_date / lot_type / tick.name


def _json_field(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_json_field(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def raw_book_event(
    exchange: Any, bidask: Any, *, session_id: str, seq: int,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot the provider's five levels and deltas for audit; deltas are not trade facts."""
    payload = {field: _json_field(getattr(bidask, field, None)) for field in BOOK_FIELDS}
    stamp = received_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    timestamp = payload["datetime"]
    if timestamp is None and payload["date"] is not None and payload["time"] is not None:
        timestamp = f"{payload['date']}T{payload['time']}"
    try:
        event_time = _iso_timestamp(timestamp)
    except (TypeError, ValueError):
        event_time = None
    trading_date = (
        datetime.fromisoformat(event_time).date().isoformat()
        if event_time else stamp.astimezone(TAIPEI).date().isoformat()
    )
    return {
        "schema_version": VERSION, "source": "shioaji", "quote_type": "BidAsk",
        "exchange": str(getattr(exchange, "value", exchange)),
        "trading_date": trading_date,
        "lot_type": "ODD" if payload["intraday_odd"] is True else "BOARD",
        "collector_session_id": str(session_id), "collection_seq": int(seq),
        "event_time": event_time,
        "received_at": stamp.astimezone(timezone.utc).isoformat(),
        "payload": payload,
    }


def _side(prices: Any, volumes: Any, lot: str, *, descending: bool):
    if not isinstance(prices, list) or not isinstance(volumes, list):
        raise ValueError("side_not_list")
    if len(prices) != len(volumes) or len(prices) > 5:
        raise ValueError("side_length")
    result_price: list[int | None] = []
    result_volume: list[int | None] = []
    valid: list[int] = []
    missing_seen = False
    for p, v in zip(prices, volumes):
        # Unfilled level must be represented as null, not a fictitious price/quantity.
        if p in (None, "", 0, "0") and v in (None, "", 0, "0"):
            result_price.append(None)
            result_volume.append(None)
            missing_seen = True
            continue
        if missing_seen:
            raise ValueError("noncontiguous_levels")
        px = _price(p)
        vol = _volume(v, lot)
        result_price.append(px)
        result_volume.append(vol)
        valid.append(px)
    if len(set(valid)) != len(valid):
        raise ValueError("duplicate_level_price")
    for previous, current in zip(valid, valid[1:]):
        if (previous <= current) if descending else (previous >= current):
            raise ValueError("unsorted_levels")
    while len(result_price) < 5:
        result_price.append(None)
        result_volume.append(None)
    return tuple(result_price), tuple(result_volume)


def normalize_raw_book(record: Mapping[str, Any]) -> tuple[MarketBook | None, str]:
    try:
        if record["schema_version"] != VERSION or record["quote_type"] != "BidAsk":
            return None, "schema_invalid"
        payload = record["payload"]
        if not isinstance(payload, dict):
            return None, "schema_invalid"
        if payload.get("simtrade"):
            return None, "simtrade"
        if payload.get("suspend"):
            return None, "suspend"
        symbol = payload.get("code")
        if not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum() or not 1 <= len(symbol) <= 12:
            return None, "symbol_invalid"
        exchange = record["exchange"]
        if exchange not in EXCHANGES:
            return None, "exchange_invalid"
        lot = "ODD" if payload.get("intraday_odd") is True else "BOARD"
        if record["lot_type"] != lot:
            return None, "lot_mismatch"
        event_time = payload.get("datetime")
        if event_time is None and payload.get("date") and payload.get("time"):
            event_time = f"{payload['date']}T{payload['time']}"
        event_time = _iso_timestamp(event_time)
        if record["trading_date"] != datetime.fromisoformat(event_time).date().isoformat():
            return None, "date_mismatch"
        receipt = datetime.fromisoformat(str(record["received_at"]).replace("Z", "+00:00"))
        if receipt.tzinfo is None:
            return None, "received_at_missing_timezone"
        bp, bv = _side(payload.get("bid_price"), payload.get("bid_volume"), lot, descending=True)
        ap, av = _side(payload.get("ask_price"), payload.get("ask_volume"), lot, descending=False)
        if bp[0] is not None and ap[0] is not None and bp[0] >= ap[0]:
            return None, "crossed_book"
        return MarketBook(
            VERSION, symbol, exchange, lot, event_time,
            receipt.astimezone(timezone.utc).isoformat(),
            str(record["collector_session_id"]), int(record["collection_seq"]),
            bp, bv, ap, av,
        ), ""
    except ValueError as exc:
        detail = str(exc)
        if detail in ("side_not_list", "side_length", "noncontiguous_levels",
                      "duplicate_level_price", "unsorted_levels"):
            return None, detail
        return None, "needs_review"
    except (KeyError, TypeError, OverflowError, InvalidOperation):
        return None, "needs_review"


def book_metrics(book: MarketBook) -> dict[str, Any]:
    """Only visible top-five liquidity; no market intent or trade side inference."""
    bid_volume = sum(x for x in book.bid_volumes_shares if x is not None)
    ask_volume = sum(x for x in book.ask_volumes_shares if x is not None)
    spread = (book.ask_prices_x10000[0] - book.bid_prices_x10000[0]
              if book.bid_prices_x10000[0] is not None and book.ask_prices_x10000[0] is not None
              else None)
    volume_total = bid_volume + ask_volume
    # A missing *side* is unknown liquidity, not a zero-volume promise.
    # Never report 100% bid/ask imbalance on an incomplete one-sided book.
    both_sides_present = (book.bid_prices_x10000[0] is not None and
                          book.ask_prices_x10000[0] is not None)
    return {
        "bid_visible_shares": bid_volume, "ask_visible_shares": ask_volume,
        "spread_x10000": spread,
        "imbalance_bps": (bid_volume * 10000 // volume_total
                          if volume_total and both_sides_present else None),
        "coverage_bid_levels": sum(x is not None for x in book.bid_prices_x10000),
        "coverage_ask_levels": sum(x is not None for x in book.ask_prices_x10000),
    }


class RawBookStore:
    def __init__(self, root: Path, *, stop_at_disk_pct: float = 75.0):
        if not 0 < stop_at_disk_pct < 100:
            raise ValueError("invalid disk watermark")
        self.root = Path(root)
        self.stop_at_disk_pct = stop_at_disk_pct

    def append(self, record: Mapping[str, Any]) -> Path:
        path = book_path(
            self.root, str(record["trading_date"]), str(record["lot_type"]),
            str(record["payload"].get("code") or "_invalid"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(path.parent)
        if disk.used / disk.total * 100 >= self.stop_at_disk_pct:
            raise OSError("collector disk watermark exceeded")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
        return path


def replay_books(path: Path) -> tuple[list[MarketBook], dict[str, int]]:
    books: list[MarketBook] = []
    rejected: dict[str, int] = {}
    for record in iter_raw(path):
        book, reason = normalize_raw_book(record)
        if book is None:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            books.append(book)
    return books, rejected
