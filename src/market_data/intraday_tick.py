"""Read-only intraday Tick model, raw audit store and deterministic replay.

This module deliberately has no Shioaji import, login or broker dependency.
Board-lot Tick volume is lots (x1000 shares); odd-lot Tick volume is shares.
Raw events are authoritative; no synthetic ticks/bars are generated.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
VERSION = 1
FIELDS = (
    "code", "datetime", "date", "time", "close", "volume", "total_volume",
    "tick_type", "intraday_odd", "suspend", "simtrade",
)
EXCHANGES = frozenset(("TSE", "OTC"))
LOT_TYPES = frozenset(("BOARD", "ODD"))


@dataclass(frozen=True)
class MarketTick:
    schema_version: int
    symbol: str
    exchange: str
    lot_type: str
    event_time: str
    received_at: str
    price_x10000: int
    trade_volume_shares: int
    cumulative_volume_shares: int | None
    tick_type: int | None
    collector_session_id: str
    collection_seq: int
    source: str = "shioaji_tick"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("missing or invalid event datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(TAIPEI).isoformat()


def _price(value: Any) -> int:
    try:
        price = Decimal(str(value))
        scaled = price * 10000
        if not price.is_finite() or scaled != scaled.to_integral_value() or price <= 0:
            raise ValueError("bad tick price")
        return int(scaled)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("bad tick price") from exc


def _volume(value: Any, lot_type: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing tick volume")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value() or number < 0:
            raise ValueError("invalid tick volume")
        return int(number) * (1000 if lot_type == "BOARD" else 1)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid tick volume") from exc


def normalize_raw_tick(record: Mapping[str, Any]) -> tuple[MarketTick | None, str]:
    """Normalize one persisted raw envelope; rejected events remain in the raw log."""
    try:
        if record["schema_version"] != VERSION or record["quote_type"] != "Tick":
            return None, "schema_invalid"
        raw = record["payload"]
        if not isinstance(raw, dict):
            return None, "schema_invalid"
        if raw.get("simtrade"):
            return None, "simtrade"
        if raw.get("suspend"):
            return None, "suspend"
        symbol = str(raw.get("code") or "")
        if not symbol or not symbol.isascii() or not symbol.isalnum() or len(symbol) > 12:
            return None, "symbol_invalid"
        lot_type = "ODD" if raw.get("intraday_odd") is True else "BOARD"
        if lot_type != record["lot_type"]:
            return None, "lot_mismatch"
        exchange = str(record["exchange"])
        if exchange not in EXCHANGES:
            return None, "exchange_invalid"
        event_time = raw.get("datetime")
        if not event_time and raw.get("date") and raw.get("time"):
            event_time = f"{raw['date']}T{raw['time']}"
        event_time = _iso_timestamp(event_time)
        received_at = _iso_timestamp(record["received_at"])
        if datetime.fromisoformat(event_time).date().isoformat() != record["trading_date"]:
            return None, "date_mismatch"
        price = _price(raw.get("close"))
        amount = _volume(raw.get("volume"), lot_type)
        total = raw.get("total_volume")
        cumulative = None if total is None else _volume(total, lot_type)
        kind = raw.get("tick_type")
        kind = None if kind is None else int(kind)
        return MarketTick(
            schema_version=VERSION, symbol=symbol, exchange=exchange, lot_type=lot_type,
            event_time=event_time, received_at=received_at,
            price_x10000=price, trade_volume_shares=amount,
            cumulative_volume_shares=cumulative, tick_type=kind,
            collector_session_id=str(record["collector_session_id"]),
            collection_seq=int(record["collection_seq"]),
        ), ""
    except (KeyError, ValueError, TypeError, OverflowError):
        return None, "needs_review"


def raw_event(
    exchange: str,
    payload: Any,
    *,
    session_id: str,
    seq: int,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot SDK callback fields into serializable primitives without importing SDK."""
    fields: dict[str, Any] = {}
    for key in FIELDS:
        item = getattr(payload, key, None)
        fields[key] = None if item is None else (
            item if isinstance(item, (bool, int, float, str)) else str(item)
        )
    stamp = received_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("received_at must be timezone aware")
    odd = fields["intraday_odd"] is True
    when = _iso_timestamp(fields.get("datetime") or (
        f"{fields['date']}T{fields['time']}" if fields.get("date") and fields.get("time") else None
    ))
    return {
        "schema_version": VERSION,
        "source": "shioaji",
        "quote_type": "Tick",
        "exchange": str(getattr(exchange, "value", exchange)),
        "trading_date": datetime.fromisoformat(when).date().isoformat(),
        "lot_type": "ODD" if odd else "BOARD",
        "collector_session_id": session_id,
        "collection_seq": seq,
        "event_time": when,
        "received_at": stamp.astimezone(timezone.utc).isoformat(),
        "payload": fields,
    }


def tick_path(root: Path, trading_date: str, lot_type: str, symbol: str) -> Path:
    """Ensure paths never escape the configured storage root."""
    date.fromisoformat(trading_date)
    if lot_type not in LOT_TYPES or not symbol.isascii() or not symbol.isalnum() or len(symbol) > 12:
        raise ValueError("invalid tick path")
    return root / "shioaji" / "ticks" / trading_date / lot_type / f"{symbol}.jsonl"


class RawTickStore:
    """Append-only raw writer; per-event flush makes loss explicit at worker boundary."""

    def __init__(self, root: Path, *, stop_at_disk_pct: float = 75.0):
        if not 0 < stop_at_disk_pct < 100:
            raise ValueError("invalid disk watermark")
        self.root = Path(root)
        self.stop_at_disk_pct = stop_at_disk_pct

    def append(self, record: Mapping[str, Any]) -> Path:
        path = tick_path(
            self.root, str(record["trading_date"]), str(record["lot_type"]),
            str(record["payload"]["code"]),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path.parent)
        if usage.used / usage.total * 100 >= self.stop_at_disk_pct:
            raise OSError("collector disk watermark exceeded")
        # One complete JSON record per append; never overwrite an earlier event.
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return path


def iter_raw(path: Path) -> Iterable[dict[str, Any]]:
    """Read JSONL and JSONL.gz. A truncated tail is an error, not silently skipped."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"empty raw line {line_no}")
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"invalid raw line {line_no}")
            yield record


def replay_ticks(path: Path) -> tuple[list[MarketTick], dict[str, int]]:
    ticks: list[MarketTick] = []
    rejected: dict[str, int] = {}
    for record in iter_raw(path):
        tick, reason = normalize_raw_tick(record)
        if tick is not None:
            ticks.append(tick)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    return ticks, rejected


def build_minute_bars(ticks: Iterable[MarketTick]) -> list[dict[str, Any]]:
    """Deterministic 1m bars, no padding. Preserve separate BOARD / ODD liquidity."""
    groups: dict[tuple[str, str, str, str], list[MarketTick]] = {}
    for tick in ticks:
        minute = tick.event_time[:16]
        key = (tick.symbol, tick.exchange, tick.lot_type, minute)
        groups.setdefault(key, []).append(tick)
    bars = []
    for (symbol, exchange, lot_type, minute), rows in sorted(groups.items()):
        rows.sort(key=lambda t: (t.event_time, t.collection_seq))
        prices = [t.price_x10000 for t in rows]
        bars.append({
            "symbol": symbol, "exchange": exchange, "lot_type": lot_type,
            "start_at": minute, "timeframe": "1m",
            "open_x10000": prices[0], "high_x10000": max(prices),
            "low_x10000": min(prices), "close_x10000": prices[-1],
            "volume_shares": sum(t.trade_volume_shares for t in rows),
            "trade_count": len(rows), "source": "shioaji_tick_replay",
        })
    return bars


def compress_raw(path: Path) -> dict[str, Any]:
    """Crash-safe compression; retain original on failure. Never replace an existing archive."""
    path = Path(path)
    if path.suffix != ".jsonl":
        raise ValueError("expected .jsonl file")
    target = Path(str(path) + ".gz")
    if target.exists():
        raise FileExistsError(str(target))
    temp = Path(str(target) + ".tmp")
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as source, temp.open("xb") as dst:
            with gzip.GzipFile(fileobj=dst, mode="wb", filename="", mtime=0) as archive:
                for line in source:
                    digest.update(line)
                    count += 1
                    archive.write(line)
            dst.flush()
            os.fsync(dst.fileno())
        if target.exists():
            raise FileExistsError(str(target))
        temp.rename(target)
        path.unlink()
    finally:
        temp.unlink(missing_ok=True)
    return {"archive": str(target), "raw_sha256": digest.hexdigest(), "lines": count}
