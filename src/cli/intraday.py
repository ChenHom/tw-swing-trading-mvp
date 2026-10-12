"""Explicit, isolated CLI for the read-only Tick collector.

No daemon/service installation and no live connection by default.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, time as clock_time
from pathlib import Path

from src.market_data.intraday_storage import archive_closed_sessions, disk_status, retention_audit
from src.market_data.intraday_collector import (
    ShioajiTickCollector, build_subscriptions, write_health,
)
from src.market_data.intraday_tick import (
    RawTickStore, TAIPEI, build_minute_bars, compress_raw, replay_ticks,
)
from src.market_data.intraday_book import RawBookStore


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


def _quote_types(sj):
    """Resolve the quote enum across Shioaji SDK layouts, without trading APIs.

    Older Shioaji versions publish QuoteType under shioaji.constant;
    some newer builds export it at package root. The official SDK also
    accepts the quote-type strings when neither enum is exported.
    """
    types = getattr(sj, "QuoteType", None)
    if types is None:
        types = getattr(getattr(sj, "constant", None), "QuoteType", None)
    if types is None:
        try:
            from shioaji.constant import QuoteType as types
        except ImportError:
            types = None
    if types is not None:
        return types.Tick, types.BidAsk
    return "tick", "bid_ask"


def _legacy_quote_version(sj):
    """Select v1 callbacks for the older api.quote.subscribe SDK surface."""
    versions = getattr(sj, "QuoteVersion", None)
    if versions is None:
        versions = getattr(getattr(sj, "constant", None), "QuoteVersion", None)
    return getattr(versions, "v1", None)


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
    # TEMPORARY: this smoke entry is pinned to the Shioaji simulation feed.
    # Real-market verification needs a separate reviewed code change.
    # Resolve the SDK quote types before login: older versions only export
    # shioaji.constant.QuoteType, not shioaji.QuoteType.
    tick_type, book_type = _quote_types(sj)
    api = sj.Shioaji(simulation=True)
    # Explicit opt-in only. Both sinks are called after raw append and
    # canonical normalization on the same Collector worker.
    publisher = None
    if getattr(args, "snapshot_output", None):
        from src.application.services.intraday_snapshot_publisher import IntradaySnapshotPublisher
        publisher = IntradaySnapshotPublisher(
            Path(args.snapshot_output),
            interval_seconds=getattr(args, "snapshot_interval_seconds", 5.0),
        )
    collector = None
    try:
        try:
            api.login(
                api_key=settings.shioaji_api_key,
                secret_key=settings.shioaji_secret_key,
                fetch_contract=True,
                subscribe_trade=False,
            )
        except Exception as exc:
            # Shioaji SDK login errors can include account/person IDs and IPs.
            raise RuntimeError(
                f"Shioaji simulation login failed ({type(exc).__name__}); "
                "broker error detail suppressed"
            ) from None
        collector = ShioajiTickCollector(
            api, subscriptions=specs, quote_type=tick_type,
            store=RawTickStore(Path(args.cache_dir), stop_at_disk_pct=args.stop_at_disk_pct),
            book_quote_type=book_type if args.with_bidask else None,
            book_store=RawBookStore(Path(args.cache_dir), stop_at_disk_pct=args.stop_at_disk_pct)
            if args.with_bidask else None,
            legacy_quote_version=_legacy_quote_version(sj),
            sink=publisher.on_tick if publisher is not None else None,
            book_sink=publisher.on_book if publisher is not None and args.with_bidask else None,
        )
        status_path = Path(args.cache_dir) / "shioaji" / "collector_health.json"
        try:
            collector.start()
            if publisher is None:
                time.sleep(args.duration_seconds)
            else:
                # Only the CLI thread performs snapshot file IO. Callbacks and
                # raw writer never wait on a Web snapshot write.
                until = time.monotonic() + args.duration_seconds
                while time.monotonic() < until:
                    publisher.publish(collector.health())
                    if collector.health().get("state") == "FAILED":
                        break
                    time.sleep(min(1.0, max(0.0, until - time.monotonic())))
        finally:
            try:
                collector.stop()
            finally:
                # Publish CLOSED/FAILED as the last snapshot, not a misleading
                # perpetual RUNNING view after this short simulation smoke.
                if publisher is not None:
                    try:
                        publisher.publish(collector.health(), force=True)
                    finally:
                        write_health(status_path, collector.health())
                else:
                    write_health(status_path, collector.health())
        health = collector.health()
        counters = health.get("counters", {})
        checks = {
            "clean_shutdown": health.get("state") == "CLOSED",
            "subscriptions_released": health.get("active_subscription_count") == 0,
            "queue_drained": health.get("queue_depth") == 0,
            "raw_capture_without_loss": health.get("collection_status") == "RAW_CAPTURED",
            "tick_observed": counters.get("raw_written", 0) > 0,
            "bidask_observed": (not args.with_bidask) or counters.get("book_raw_written", 0) > 0,
        }
        result = {
            "mode": "ONE_OFF_SIMULATION_QUOTE_SMOKE",
            "simulation": True,
            "real_market_verified": False,
            "smoke_status": "PASS_CAPTURE_ONLY" if all(checks.values()) else "FAIL",
            "smoke_checks": checks,
            "sdk_version": str(getattr(sj, "__version__", "unknown")),
            "scope": sources,
            "health": health,
        }
    finally:
        # Failed startup or quote-type errors must not leave SDK sessions open.
        # If earlier failure is already propagating, keep the original cause.
        pending_error = sys.exc_info()[0] is not None
        try:
            api.logout()
        except Exception as exc:
            if not pending_error:
                raise RuntimeError(
                    f"Shioaji simulation logout failed ({type(exc).__name__})"
                ) from None
    # Print only after SDK logout has succeeded. Never exit zero on a capture
    # with unresolved subscriptions, worker loss, or missing raw stream.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["smoke_status"] != "PASS_CAPTURE_ONLY":
        raise SystemExit(1)


def cmd_intraday_audit(args) -> None:
    """Strict offline raw quality audit: does not access credentials or the broker."""
    from src.market_data.intraday_audit import audit_intraday_raw

    required = ("tick_board", "tick_odd", "bidask_board", "bidask_odd")
    counts = [getattr(args, "expected_" + field) for field in required]
    if any(value is not None for value in counts) and not all(value is not None for value in counts):
        raise ValueError("provide all four --expected-* counts or none")
    expected = None
    if all(value is not None for value in counts):
        expected = {
            "ticks_BOARD": args.expected_tick_board,
            "ticks_ODD": args.expected_tick_odd,
            "bidask_BOARD": args.expected_bidask_board,
            "bidask_ODD": args.expected_bidask_odd,
        }
    result = audit_intraday_raw(
        Path(args.cache_dir), symbol=args.symbol, trading_date=args.date,
        expected=expected,
    )
    report = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report, encoding="utf-8")
    print(report, end="")
    if result["status"] != "PASS_RAW_ONLY":
        raise SystemExit(1)


def cmd_intraday_maintain(args) -> None:
    """Explicit offline post-close maintenance. No purge and no broker login."""
    from src.calendar.calendar import ExchangeCalendarsTradingCalendar
    before = date.fromisoformat(args.before_date)
    now = datetime.now(TAIPEI)
    if before > now.date():
        raise ValueError("cannot rotate before a future trading date")
    if before == now.date() and now.time() < clock_time(14, 0):
        raise RuntimeError("refuse to archive intraday file before market close")
    root = Path(args.cache_dir)
    calendar = ExchangeCalendarsTradingCalendar()
    status = disk_status(root, warn_pct=args.warn_pct, stop_pct=args.stop_pct)
    archival = archive_closed_sessions(root, before_date=before,
                                       is_trading_day=calendar.is_trading_day)
    policy = retention_audit(root, as_of=before,
                             is_trading_day=calendar.is_trading_day,
                             minimum_sessions=180)
    print(json.dumps({
        "mode": "OFFLINE_MAINTENANCE_NO_PURGE", "disk": status,
        "archive": archival, "retention": policy,
        "service_scheduled": False, "broker_login": False,
    }, ensure_ascii=False, indent=2))
    if archival["blocked"] or status["stop"]:
        raise RuntimeError("raw maintenance requires review or disk cleanup; no data deleted")
