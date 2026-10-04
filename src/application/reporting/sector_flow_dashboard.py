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
LH_WINDOW = 20  # trading days of weekly large-holder changes shown in the tab
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
            "subs": [{"name": g["category"], "n": g["member_count"], "amt": round(g["estimated_institutional_net_amount_twd"]),
                      "in": [stock(s) for s in g["top_inflows"][:5]], "out": [stock(s) for s in g["top_outflows"][:5]]}
                     for g in item.get("subcategories", [])],
        }
        for item in report.get("category_detail", [])
    }


def _large_holder(main: dict[str, Any], weekly: list[tuple[str, dict[str, Any]]], snapshots: int, start: str, top: int = 5) -> dict[str, Any]:
    """TDCC large holders (levels 12-15, 400+ lots) by category over the window, ranked by estimated value.

    `weekly` holds one report large_holder block per snapshot in the window, each comparing it with the
    snapshot before (so every week is computed exactly like `report sector-flow` ending on that date).
    Window figures are sums of those weeks; a week whose pair could not be compared stays null.
    """
    out: dict[str, Any] = {"status": main["status"], "reason": main.get("reason"), "prior": main.get("prior_as_of_date"),
                           "latest": main.get("latest_as_of_date"), "snapshots": snapshots, "window": LH_WINDOW,
                           "start": start, "weeks": [d for d, _ in weekly], "reshaped": 0, "rows": []}
    if main["status"] != "ok":
        return out
    if not weekly:
        out.update(status="insufficient_data", reason="no_snapshot_in_window")
        return out
    cats: dict[str, dict[str, Any]] = {}
    for i, (_, lh) in enumerate(weekly):
        if lh["status"] != "ok":
            continue
        out["reshaped"] += lh["excluded_symbols"]["custody_shares_changed"]
        for c in lh["categories"]:
            cat = cats.setdefault(c["category"], {"broad": c["is_broad"], "wk": [None] * len(weekly), "stocks": {}})
            cat["wk"][i] = c["estimated_change_twd"]
        for s in lh["stocks"]:
            for category in s["categories"]:
                acc = cats[category]["stocks"].setdefault(s["symbol"], {"sym": s["symbol"], "name": s["name"], "amt": 0.0, "shares": 0, "pp": 0.0, "priced": True})
                acc["shares"] += s["large_holder_share_delta"]
                acc["pp"] += s["percent_point_delta"]
                if s["estimated_change_twd"] is None:
                    acc["priced"] = False
                else:
                    acc["amt"] += s["estimated_change_twd"]

    def stock(a: dict[str, Any]) -> dict[str, Any]:
        return {"sym": a["sym"], "name": a["name"], "amt": round(a["amt"]), "shares": a["shares"], "pp": round(a["pp"], 2)}

    rows = []
    for name, cat in cats.items():
        group = list(cat["stocks"].values())
        priced = [a for a in group if a["priced"]]
        rows.append({
            "name": name, "broad": cat["broad"], "amt": round(sum(v for v in cat["wk"] if v is not None)),
            "wk": [None if v is None else round(v) for v in cat["wk"]],
            "up": sum(1 for a in group if a["shares"] > 0), "down": sum(1 for a in group if a["shares"] < 0),
            "missing": len(group) - len(priced),
            "in": [stock(a) for a in sorted((a for a in priced if a["amt"] > 0), key=lambda a: (-a["amt"], a["sym"]))[:top]],
            "out": [stock(a) for a in sorted((a for a in priced if a["amt"] < 0), key=lambda a: (a["amt"], a["sym"]))[:top]],
        })
    out["rows"] = sorted(rows, key=lambda r: (-r["amt"], r["name"]))
    return out


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
        "large_holder": {"status": "insufficient_data", "reason": "no_trading_dates", "prior": None, "latest": None, "snapshots": 0,
                         "window": LH_WINDOW, "start": None, "weeks": [], "reshaped": 0, "rows": []},
    }
    if not dates:
        out["warnings"] = ["no trading dates found in cache"]
        return out

    reports: dict[str, dict[str, Any]] = {}

    def report_from(start: str) -> dict[str, Any]:
        if start not in reports:
            categories = sorted({c["category"] for day in full["daily"] if day["trading_date"] >= start for c in day["categories"]})
            reports[start] = build_sector_flow_report(cache_dir=cache_dir, start_date=start, end_date=end_date,
                                                      detail_categories=categories, top=10**6, max_days=None,
                                                      large_holder_stocks=True)
        return reports[start]

    main = report_from(dates[0])
    out["status"] = main["status"]
    out["warnings"] = main["warnings"][:20]
    out["taxonomy_snapshot_date"] = main["taxonomy"]["snapshot_date"]
    out["holidays"] = main["dates_without_data"][-10:]
    # One report per TDCC week in the window, each ending on that snapshot: same universe start and industry
    # snapshot as the tab, closes as of that week.
    root = cache_dir / "tdcc" / "holding_distribution"
    snapshots = sorted(p.parent.name for p in root.glob("*/market.json") if p.parent.name <= end_date)
    lh_start = dates[-LH_WINDOW:][0]
    weekly = [(d, build_sector_flow_report(cache_dir=cache_dir, start_date=dates[0], end_date=d, max_days=None,
                                           large_holder_stocks=True, taxonomy_date=end_date)["large_holder"])
              for d in snapshots[1:] if d >= lh_start]
    out["large_holder"] = _large_holder(main["large_holder"], weekly, len(snapshots), lh_start)

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
