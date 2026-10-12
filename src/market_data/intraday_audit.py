"""Read-only audit of a fixed date/symbol's Shioaji Tick and BidAsk raw files.

Independent of SDK login, trading database, or synthetic market data.
A PASS_RAW_ONLY does NOT verify full exchange feed completeness.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .intraday_book import normalize_raw_book
from .intraday_tick import LOT_TYPES, _price, normalize_raw_tick

STREAMS = ("ticks", "bidask")
LOTS = ("BOARD", "ODD")


def _iter_strict_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as source:
        for number, line in enumerate(source, 1):
            if not line.endswith(b"\n"):
                raise ValueError(f"line_{number}_truncated")
            if not line.strip():
                raise ValueError(f"line_{number}_empty")
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"line_{number}_invalid_json") from exc
            if not isinstance(record, dict):
                raise ValueError(f"line_{number}_not_object")
            yield record


def audit_intraday_raw(root: Path, *, symbol: str, trading_date: str,
                       expected: dict[str, int] | None = None) -> dict[str, Any]:
    """Audit exactly four files with no external calls and no file mutation.

    Optional expected counts are an independently observed capture baseline,
    never presumed to be the number of market events that *should* exist.
    """
    if not symbol or not symbol.isascii() or not symbol.isalnum() or len(symbol) > 12:
        raise ValueError("invalid symbol")
    date.fromisoformat(trading_date)
    if expected is not None:
        if set(expected) != {f"{stream}_{lot}" for stream in STREAMS for lot in LOTS}:
            raise ValueError("expected counts must name exactly four stream/lot keys")
        if any(type(value) is not int or value < 0 for value in expected.values()):
            raise ValueError("expected counts must be nonnegative integers")
    results = {}
    any_errors = False
    any_review = False
    any_missing = False
    for stream in STREAMS:
        for lot in LOTS:
            key = f"{stream}_{lot}"
            base = Path(root) / "shioaji" / stream / trading_date / lot / f"{symbol}.jsonl"
            candidates = [p for p in (base, Path(str(base) + ".gz")) if p.is_file()]
            row = {
                "path": str(candidates[0]) if len(candidates) == 1 else None,
                "raw_lines": 0, "accepted": 0, "rejected": {},
                "errors": [], "warnings": [],
                "sha256": None, "first_event_at": None, "last_event_at": None,
                "first_received_at": None, "last_received_at": None,
                "max_delay_seconds": None,
                "sample_price_x10000": None, "sample_volume_shares": None,
            }
            results[key] = row
            if not candidates:
                row["errors"].append("missing_raw_file")
                any_missing = True
                continue
            if len(candidates) > 1:
                row["errors"].append("conflicting_raw_and_archive")
                any_errors = True
                continue
            path = candidates[0]
            digest = hashlib.sha256()
            last_sequence: dict[str, int] = {}
            rejected = Counter()
            try:
                # Digest archived/compressed bytes as found; no extraction or edits.
                with path.open("rb") as binary:
                    for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                        digest.update(chunk)
                row["sha256"] = digest.hexdigest()
                for number, record in enumerate(_iter_strict_jsonl(path), 1):
                    row["raw_lines"] += 1
                    payload = record.get("payload")
                    if (not isinstance(payload, dict)
                            or str(payload.get("code")) != symbol
                            or record.get("lot_type") != lot
                            or record.get("trading_date") != trading_date
                            or record.get("quote_type") != ("Tick" if stream == "ticks" else "BidAsk")
                            or record.get("source") != "shioaji"):
                        row["errors"].append(f"line_{number}_envelope_mismatch")
                        continue
                    session = record.get("collector_session_id")
                    seq = record.get("collection_seq")
                    if (not isinstance(session, str) or not session
                            or type(seq) is not int or seq < 0):
                        row["errors"].append(f"line_{number}_identity_invalid")
                        continue
                    if session in last_sequence and seq <= last_sequence[session]:
                        row["errors"].append(f"line_{number}_sequence_not_increasing")
                    last_sequence[session] = seq
                    fn = normalize_raw_tick if stream == "ticks" else normalize_raw_book
                    canonical, reason = fn(record)
                    if canonical is None:
                        rejected[reason] += 1
                        continue
                    row["accepted"] += 1
                    event_at = datetime.fromisoformat(canonical.event_time)
                    receipt_at = datetime.fromisoformat(canonical.received_at)
                    delay = (receipt_at - event_at).total_seconds()
                    row["max_delay_seconds"] = max(row["max_delay_seconds"] or delay, delay)
                    if delay < -5:
                        row["warnings"].append(f"line_{number}_provider_clock_ahead")
                    elif delay > 30:
                        row["warnings"].append(f"line_{number}_event_delayed")
                    if row["first_event_at"] is None:
                        row["first_event_at"] = canonical.event_time
                        row["first_received_at"] = canonical.received_at
                    row["last_event_at"] = canonical.event_time
                    row["last_received_at"] = canonical.received_at
                    # Independent arithmetic check against raw SDK volume units:
                    # BOARD=lots*1000, ODD=shares. Do not mix namespaces.
                    factor = 1000 if lot == "BOARD" else 1
                    if stream == "ticks":
                        expected_volume = Decimal(str(payload.get("volume"))) * factor
                        expected_price = _price(payload.get("close"))
                        if (canonical.trade_volume_shares != expected_volume
                                or canonical.price_x10000 != expected_price):
                            row["errors"].append(f"line_{number}_tick_unit_mismatch")
                        row["sample_price_x10000"] = canonical.price_x10000
                        row["sample_volume_shares"] = canonical.trade_volume_shares
                    else:
                        for raw_values, canonical_values in (
                            (payload.get("bid_volume"), canonical.bid_volumes_shares),
                            (payload.get("ask_volume"), canonical.ask_volumes_shares),
                        ):
                            for idx, raw_vol in enumerate(raw_values or []):
                                if canonical_values[idx] is not None and (
                                    canonical_values[idx] != Decimal(str(raw_vol)) * factor
                                ):
                                    row["errors"].append(f"line_{number}_book_unit_mismatch")
                        if (canonical.bid_prices_x10000[0] is not None
                                and canonical.ask_prices_x10000[0] is not None
                                and canonical.bid_prices_x10000[0] >= canonical.ask_prices_x10000[0]):
                            row["errors"].append(f"line_{number}_crossed_book")
                        row["sample_price_x10000"] = canonical.bid_prices_x10000[0]
                        row["sample_volume_shares"] = canonical.bid_volumes_shares[0]
            except (ValueError, OSError, EOFError, TypeError, IndexError, ArithmeticError) as exc:
                row["errors"].append(f"raw_read_or_validation_error:{type(exc).__name__}:{str(exc)[:60]}")
            row["rejected"] = dict(rejected)
            if expected is not None and row["raw_lines"] != expected[key]:
                row["errors"].append(f"expected_{expected[key]}_observed_{row['raw_lines']}")
            if row["errors"]:
                any_errors = True
            if rejected or row["warnings"] or row["accepted"] == 0:
                any_review = True

    status = ("FAIL" if any_errors else "INCOMPLETE" if any_missing
              else "REVIEW" if any_review else "PASS_RAW_ONLY")
    return {
        "schema_version": 1,
        "mode": "OFFLINE_INTRADAY_RAW_AUDIT",
        "symbol": symbol, "trading_date": trading_date,
        "status": status, "market_completeness": "UNVERIFIED",
        "total_raw_lines": sum(r["raw_lines"] for r in results.values()),
        "total_accepted": sum(r["accepted"] for r in results.values()),
        "capture_expected_counts_provided": expected is not None,
        "streams": results,
        "no_broker_login": True, "no_trading_api": True,
        "read_only": True,
    }
