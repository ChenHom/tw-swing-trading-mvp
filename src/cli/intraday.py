"""Explicit, isolated CLI for the read-only Tick collector.

No daemon/service installation and no live connection by default.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path

from src.market_data.intraday_collector import (
    ShioajiTickCollector, build_subscriptions, write_health,
)
from src.market_data.intraday_tick import (
    RawTickStore, TAIPEI, build_minute_bars, compress_raw, replay_ticks,
)


def _symbols_from_file(path: str | None) -> list[str]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("symbols", data.get("candidates", data.get("signals")))
    if not isinstance(data, list):
        raise ValueError("symbol input must be JSON list or {symbols|candidates|signals:[...]}")
    result = []
    for item in data:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and ("symbol" in item or "code" in item):
            result.append(str(item.get("symbol") or item.get("code")))
        else:
            raise ValueError("invalid symbol entry")
    return result


def _read_positions(db_path: str, accounts: list[str]) -> list[str]:
    """Read existing positions via the repo projection, without mutating app.db."""
    if not accounts:
        return []
    from src.portfolio.projection import PortfolioProjection
    # Connection opened read-only; no schema initialization or migrations.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        projection = PortfolioProjection(conn)
        result = []
        for account in accounts:
            for (_strategy_id, symbol), position in projection.get_strategy_positions(
                account, include_long_term=True
            ).items():
                if position["quantity"] > 0:
                    result.append(symbol)
        return result
    finally:
        conn.close()


def scope_for_args(args):
    positions = _read_positions(args.db, args.positions_accounts or [])
    candidates = _symbols_from_file(args.candidates_file)
    watchlist = _symbols_from_file(args.watchlist_file)
    watchlist += [symbol.strip() for symbol in (args.symbols or "").split(",") if symbol.strip()]
    specs, source_audit = build_subscriptions(
        positions=positions, candidates=candidates, watchlist=watchlist,
        lot_types=("BOARD", "ODD"), max_symbols=args.max_symbols,
    )
    return specs, source_audit


def cmd_intraday_scope(args) -> None:
    specs, sources = scope_for_args(args)
    print(json.dumps({
        "mode": "PLAN_ONLY", "quote_type": "Tick",
        "symbols": sources,
        "subscriptions": [{"symbol": s.symbol, "lot_type": s.lot_type} for s in specs],
        "subscription_count": len(specs),
        "sdk_subscription_limit_verified": False,
        "live_login": False,
    }, indent=2, ensure_ascii=False))


def cmd_intraday_replay(args) -> None:
    ticks, rejected = replay_ticks(Path(args.input))
    bars = build_minute_bars(ticks)
    result = {
        "schema_version": 1, "source_path": str(args.input), "accepted": len(ticks),
        "rejected": rejected, "ticks": [tick.as_dict() for tick in ticks],
        "minute_bars": bars,
    }
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


def cmd_intraday_compress(args) -> None:
    print(json.dumps(compress_raw(Path(args.input)), ensure_ascii=False))


def cmd_intraday_collect(args) -> None:
    """One-off live read-only smoke ONLY; not intended for unattended operation."""
    if not args.enable_live_smoke or os.environ.get("INTRADAY_LIVE_SMOKE_APPROVED") != "yes":
        raise RuntimeError("Live quote smoke blocked: explicit flag and approval environment are required")
    if args.duration_seconds < 1 or args.duration_seconds > 60:
        raise ValueError("one-off smoke duration must be 1..60 seconds")
    from src.calendar.calendar import ExchangeCalendarsTradingCalendar
    now = datetime.now(TAIPEI)
    if not ExchangeCalendarsTradingCalendar().is_trading_day(now.date()):
        raise RuntimeError("outside exchange trading day")
    if not clock_time(8, 30) <= now.time() <= clock_time(13, 40):
        raise RuntimeError("outside intraday read-only smoke window")
    specs, sources = scope_for_args(args)
    if not specs:
        raise ValueError("empty candidate scope")
    # Delayed import ensures plan/replay/tests never import native SDK.
    import shioaji as sj
    from src.config import AppSettings
    settings = AppSettings()
    if not settings.shioaji_api_key or not settings.shioaji_secret_key:
        raise RuntimeError("Shioaji quote credentials not configured")
    # Real-time quotes require production market feed; trading CA is NEVER activated.
    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=settings.shioaji_api_key,
        secret_key=settings.shioaji_secret_key,
        fetch_contract=True,
        subscribe_trade=False,
    )
    collector = ShioajiTickCollector(
        api, subscriptions=specs, quote_type=sj.QuoteType.Tick,
        store=RawTickStore(Path(args.cache_dir), stop_at_disk_pct=args.stop_at_disk_pct),
    )
    status_path = Path(args.cache_dir) / "shioaji" / "collector_health.json"
    try:
        collector.start()
        time.sleep(args.duration_seconds)
    finally:
        try:
            collector.stop()
            write_health(status_path, collector.health())
        finally:
            api.logout()
    result = {"mode": "ONE_OFF_READ_ONLY_SMOKE", "scope": sources, "health": collector.health()}
    print(json.dumps(result, indent=2, ensure_ascii=False))
