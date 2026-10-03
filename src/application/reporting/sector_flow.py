from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from src.market_data.sector_flow_sources import (
    ClosePriceRow,
    HoldingDistributionRow,
    InstitutionalFlowRow,
    ProviderSchemaError,
    parse_tdcc_holdings,
    parse_tpex_closes,
    parse_tpex_institutional,
    parse_twse_closes,
    parse_twse_institutional,
    read_json,
)


# Maps TPEx / older category names to one canonical name so one sector is one row.
CATEGORY_SYNONYMS = {
    "其他電子類": "其他電子業",
    "居家生活類": "居家生活",
    "數位雲端類": "數位雲端",
    "綠能環保類": "綠能環保",
    "運動休閒類": "運動休閒",
    "金融業": "金融保險",
    "農業科技業": "農業科技",
    "觀光事業": "觀光餐旅",
}
BOARD_LABELS = frozenset({"創新板股票", "創新版股票"})  # board labels, never categories
CATCH_ALL_CATEGORY = "其他"
UNCLASSIFIED = "未分類"
TDCC_MAX_SNAPSHOT_GAP_DAYS = 14
TDCC_MAX_STALENESS_DAYS = 7
PRICE_COVERAGE_MIN = 0.9
BROAD_CATEGORIES = frozenset({"電子工業", "化學生技醫療"})
CATEGORY_OVERLAP_NOTE = "一檔股票可能同時計入多個族群（例如大類「電子工業」與細類「半導體業」），族群之間互有重疊，不可加總。"


@dataclass(frozen=True)
class TaxonomyEntry:
    symbol: str
    name: str
    categories: tuple[str, ...]
    market: str
    snapshot_date: str


def normalize_categories(raw: Sequence[str]) -> tuple[str, ...]:
    names = {CATEGORY_SYNONYMS.get(name, name) for name in (item.strip() for item in raw) if name and name not in BOARD_LABELS}
    if len(names) > 1:
        names.discard(CATCH_ALL_CATEGORY)
    return tuple(sorted(names))


def _categories_of(metadata: TaxonomyEntry | None) -> tuple[str, ...]:
    return metadata.categories if metadata and metadata.categories else (UNCLASSIFIED,)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value}") from exc


def _dates(start_date: str, end_date: str, max_days: int | None = 31) -> list[str]:
    start = _iso_date(start_date)
    end = _iso_date(end_date)
    if start > end:
        raise ValueError("start date must not exceed end date")
    if max_days is not None and (end - start).days >= max_days:
        raise ValueError(f"sector-flow range must not exceed {max_days} calendar days")
    result = []
    cursor = start
    while cursor <= end:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def load_taxonomy(cache_dir: Path, *, end_date: str) -> dict[str, TaxonomyEntry]:
    """Load the newest eligible FinMind stock-info cache snapshot."""
    root = cache_dir / "finmind" / "TaiwanStockInfo"
    eligible: list[tuple[str, Path]] = []
    if root.exists():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                snapshot = date.fromisoformat(child.name)
            except ValueError:
                continue
            if snapshot <= _iso_date(end_date):
                eligible.append((child.name, child))
    if not eligible:
        return {}
    snapshot_date, snapshot_dir = max(eligible, key=lambda item: item[0])
    raw: dict[str, dict[str, Any]] = {}
    for path in sorted(snapshot_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            symbol = str(row.get("stock_id", "")).strip()
            if not symbol:
                continue
            item = raw.setdefault(symbol, {"name": str(row.get("stock_name") or row.get("name") or symbol).strip(), "market": str(row.get("type") or "").strip().lower(), "categories": []})
            item["categories"].append(str(row.get("industry_category") or ""))
    return {
        symbol: TaxonomyEntry(symbol=symbol, name=item["name"], categories=normalize_categories(item["categories"]), market=item["market"], snapshot_date=snapshot_date)
        for symbol, item in raw.items()
    }


def _source_path(cache_dir: Path, provider: str, dataset: str, trading_date: str) -> Path:
    return cache_dir / provider / dataset / trading_date / "market.json"


def _load_source(
    cache_dir: Path,
    provider: str,
    dataset: str,
    parser: Any,
    trading_date: str,
) -> tuple[list[Any], dict[str, Any]]:
    path = _source_path(cache_dir, provider, dataset, trading_date)
    # Relative to cache_dir, so the report is identical however --cache-dir is spelled.
    status: dict[str, Any] = {"requested_date": trading_date, "cache_path": path.relative_to(cache_dir).as_posix()}
    if not path.exists():
        status.update({"state": "missing", "row_count": 0})
        return [], status
    try:
        rows = parser(read_json(path), trading_date)
        status.update({"state": "ok" if rows else "no_data", "row_count": len(rows), "embedded_date": trading_date})
        return rows, status
    except (OSError, ValueError) as exc:  # ValueError covers bad JSON, bad UTF-8 and ProviderSchemaError
        status.update({"state": "schema_error", "row_count": 0, "error": str(exc)})
        return [], status


def _blank_category(category: str) -> dict[str, Any]:
    return {
        "category": category,
        "foreign_net_shares": 0,
        "investment_trust_net_shares": 0,
        "dealer_net_shares": 0,
        "institutional_net_shares": 0,
        "estimated_foreign_net_amount_twd": 0.0,
        "estimated_investment_trust_net_amount_twd": 0.0,
        "estimated_dealer_net_amount_twd": 0.0,
        "estimated_institutional_net_amount_twd": 0.0,
        "covered_symbol_count": 0,
        "missing_price_count": 0,
        "amount_method": "net_shares_times_close",
        "_contributors": [],
    }


def _finalize_categories(categories: Mapping[str, dict[str, Any]], ranking_method: str) -> list[dict[str, Any]]:
    result = []
    for value in categories.values():
        item = dict(value)
        contributors = item.pop("_contributors")
        for field in (
            "estimated_foreign_net_amount_twd",
            "estimated_investment_trust_net_amount_twd",
            "estimated_dealer_net_amount_twd",
            "estimated_institutional_net_amount_twd",
        ):
            item[field] = round(float(item[field]), 2)
        for contributor in contributors:
            if contributor["estimated_net_amount_twd"] is not None:
                contributor["estimated_net_amount_twd"] = round(float(contributor["estimated_net_amount_twd"]), 2)
        # Contributors are selected and ordered by the same metric as the category ranking.
        field = "estimated_net_amount_twd" if ranking_method == "estimated_amount" else "institutional_net_shares"
        measured = [row for row in contributors if row[field] is not None]
        positives = sorted((row for row in measured if row[field] > 0), key=lambda row: (row[field], row["symbol"]), reverse=True)
        negatives = sorted((row for row in measured if row[field] < 0), key=lambda row: (row[field], row["symbol"]))
        item["is_broad"] = item["category"] in BROAD_CATEGORIES
        item["top_positive_contributors"] = positives[:5]
        item["top_negative_contributors"] = negatives[:5]
        result.append(item)
    key = "estimated_institutional_net_amount_twd" if ranking_method == "estimated_amount" else "institutional_net_shares"
    return sorted(result, key=lambda item: (item[key], item["category"]), reverse=True)


def _aggregate_day(
    flows: Sequence[InstitutionalFlowRow],
    closes: Mapping[tuple[str, str], float],
    taxonomy: Mapping[str, TaxonomyEntry],
    ranking_method: str,
) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for row in flows:
        metadata = taxonomy.get(row.symbol)
        close = closes.get((row.market, row.symbol))
        amount = None if close is None else row.institutional_net_shares * close
        for category in _categories_of(metadata):
            item = categories.setdefault(category, _blank_category(category))
            for field in ("foreign_net_shares", "investment_trust_net_shares", "dealer_net_shares", "institutional_net_shares"):
                item[field] += getattr(row, field)
            if close is None:
                item["missing_price_count"] += 1
            else:
                item["covered_symbol_count"] += 1
                item["estimated_foreign_net_amount_twd"] += row.foreign_net_shares * close
                item["estimated_investment_trust_net_amount_twd"] += row.investment_trust_net_shares * close
                item["estimated_dealer_net_amount_twd"] += row.dealer_net_shares * close
                item["estimated_institutional_net_amount_twd"] += amount
            item["_contributors"].append({
                "market": row.market,
                "symbol": row.symbol,
                "name": metadata.name if metadata else row.name,
                "institutional_net_shares": row.institutional_net_shares,
                "estimated_net_amount_twd": amount,
            })
    return _finalize_categories(categories, ranking_method)


def _aggregate_period(
    day_inputs: Sequence[tuple[str, list[InstitutionalFlowRow], dict[tuple[str, str], float]]],
    taxonomy: Mapping[str, TaxonomyEntry],
    ranking_method: str,
) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    contributors: dict[tuple[str, str, str], dict[str, Any]] = {}
    covered: dict[str, set[tuple[str, str]]] = defaultdict(set)
    missing: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for _trading_date, flows, closes in day_inputs:
        for row in flows:
            metadata = taxonomy.get(row.symbol)
            close = closes.get((row.market, row.symbol))
            amount = None if close is None else row.institutional_net_shares * close
            for category in _categories_of(metadata):
                item = categories.setdefault(category, _blank_category(category))
                for field in ("foreign_net_shares", "investment_trust_net_shares", "dealer_net_shares", "institutional_net_shares"):
                    item[field] += getattr(row, field)
                # Period counts are distinct stocks, not stock-days.
                if close is None:
                    missing[category].add((row.market, row.symbol))
                else:
                    covered[category].add((row.market, row.symbol))
                    item["estimated_foreign_net_amount_twd"] += row.foreign_net_shares * close
                    item["estimated_investment_trust_net_amount_twd"] += row.investment_trust_net_shares * close
                    item["estimated_dealer_net_amount_twd"] += row.dealer_net_shares * close
                    item["estimated_institutional_net_amount_twd"] += amount
                key = (category, row.market, row.symbol)
                contributor = contributors.setdefault(key, {
                    "market": row.market,
                    "symbol": row.symbol,
                    "name": metadata.name if metadata else row.name,
                    "institutional_net_shares": 0,
                    "estimated_net_amount_twd": 0.0 if amount is not None else None,
                })
                contributor["institutional_net_shares"] += row.institutional_net_shares
                if amount is None:
                    contributor["estimated_net_amount_twd"] = None
                elif contributor["estimated_net_amount_twd"] is not None:
                    contributor["estimated_net_amount_twd"] += amount
    for (category, _market, _symbol), contributor in contributors.items():
        categories[category]["_contributors"].append(contributor)
    for category, item in categories.items():
        item["covered_symbol_count"] = len(covered[category])
        item["missing_price_count"] = len(missing[category])
    return _finalize_categories(categories, ranking_method)


def _load_holdings(cache_dir: Path, end_date: str) -> tuple[dict[str, list[HoldingDistributionRow]], list[dict[str, str]]]:
    root = cache_dir / "tdcc" / "holding_distribution"
    result: dict[str, list[HoldingDistributionRow]] = {}
    errors: list[dict[str, str]] = []
    if not root.exists():
        return result, errors
    for path in sorted(root.glob("*/market.json")):
        try:
            if date.fromisoformat(path.parent.name) > _iso_date(end_date):
                continue
        except ValueError:
            continue
        try:
            rows = parse_tdcc_holdings(read_json(path))
        except (OSError, ValueError) as exc:
            errors.append({"as_of_date": path.parent.name, "error": str(exc)})
            continue
        if rows and rows[0].as_of_date <= end_date:
            result[rows[0].as_of_date] = rows
    return result, errors


def build_large_holder_proxy(
    *,
    holdings_by_date: Mapping[str, Sequence[HoldingDistributionRow]],
    taxonomy: Mapping[str, TaxonomyEntry],
    closes: Mapping[str, float],
    end_date: str,
    start_date: str,
    universe: Collection[str],
) -> dict[str, Any]:
    eligible = sorted(key for key in holdings_by_date if key <= end_date)
    latest = eligible[-1] if eligible else None
    if len(eligible) < 2:
        return {"status": "insufficient_data", "reason": "fewer_than_two_eligible_snapshots", "latest_as_of_date": latest}
    prior = eligible[-2]
    latest_day, prior_day = _iso_date(latest), _iso_date(prior)
    reason = None
    if latest_day < _iso_date(start_date) - timedelta(days=TDCC_MAX_STALENESS_DAYS):
        reason = "latest_snapshot_too_old"
    elif (latest_day - prior_day).days > TDCC_MAX_SNAPSHOT_GAP_DAYS:
        reason = "snapshots_not_consecutive_weeks"
    if reason:
        return {"status": "insufficient_data", "reason": reason, "latest_as_of_date": latest, "prior_as_of_date": prior}

    def by_symbol(rows: Sequence[HoldingDistributionRow]) -> dict[str, tuple[int, float]]:
        shares: dict[str, int] = defaultdict(int)
        percents: dict[str, float] = defaultdict(float)
        for row in rows:
            if 12 <= row.level <= 15:
                shares[row.symbol] += row.shares
                percents[row.symbol] += row.percent
        return {symbol: (shares[symbol], percents[symbol]) for symbol in shares}

    old = by_symbol(holdings_by_date[prior])
    new = by_symbol(holdings_by_date[latest])
    listed = set(universe)
    old_symbols = {row.symbol for row in holdings_by_date[prior]}
    new_symbols = {row.symbol for row in holdings_by_date[latest]}
    candidates = old_symbols | new_symbols
    in_universe = candidates & listed
    counted = in_universe & old_symbols & new_symbols
    excluded = {"outside_listed_universe": len(candidates - listed), "not_in_both_snapshots": len(in_universe - counted)}
    categories: dict[str, dict[str, Any]] = {}
    for symbol in sorted(counted):
        share_delta = new.get(symbol, (0, 0.0))[0] - old.get(symbol, (0, 0.0))[0]
        percent_delta = new.get(symbol, (0, 0.0))[1] - old.get(symbol, (0, 0.0))[1]
        for category in _categories_of(taxonomy.get(symbol)):
            item = categories.setdefault(category, {"category": category, "is_broad": category in BROAD_CATEGORIES, "large_holder_share_delta": 0, "sum_stock_percent_point_delta": 0.0, "estimated_change_twd": 0.0, "missing_price_count": 0})
            item["large_holder_share_delta"] += share_delta
            item["sum_stock_percent_point_delta"] += percent_delta
            if symbol in closes:
                item["estimated_change_twd"] += share_delta * closes[symbol]
            else:
                item["missing_price_count"] += 1
    for item in categories.values():
        item["sum_stock_percent_point_delta"] = round(float(item["sum_stock_percent_point_delta"]), 6)
        item["estimated_change_twd"] = round(float(item["estimated_change_twd"]), 2)
    return {
        "status": "ok",
        "method": "holding_change_proxy",
        "prior_as_of_date": prior,
        "latest_as_of_date": latest,
        "levels": [12, 13, 14, 15],
        "excluded_symbols": excluded,
        "categories": sorted(categories.values(), key=lambda row: (row["large_holder_share_delta"], row["category"]), reverse=True),
    }


ONLY_BROAD_GROUP = "（僅大類）"


class UnknownCategoryError(ValueError):
    """A requested drill-down category has no member stock with flow rows in the period."""


_SHARE_FIELDS = ("foreign_net_shares", "investment_trust_net_shares", "dealer_net_shares", "institutional_net_shares")
_TOTAL_FIELDS = _SHARE_FIELDS + (
    "estimated_foreign_net_amount_twd",
    "estimated_investment_trust_net_amount_twd",
    "estimated_dealer_net_amount_twd",
    "estimated_institutional_net_amount_twd",
    "covered_symbol_count",
    "missing_price_count",
    "amount_method",
)


def build_category_detail(
    *,
    day_inputs: Sequence[tuple[str, list[InstitutionalFlowRow], dict[tuple[str, str], float]]],
    taxonomy: Mapping[str, TaxonomyEntry],
    ranking_method: str,
    period_summary: Sequence[Mapping[str, Any]],
    categories: Sequence[str],
    top: int,
) -> list[dict[str, Any]]:
    """Per-category drill-down: every member stock over the period, not just the top-5 contributors."""
    if top < 1:
        raise ValueError("top must be >= 1")
    summary = {row["category"]: row for row in period_summary}
    requested = list(dict.fromkeys(CATEGORY_SYNONYMS.get(name.strip(), name.strip()) for name in categories))
    unknown = [name for name in requested if name not in summary]
    if unknown:
        raise UnknownCategoryError(f"no flow rows for category: {', '.join(unknown)}; available categories: {', '.join(sorted(summary)) or '(none)'}")
    dates = [item[0] for item in day_inputs]
    stocks: dict[tuple[str, str], dict[str, Any]] = {}
    for trading_date, flows, closes in day_inputs:
        for row in flows:
            metadata = taxonomy.get(row.symbol)
            close = closes.get((row.market, row.symbol))
            stock = stocks.setdefault((row.market, row.symbol), {
                "market": row.market,
                "symbol": row.symbol,
                "name": metadata.name if metadata else row.name,
                "_categories": _categories_of(metadata),
                **{field: 0 for field in _SHARE_FIELDS},
                "_priced_amount": 0.0,  # priced rows only, matching how period_summary sums amounts
                "estimated_net_amount_twd": 0.0,  # None as soon as any observed day lacked a price
                "_daily": {},
            })
            for field in _SHARE_FIELDS:
                stock[field] += getattr(row, field)
            amount = None if close is None else row.institutional_net_shares * close
            day = stock["_daily"].setdefault(trading_date, [0, 0.0])
            day[0] += row.institutional_net_shares
            if amount is None:
                stock["estimated_net_amount_twd"] = None
                day[1] = None
            else:
                stock["_priced_amount"] += amount
                if stock["estimated_net_amount_twd"] is not None:
                    stock["estimated_net_amount_twd"] += amount
                if day[1] is not None:
                    day[1] += amount
    by_amount = ranking_method == "estimated_amount"
    metric = "estimated_net_amount_twd" if by_amount else "institutional_net_shares"

    def daily_of(stock: Mapping[str, Any]) -> list[dict[str, Any]]:
        cells = [stock["_daily"].get(d, (0, 0.0)) for d in dates]
        return [
            {"trading_date": d, "institutional_net_shares": cell[0], "estimated_net_amount_twd": None if cell[1] is None else round(float(cell[1]), 2)}
            for d, cell in zip(dates, cells)
        ]

    result = []
    for category in requested:
        members = [stock for stock in stocks.values() if category in stock["_categories"]]
        measured = [stock for stock in members if stock[metric] is not None]
        sides = {
            "top_inflows": sorted((s for s in measured if s[metric] > 0), key=lambda s: (-s[metric], s["symbol"])),
            "top_outflows": sorted((s for s in measured if s[metric] < 0), key=lambda s: (s[metric], s["symbol"])),
        }
        item: dict[str, Any] = {
            "category": category,
            "is_broad": category in BROAD_CATEGORIES,
            "ranking_method": ranking_method,
            "top": top,
            "member_count": len(members),
            **{field: summary[category][field] for field in _TOTAL_FIELDS},
        }
        if category in BROAD_CATEGORIES:
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            overlap = 0
            for stock in members:
                # Another broad category is a sibling umbrella, never a sub-category.
                subs = [name for name in stock["_categories"] if name not in BROAD_CATEGORIES] or [ONLY_BROAD_GROUP]
                overlap += len(subs) > 1
                for other in subs:
                    groups[other].append(stock)
            rows = [
                {
                    "category": name,
                    "member_count": len(group),
                    **{field: sum(s[field] for s in group) for field in _SHARE_FIELDS},
                    "estimated_institutional_net_amount_twd": round(float(sum(s["_priced_amount"] for s in group)), 2),
                }
                for name, group in groups.items()
            ]
            key = "estimated_institutional_net_amount_twd" if by_amount else "institutional_net_shares"
            item["subcategories"] = sorted(rows, key=lambda row: (-row[key], row["category"]))
            item["subcategory_overlap"] = True
            item["subcategory_overlap_count"] = overlap
        for side, ordered in sides.items():
            denominator = sum(s[metric] for s in ordered)
            item[side] = [
                {
                    "market": s["market"],
                    "symbol": s["symbol"],
                    "name": s["name"],
                    **{field: s[field] for field in _SHARE_FIELDS},
                    "estimated_net_amount_twd": None if s["estimated_net_amount_twd"] is None else round(float(s["estimated_net_amount_twd"]), 2),
                    "share_of_side_pct": round(s[metric] / denominator * 100, 2),
                    "daily": daily_of(s),
                }
                for s in ordered[:top]
            ]
        result.append(item)
    return result


def build_sector_flow_report(
    *, cache_dir: Path, start_date: str, end_date: str, detail_categories: Sequence[str] = (), top: int = 10,
    max_days: int | None = 31,
) -> dict[str, Any]:
    """`max_days=None` lifts the CLI's 31-day cap for offline callers that need longer windows (the web tab)."""
    requested_dates = _dates(start_date, end_date, max_days)
    taxonomy = load_taxonomy(cache_dir, end_date=end_date)
    source_status: dict[str, dict[str, Any]] = {}
    day_inputs: list[tuple[str, list[InstitutionalFlowRow], dict[tuple[str, str], float]]] = []
    all_closes_by_symbol: dict[str, float] = {}
    total_flow_rows = 0
    total_priced_rows = 0
    mapped_rows = 0
    incomplete_source = False
    incomplete_notes: list[str] = []
    dates_without_data: list[str] = []

    for trading_date in requested_dates:
        source_status[trading_date] = {}
        twse_flow, status = _load_source(cache_dir, "twse", "T86", parse_twse_institutional, trading_date)
        source_status[trading_date]["twse_institutional"] = status
        tpex_flow, status = _load_source(cache_dir, "tpex", "institutional", parse_tpex_institutional, trading_date)
        source_status[trading_date]["tpex_institutional"] = status
        twse_close, status = _load_source(cache_dir, "twse", "MI_INDEX", parse_twse_closes, trading_date)
        source_status[trading_date]["twse_close"] = status
        tpex_close, status = _load_source(cache_dir, "tpex", "daily_close", parse_tpex_closes, trading_date)
        source_status[trading_date]["tpex_close"] = status
        flows = list(twse_flow) + list(tpex_flow)
        states = source_status[trading_date]
        if all(states[key]["state"] in ("missing", "no_data") for key in states):
            # No holiday calendar: a day with no usable source is "no data", not a failure.
            # A schema_error is never a holiday, so it falls through and degrades the report.
            dates_without_data.append(trading_date)
            continue
        bad = [f"{key}={states[key]['state']}" for key in states if states[key]["state"] != "ok"]
        if bad:
            incomplete_source = True
            incomplete_notes.append(f"incomplete sources on {trading_date}: {', '.join(bad)}")
        if not flows:
            continue
        closes = {(row.market, row.symbol): row.close for row in list(twse_close) + list(tpex_close)}
        for row in list(twse_close) + list(tpex_close):
            all_closes_by_symbol[row.symbol] = row.close
        total_flow_rows += len(flows)
        total_priced_rows += sum(1 for row in flows if (row.market, row.symbol) in closes)
        mapped_rows += sum(1 for row in flows if row.symbol in taxonomy and taxonomy[row.symbol].categories)
        day_inputs.append((trading_date, flows, closes))

    price_coverage = total_priced_rows / total_flow_rows if total_flow_rows else 0.0
    taxonomy_coverage = mapped_rows / total_flow_rows if total_flow_rows else 0.0
    daily_coverages = [sum(1 for row in flows if (row.market, row.symbol) in closes) / len(flows) for _, flows, closes in day_inputs]
    prices_ok = bool(daily_coverages) and min(daily_coverages) >= PRICE_COVERAGE_MIN
    ranking_method = "estimated_amount" if prices_ok else "net_shares"
    low_days = [f"{day[0]} ({coverage:.1%})" for day, coverage in zip(day_inputs, daily_coverages) if coverage < PRICE_COVERAGE_MIN]
    daily = [
        {
            "trading_date": trading_date,
            "price_coverage": sum(1 for row in flows if (row.market, row.symbol) in closes) / len(flows),
            "categories": _aggregate_day(flows, closes, taxonomy, ranking_method),
        }
        for trading_date, flows, closes in day_inputs
    ]
    if not total_flow_rows:
        status = "blocked"
    elif incomplete_source or not prices_ok or taxonomy_coverage < 1.0:
        status = "degraded"
    else:
        status = "ok"
    warnings = list(incomplete_notes)
    if low_days and total_flow_rows:
        warnings.append(f"price coverage below 90% on {', '.join(low_days)}; rankings use exact net shares")
    if taxonomy_coverage < 1.0 and total_flow_rows:
        warnings.append("taxonomy mapping incomplete; unmatched symbols are 未分類")
    holdings, holding_errors = _load_holdings(cache_dir, end_date)
    # Only a corrupt snapshot that would be one of the two compared blocks the proxy;
    # ingest only fetches the latest week, so an old corrupt file could never be repaired.
    compared = sorted(set(holdings) | {item["as_of_date"] for item in holding_errors})[-2:]
    holding_errors = [item for item in holding_errors if item["as_of_date"] in compared]
    universe = {row.symbol for _, flows, _ in day_inputs for row in flows}
    if holding_errors:
        large_holder: dict[str, Any] = {"status": "schema_error", "reason": "unreadable_tdcc_snapshot", "errors": holding_errors}
        warnings.append(f"TDCC snapshot unreadable: {', '.join(item['as_of_date'] for item in holding_errors)}")
        status = "degraded" if status != "blocked" else status
    else:
        large_holder = build_large_holder_proxy(holdings_by_date=holdings, taxonomy=taxonomy, closes=all_closes_by_symbol, end_date=end_date, start_date=start_date, universe=universe)
    period_summary = _aggregate_period(day_inputs, taxonomy, ranking_method)
    report = {
        "schema_version": 1,
        "requested_period": {"start_date": start_date, "end_date": end_date},
        "observed_trading_dates": [item[0] for item in day_inputs],
        "status": status,
        "ranking_method": ranking_method,
        "dates_without_data": sorted(dates_without_data),
        "category_overlap": True,
        "category_overlap_note": CATEGORY_OVERLAP_NOTE,
        "source_status": source_status,
        "taxonomy": {"snapshot_date": next(iter(taxonomy.values())).snapshot_date if taxonomy else None, "mapped_rows": mapped_rows, "total_rows": total_flow_rows, "coverage": taxonomy_coverage},
        "price_coverage": price_coverage,
        "daily": daily,
        "period_summary": period_summary,
        "large_holder": large_holder,
        "exclusions": {"symbol_rule": "^[1-9][0-9]{3}$; excludes ^91[0-9]{2}$ (Taiwan depositary receipts)"},
        "warnings": warnings,
    }
    if detail_categories:
        report["category_detail"] = build_category_detail(
            day_inputs=day_inputs, taxonomy=taxonomy, ranking_method=ranking_method,
            period_summary=period_summary, categories=detail_categories, top=top,
        )
    return report
