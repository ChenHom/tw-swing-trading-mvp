"""Sector-flow dashboard payload: one JSON the web tab reads (offline, cache only).

Contract: see AGENTS.md "族群資金流". Amounts are estimates (net shares x close); categories overlap.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.application.reporting.sector_flow import build_sector_flow_report

LOOKBACK_DAYS = 140  # calendar days that comfortably hold 90 trading days
_FIELDS = {"amt": "estimated_institutional_net_amount_twd", "f": "estimated_foreign_net_amount_twd",
           "t": "estimated_investment_trust_net_amount_twd", "dl": "estimated_dealer_net_amount_twd"}


def _index_closes(cache_dir: Path, trading_date: str) -> dict[str, float]:
    """TWSE price indices of one date from the cached MI_INDEX payload; {} when missing or unparsable."""
    try:
        payload = json.loads((cache_dir / "twse" / "MI_INDEX" / trading_date / "market.json").read_text(encoding="utf-8"))
        table = next(t for t in payload["tables"] if "價格指數(臺灣證券交易所)" in (t.get("title") or ""))
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return {}
    out = {}
    for row in table.get("data", []):
        try:
            out[row[0]] = float(str(row[1]).replace(",", ""))
        except (ValueError, IndexError, TypeError):
            continue
    return out


def _window_stocks(report: dict[str, Any]) -> dict[str, Any]:
    def stock(s: dict[str, Any]) -> dict[str, Any]:
        amount = s["estimated_net_amount_twd"]  # None only when the window fell back to net-share ranking
        return {"sym": s["symbol"], "name": s["name"], "amt": None if amount is None else round(amount),
                "share": round(s["share_of_side_pct"], 1), "f": s["foreign_net_shares"],
                "t": s["investment_trust_net_shares"], "dl": s["dealer_net_shares"]}

    return {
        item["category"]: {
            "members": item["member_count"],
            "in": [stock(s) for s in item["top_inflows"][:5]],
            "out": [stock(s) for s in item["top_outflows"][:5]],
            "subs": [{"name": g["category"], "n": g["member_count"], "amt": round(g["estimated_institutional_net_amount_twd"])}
                     for g in item.get("subcategories", [])],
        }
        for item in report.get("category_detail", [])
    }


def build_sector_flow_dashboard(*, cache_dir: Path, end_date: str, windows=(20, 30, 60, 90)) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    lookback_start = (date.fromisoformat(end_date) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    # The 31-day cap guards network ingest; an offline replay can take a whole window in one report,
    # which keeps every figure identical to `report sector-flow` over the same dates.
    full = build_sector_flow_report(cache_dir=cache_dir, start_date=lookback_start, end_date=end_date, max_days=None)
    dates = full["observed_trading_dates"][-max(windows):]
    out: dict[str, Any] = {
        "schema_version": 1, "end_date": end_date, "dates": dates, "taiex": [], "windows": list(windows),
        "status": "blocked", "warnings": [], "taxonomy_snapshot_date": None, "holidays": [], "rows": [],
        "stocks": {str(w): {} for w in windows},
    }
    if not dates:
        out["warnings"] = ["no trading dates found in cache"]
        return out

    reports: dict[str, dict[str, Any]] = {}

    def report_from(start: str) -> dict[str, Any]:
        if start not in reports:
            categories = sorted({c["category"] for day in full["daily"] if day["trading_date"] >= start for c in day["categories"]})
            reports[start] = build_sector_flow_report(cache_dir=cache_dir, start_date=start, end_date=end_date,
                                                      detail_categories=categories, top=10**6, max_days=None)
        return reports[start]

    main = report_from(dates[0])
    out["status"] = main["status"]
    out["warnings"] = main["warnings"][:20]
    out["taxonomy_snapshot_date"] = main["taxonomy"]["snapshot_date"]
    out["holidays"] = main["dates_without_data"][-10:]

    broad = {c["category"]: c.get("is_broad", False) for c in main["period_summary"]}
    series: dict[str, dict[str, dict[str, float]]] = {}
    for day in main["daily"]:
        for c in day["categories"]:
            s = series.setdefault(c["category"], {k: {} for k in _FIELDS})
            for k, field in _FIELDS.items():
                s[k][day["trading_date"]] = c[field] or 0.0

    idx = {d: _index_closes(cache_dir, d) for d in dates}
    idx_names = set().union(*idx.values())
    out["taiex"] = [idx[d].get("發行量加權股價指數") for d in dates]
    for n in sorted(series):
        name_idx = next((c for c in (n + "類指數", n.removesuffix("工業") + "類指數", n.removesuffix("業") + "類指數") if c in idx_names), None)
        out["rows"].append({
            "name": n, "broad": broad.get(n, False), "idx": name_idx,
            "idxv": [idx[d].get(name_idx) for d in dates] if name_idx else None,
            **{k: [round(series[n][k].get(d, 0.0)) for d in dates] for k in ("f", "t", "dl")},
            "daily": [round(series[n]["amt"].get(d, 0.0)) for d in dates],
        })

    for w in windows:
        out["stocks"][str(w)] = _window_stocks(report_from(dates[-w:][0]))
    return out
