"""PR-3: preregistered, fail-closed intraday execution quality study.

This does NOT model actual fills, PnL, or recover rejected strategy approvals.
Visible best ask is an *indicative* quote only; no matching-engine queue or fill
assumption is made. All decisions use the time data reached the collector.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.market_data.intraday_book import MarketBook
from src.market_data.intraday_observations import ObservationEvent

SCHEMA_VERSION = 1
PROTOCOL = "intraday-execution-study-v1"
REQUIRED_DAYS = 60
REQUIRED_OPPORTUNITIES = 100
OOS_DAYS = 20
ALLOWED_EVENTS = frozenset(("SUPPORT_HELD", "BREAKOUT_OBSERVED"))


def _datetime(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("offset-aware timestamp required")
    return d


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class StudyManifest:
    experiment_id: str
    frozen_at: str
    rule_version: str = "intraday-observation-v1"
    max_quote_delay_seconds: int = 30
    entry_fee_bps: int = 15
    sell_tax_bps: int = 0
    max_entry_wait_seconds: int = 120
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if not self.experiment_id or not self.experiment_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid experiment id")
        _datetime(self.frozen_at)
        if self.schema_version != SCHEMA_VERSION or self.rule_version != "intraday-observation-v1":
            raise ValueError("unknown version")
        if not (0 <= self.entry_fee_bps <= 100 and 0 <= self.sell_tax_bps <= 100):
            raise ValueError("unsupported cost assumption")
        if not (1 <= self.max_quote_delay_seconds <= 300 and
                1 <= self.max_entry_wait_seconds <= 600):
            raise ValueError("invalid quote freshness bounds")


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    symbol: str
    exchange: str
    lot_type: str
    trading_date: str
    known_at: str
    baseline_decision_at: str
    signal_source: str
    plan_id: str
    plan_digest: str

    def __post_init__(self):
        from src.market_data.intraday_tick import EXCHANGES, LOT_TYPES
        if not self.opportunity_id or not self.plan_id or not self.plan_digest or not self.signal_source:
            raise ValueError("PIT opportunity lineage required")
        if not self.symbol or not self.symbol.isascii() or not self.symbol.isalnum():
            raise ValueError("invalid symbol")
        if self.exchange not in EXCHANGES or self.lot_type not in LOT_TYPES:
            raise ValueError("invalid market")
        date.fromisoformat(self.trading_date)
        if _datetime(self.known_at) > _datetime(self.baseline_decision_at):
            raise ValueError("opportunity was not known at baseline decision")
        if _datetime(self.baseline_decision_at).date().isoformat() != self.trading_date:
            raise ValueError("baseline must be same trading date")


def _indicative_ask(
    books: list[MarketBook], *, candidate: Opportunity, decision_at: datetime,
    manifest: StudyManifest, required_session: str | None = None,
) -> tuple[int | None, str, str | None]:
    """Earliest observable ask after decision; reject delayed, crossed and absent books."""
    cutoff = decision_at + timedelta(seconds=manifest.max_entry_wait_seconds)
    matched = sorted((b for b in books
                      if (b.symbol, b.exchange, b.lot_type) ==
                         (candidate.symbol, candidate.exchange, candidate.lot_type)
                      and decision_at <= _datetime(b.received_at) <= cutoff
                      and (required_session is None or b.collector_session_id == required_session)),
                     key=lambda b: (_datetime(b.received_at), b.collection_seq))
    for book in matched:
        event_at = _datetime(book.event_time)
        received_at = _datetime(book.received_at)
        if event_at > received_at or (received_at-event_at).total_seconds() > manifest.max_quote_delay_seconds:
            continue
        ask = book.ask_prices_x10000[0]
        bid = book.bid_prices_x10000[0]
        if ask is None or bid is None or ask <= bid or ask <= 0:
            continue
        return ask, "QUOTE_ONLY_NOT_FILL", book.collector_session_id
    return None, "NO_FRESH_EXECUTABLE_QUOTE", None


def _row(candidate: Opportunity, books: list[MarketBook], events: list[ObservationEvent],
         manifest: StudyManifest, *, data_health: str) -> dict[str, Any]:
    baseline_at = _datetime(candidate.baseline_decision_at)
    result: dict[str, Any] = {
        "opportunity_id": candidate.opportunity_id,
        "symbol": candidate.symbol, "exchange": candidate.exchange, "lot_type": candidate.lot_type,
        "trading_date": candidate.trading_date, "plan_id": candidate.plan_id,
        "plan_digest": candidate.plan_digest, "signal_source": candidate.signal_source,
        "baseline_ask_x10000": None, "challenger_ask_x10000": None,
        "indicative_delta_bps": None, "status": "INSUFFICIENT_DATA",
        "baseline_status": "UNAVAILABLE", "challenger_status": "UNAVAILABLE",
        "fills_assumed": False, "data_health": data_health, "reason_codes": [],
    }
    if data_health != "HEALTHY":
        result["reason_codes"].append("SESSION_NOT_VERIFIED")
        return result
    if _datetime(candidate.known_at) < _datetime(manifest.frozen_at):
        # Manifest must be registered before its first observed opportunity.
        result["status"] = "INVALID"
        result["reason_codes"].append("MANIFEST_NOT_PREREGISTERED")
        return result

    baseline, base_reason, base_session = _indicative_ask(
        books, candidate=candidate, decision_at=baseline_at, manifest=manifest,
    )
    if baseline is None:
        result["reason_codes"].append(base_reason)
        return result
    result["baseline_ask_x10000"] = baseline
    result["baseline_status"] = base_reason

    valid_events = sorted((
        e for e in events
        if e.symbol == candidate.symbol and e.exchange == candidate.exchange
        and e.lot_type == candidate.lot_type and e.plan_id == candidate.plan_id
        and e.plan_digest == candidate.plan_digest and e.rule_version == manifest.rule_version
        and e.kind in ALLOWED_EVENTS and e.status == "OBSERVED"
        and e.data_health == "HEALTHY"
        and baseline_at <= _datetime(e.observed_at)
        and baseline_at <= _datetime(e.event_time) <= _datetime(e.observed_at)
        and e.collector_session_id == base_session
    ), key=lambda e: (_datetime(e.observed_at), e.collection_seq))
    if not valid_events:
        result["status"] = "MISSED"
        result["challenger_status"] = "NO_CONFIRMATION"
        return result
    earliest = _datetime(valid_events[0].observed_at)
    challenger, chall_reason, _challenger_session = _indicative_ask(
        books, candidate=candidate, decision_at=earliest, manifest=manifest,
        required_session=base_session,
    )
    if challenger is None:
        result["status"] = "MISSED"
        result["challenger_status"] = chall_reason
        return result
    result["challenger_ask_x10000"] = challenger
    result["challenger_status"] = chall_reason
    # Entry fees cancel out under an identical side/cost assumption; keep fee
    # explicit to prevent pretending this estimates a full realized trade.
    base_cost = baseline * (10000 + manifest.entry_fee_bps)
    chall_cost = challenger * (10000 + manifest.entry_fee_bps)
    result["indicative_delta_bps"] = round((chall_cost-base_cost)*10000/base_cost, 2)
    result["status"] = "PAIRED_INDICATIVE"
    return result


def evaluate(
    manifest: StudyManifest, opportunities: Iterable[Opportunity],
    books: Iterable[MarketBook], events: Iterable[ObservationEvent],
    *, data_health: str = "UNKNOWN",
) -> dict[str, Any]:
    if data_health not in ("HEALTHY", "DEGRADED", "FAILED", "UNKNOWN"):
        raise ValueError("invalid data health")
    cohort = list(opportunities)
    ids = [c.opportunity_id for c in cohort]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate opportunity_id; no double counting")
    ordered = sorted(cohort, key=lambda c: (c.trading_date, c.opportunity_id))
    book_list = sorted(books, key=lambda b: (b.received_at, b.collection_seq, b.symbol, b.lot_type))
    event_list = sorted(events, key=lambda e: (e.observed_at, e.collection_seq, e.event_id))
    rows = [_row(c, book_list, event_list, manifest, data_health=data_health) for c in ordered]
    dates = sorted(set(c.trading_date for c in ordered))
    oos_dates = set(dates[-OOS_DAYS:]) if len(dates) >= REQUIRED_DAYS else set()
    for row in rows:
        row["split"] = ("OOS" if row["trading_date"] in oos_dates else
                        "EXPLORATORY" if len(dates) < REQUIRED_DAYS else "TRAIN")
    qualifying = [r for r in rows if r["baseline_ask_x10000"] is not None and r["status"] in ("MISSED", "PAIRED_INDICATIVE")]
    qualified_dates = {r["trading_date"] for r in qualifying}
    qualified_oos_dates = qualified_dates & oos_dates
    # Split is declared by time, not by positive results. This gate alone
    # is NOT a scientific efficacy approval; a human must review uncertainty.
    gates = {
        "trading_days": len(dates), "unique_opportunities": len(rows),
        "evaluable_opportunities": len(qualifying), "evaluable_days": len(qualified_dates),
        "oos_days": len(oos_dates), "evaluable_oos_days": len(qualified_oos_dates),
        "min_trading_days": REQUIRED_DAYS, "min_opportunities": REQUIRED_OPPORTUNITIES,
        "min_oos_days": OOS_DAYS,
        "met": (len(qualified_dates) >= REQUIRED_DAYS and len(qualifying) >= REQUIRED_OPPORTUNITIES
                and len(qualified_oos_dates) >= OOS_DAYS and
                all(row["data_health"] == "HEALTHY" for row in rows)),
    }
    statuses = {key: sum(row["status"] == key for row in rows)
                for key in ("PAIRED_INDICATIVE", "MISSED", "INSUFFICIENT_DATA", "INVALID")}
    comparable = [x["indicative_delta_bps"] for x in rows
                  if x["indicative_delta_bps"] is not None]
    return {
        "schema_version": SCHEMA_VERSION, "mode": "SHADOW_RESEARCH_ONLY",
        "experiment_id": manifest.experiment_id, "manifest": asdict(manifest),
        "manifest_sha256": _digest(asdict(manifest)),
        "input_sha256": _digest({
            "opportunities": [asdict(x) for x in ordered],
            "books": [x.as_dict() for x in book_list],
            "events": [x.as_dict() for x in event_list],
            "data_health": data_health,
        }),
        "gate": gates,
        "verdict": "ELIGIBLE_FOR_MANUAL_REVIEW" if gates["met"] else "EVIDENCE_PENDING",
        "status_counts": statuses,
        "paired_indicative_mean_delta_bps": (
            round(sum(comparable)/len(comparable), 2) if comparable else None
        ),
        "scope_note": "Quote-level comparison, no executed fills, no PnL, no strategy approval",
        "cohort": rows,
    }


def write_report(result: Mapping[str, Any], output_root: Path) -> Path:
    experiment_id = str(result["experiment_id"])
    if not experiment_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("unsafe experiment id")
    directory = Path(output_root) / experiment_id
    directory.mkdir(parents=True, exist_ok=True)
    # No direct writes to data/app.db or any existing brokerage ledger.
    output = directory / "comparison.json"
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != content:
        raise FileExistsError("immutable experiment result already exists with different data")
    output.write_text(content, encoding="utf-8")
    return output
