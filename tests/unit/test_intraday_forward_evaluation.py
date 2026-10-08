"""PR-3 synthetic forward study tests. No Shioaji credentials, broker or app DB."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.research.intraday_evaluation import (
    Opportunity, StudyManifest, evaluate, write_report,
)
from src.market_data.intraday_book import MarketBook
from src.market_data.intraday_observations import ObservationEvent

TAIPEI = timezone(timedelta(hours=8))
FREEZE = datetime(2026, 10, 7, 15, tzinfo=TAIPEI)
OPEN = datetime(2026, 10, 8, 9, 0, tzinfo=TAIPEI)


def manifest(**kw):
    return StudyManifest("example", FREEZE.isoformat(), **kw)


def opportunity(**kw):
    defaults = dict(
        opportunity_id="opp-1", symbol="2327", exchange="TSE", lot_type="BOARD",
        trading_date="2026-10-08", known_at=datetime(2026, 10, 7, 20, tzinfo=TAIPEI).isoformat(),
        baseline_decision_at=OPEN.isoformat(), signal_source="predeclared-shadow-plan",
        plan_id="p1", plan_digest="fixedsha",
    )
    defaults.update(kw)
    return Opportunity(**defaults)


def book(when=OPEN, *, received=None, ask=6380000, bid=6370000, symbol="2327", lot="BOARD"):
    return MarketBook(
        1, symbol, "TSE", lot, when.isoformat(), (received or when).isoformat(),
        "session", 1, (bid, None, None, None, None), (3000, None, None, None, None),
        (ask, None, None, None, None), (2000, None, None, None, None),
    )


def event(when=OPEN, *, received=None, kind="BREAKOUT_OBSERVED",
          plan="p1", digest="fixedsha", lot="BOARD", status="OBSERVED", health="HEALTHY"):
    return ObservationEvent(
        event_id="test-evt", rule_version="intraday-observation-v1",
        plan_id=plan, symbol="2327", exchange="TSE", lot_type=lot,
        kind=kind, status=status,
        event_time=when.isoformat(), observed_at=(received or when).isoformat(),
        collector_session_id="session", collection_seq=1,
        reason_codes=(), metrics={}, data_health=health, plan_digest=digest,
    )


def test_successful_pair_is_indicative_not_fill():
    m = manifest()
    c = opportunity()
    new = OPEN + timedelta(seconds=20)
    result = evaluate(m, [c], [book(), book(new, ask=6390000)],
                      [event(new)], data_health="HEALTHY")
    row = result["cohort"][0]
    assert row["status"] == "PAIRED_INDICATIVE"
    assert row["baseline_ask_x10000"] == 6380000
    assert row["challenger_ask_x10000"] == 6390000
    assert row["indicative_delta_bps"] > 0
    assert row["fills_assumed"] is False
    assert result["verdict"] == "EVIDENCE_PENDING"
    assert result["gate"]["unique_opportunities"] == 1


def test_rejected_and_missed_are_kept_in_denominator():
    m = manifest()
    items = [opportunity(), opportunity(opportunity_id="opp-2", plan_id="p2")]
    result = evaluate(m, items, [book()], [], data_health="HEALTHY")
    assert result["status_counts"]["MISSED"] == 2
    assert len(result["cohort"]) == 2
    assert result["paired_indicative_mean_delta_bps"] is None


def test_health_unknown_or_degraded_fail_closed():
    for health in ("UNKNOWN", "DEGRADED", "FAILED"):
        result = evaluate(manifest(), [opportunity()], [book()], [event()], data_health=health)
        assert result["cohort"][0]["status"] == "INSUFFICIENT_DATA"
        assert "SESSION_NOT_VERIFIED" in result["cohort"][0]["reason_codes"]


def test_event_requires_matching_digest_lot_rule_and_time():
    c = opportunity()
    bads = [
        event(digest="unrelated"),
        event(lot="ODD"),
        event(status="INCONCLUSIVE"),
        event(kind="BOOK_IMBALANCE_OBSERVED"),
        replace(event(), rule_version="unexpected"),
        replace(event(), observed_at=(OPEN-timedelta(seconds=1)).isoformat()),
    ]
    for bad in bads:
        result = evaluate(manifest(), [c], [book()], [bad], data_health="HEALTHY")
        assert result["cohort"][0]["status"] == "MISSED"


def test_delayed_or_future_quotes_cannot_be_consumed():
    result = evaluate(manifest(max_quote_delay_seconds=5), [opportunity()],
                      [book(received=OPEN+timedelta(seconds=10))],
                      [event()], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "INSUFFICIENT_DATA"
    result = evaluate(manifest(), [opportunity()],
                      [book(OPEN+timedelta(seconds=5), received=OPEN)],
                      [event()], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "INSUFFICIENT_DATA"


def test_wrong_market_and_lot_not_equivalent():
    result = evaluate(manifest(), [opportunity(lot_type="ODD")], [book()], [event()], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "INSUFFICIENT_DATA"


def test_preregistration_and_pit_gate():
    c = opportunity(known_at=(FREEZE-timedelta(minutes=1)).isoformat())
    assert evaluate(manifest(), [c], [book()], [event()], data_health="HEALTHY")["cohort"][0]["status"] == "INVALID"
    with pytest.raises(ValueError, match="not known"):
        opportunity(known_at=(OPEN+timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError, match="same trading date"):
        opportunity(trading_date="2026-10-07")


def test_duplicate_opportunity_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(manifest(), [opportunity(), opportunity()], [], [], data_health="UNKNOWN")


def test_minimum_60_100_20_forward_gate_is_not_strategy_approval():
    cohort = []
    sessions = [(datetime(2026, 10, 8, tzinfo=TAIPEI) + timedelta(days=day)).date()
                for day in range(100)]
    sessions = [day for day in sessions if day.weekday() < 5][:60]
    for day, trading_day in enumerate(sessions):
        for n in range(2 if day < 40 else 1):
            cohort.append(opportunity(
                opportunity_id=f"opp-{day}-{n}", plan_id=f"plan-{day}-{n}", trading_date=trading_day.isoformat(),
                known_at=FREEZE.isoformat(),
                baseline_decision_at=datetime(trading_day.year, trading_day.month,
                                              trading_day.day, 9, tzinfo=TAIPEI).isoformat(),
            ))
    # Here 60 date labels and 100 opportunities prove cohort arithmetic only.
    r = evaluate(manifest(), cohort, [], [], data_health="HEALTHY")
    assert r["gate"]["trading_days"] == 60
    assert r["gate"]["unique_opportunities"] == 100
    assert r["gate"]["oos_days"] == 20
    assert sum(row["split"] == "OOS" for row in r["cohort"]) == 20
    assert r["verdict"] == "EVIDENCE_PENDING"  # 100 candidates without data are not 100 valid observations
    assert r["gate"]["evaluable_opportunities"] == 0
    assert all(row["status"] == "INSUFFICIENT_DATA" for row in r["cohort"])
    assert r["scope_note"].startswith("Quote-level")


def test_report_is_idempotent_but_does_not_overwrite_changed_data(tmp_path):
    r = evaluate(manifest(), [opportunity()], [book()], [], data_health="UNKNOWN")
    p = write_report(r, tmp_path)
    assert p == write_report(r, tmp_path)
    assert p.read_text().count("EVIDENCE_PENDING") == 1
    changed = dict(r, status_counts={"changed": 1})
    with pytest.raises(FileExistsError):
        write_report(changed, tmp_path)


def test_input_order_deterministic_when_books_have_distinct_received_times():
    m = manifest()
    op = opportunity()
    later = book(OPEN+timedelta(seconds=20), ask=6400000)
    first = book()
    a = evaluate(m, [op], [first, later], [event(OPEN+timedelta(seconds=20))], data_health="HEALTHY")
    b = evaluate(m, [op], [later, first], [event(OPEN+timedelta(seconds=20))], data_health="HEALTHY")
    assert a["cohort"] == b["cohort"]


def test_foreign_session_confirmations_are_not_accepted():
    m = manifest()
    when = OPEN + timedelta(seconds=15)
    foreign = replace(event(when), collector_session_id="reconnected-session")
    result = evaluate(m, [opportunity()], [book(), book(when)], [foreign], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "MISSED"


def test_confirmation_event_occurring_before_entry_decision_is_not_pit_safe():
    m = manifest()
    early = event(OPEN - timedelta(seconds=20),
                  received=OPEN + timedelta(seconds=10))
    result = evaluate(m, [opportunity()], [book()], [early], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "MISSED"


def test_quote_from_new_session_is_not_used_to_price_old_confirmation():
    m = manifest()
    when = OPEN + timedelta(seconds=20)
    book_after_reconnect = replace(book(when), collector_session_id="new-session")
    result = evaluate(m, [opportunity()], [book(), book_after_reconnect],
                      [event(when)], data_health="HEALTHY")
    assert result["cohort"][0]["status"] == "MISSED"


def test_renaming_same_economic_opportunity_cannot_inflate_sample():
    a = opportunity()
    b = opportunity(opportunity_id="different-name")
    with pytest.raises(ValueError, match="duplicate economic opportunity"):
        evaluate(manifest(), [a, b], [book()], [], data_health="HEALTHY")


def test_bundle_contains_all_audit_artifacts(tmp_path):
    r = evaluate(manifest(), [opportunity()], [book()], [], data_health="HEALTHY")
    p = write_report(r, tmp_path)
    names = {f.name for f in p.parent.iterdir()}
    assert names == {
        "comparison.json", "comparison.md", "cohort.csv", "exclusions.jsonl",
        "manifest.json", "lineage.json",
    }
    assert "opp-1" in (p.parent/"cohort.csv").read_text()
    assert "MISSED" in (p.parent/"exclusions.jsonl").read_text()
    assert r["input_sha256"] in (p.parent/"lineage.json").read_text()
    assert r["manifest_sha256"] == __import__("json").loads(
        (p.parent/"lineage.json").read_text()
    )["manifest_sha256"]
