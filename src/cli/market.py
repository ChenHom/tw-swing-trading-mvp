import argparse
import sys
import os
import fcntl
import json
import hashlib
import sqlite3
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.config import AppSettings
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.portfolio.ledger import PortfolioLedger
from src.contracts.models import (
    TrendPullbackParams, StrategyApprovalManifest, StrategyInfo, LimitsInfo,
    ValidityInfo, IntegrityInfo, PermissionsInfo, DailySignalBundle, SignalItem, ExecutionContext
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.market_data.provider import ShioajiMarketDataProvider
from src.application.runners.backtest import BacktestRunner
from src.application.runners.simulation import DailySimulationRunner, EntryStrategySpec
from src.approval.validator import ManifestValidator
from src.approval.store import load_active_manifests, activate_manifest, deactivate_strategy
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.strategy import registry as strategy_registry
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.trading.planner import OrderPlanner, PortfolioState
from src.trading.allocator import GlobalLimits
from src.broker.fake_broker import FakeBroker
from src.application.execution.engine import TradeExecutionEngine
from src.application.services import trade_write
from src.cli import common


def cmd_market_backfill(args):
    settings = common.get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    provider = ShioajiMarketDataProvider(settings.shioaji_api_key, settings.shioaji_secret_key)
    runner = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=provider,
        market_repo=repo, projection=PortfolioProjection(conn),
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals
    )
    
    today_dt = date.today()
    start_date = today_dt - timedelta(days=args.calendar_days)

    sessions = calendar.sessions_between(start_date, today_dt)
    print(f"Backfilling {len(sessions)} trading sessions from {start_date} to {today_dt}...")

    # Indices first (market-regime MA filters depend on them), then stocks.
    # skip_missing_symbols: 上市前無資料的個股/ETF 只跳過該檔，不阻斷整個日期。
    sync_specs = list(settings.universe.indices) + list(settings.universe.symbols)

    success_count = 0
    for s_date in sessions:
        print(f"Syncing market data for {s_date}...")
        if runner.sync_market_data(s_date, sync_specs, skip_missing_symbols=True):
            success_count += 1
            
    print(f"Successfully sync'd {success_count} / {len(sessions)} days.")
    conn.close()


def cmd_market_backfill_history(args):
    """research.db 深歷史回補（P0-T9）：FinMind 主 + 證交所補缺口，canonical raw bar + 股利事件。"""
    from src.config import SymbolConfig
    from src.market_data.finmind_provider import FinMindProvider
    from src.market_data.twse_provider import TwseProvider

    settings = common.get_settings()
    db_path = args.db or "data/research.db"
    init_db(db_path)
    conn = get_db_connection(db_path)
    repo = SqliteMarketBarRepository(conn)
    calendar = ExchangeCalendarsTradingCalendar()

    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    sessions = set(calendar.sessions_between(start_date, end_date))

    finmind = FinMindProvider() if args.source in ("finmind", "auto") else None
    twse = TwseProvider() if args.source in ("twse", "auto") else None

    # roster 模式：用 FinMind 全市場 roster（含部分下市股）枚舉候選股，供 PIT 流動性 universe；
    # raw-only、跳股利（廣度池只需 raw 成交額排名，CA 之後按需補；核心 universe.yaml 那批已有股利）。
    roster_mode = getattr(args, "roster", None) == "twse"
    if roster_mode:
        if finmind is None:
            print("ERROR: --roster 需要 FinMind 來源（--source finmind 或 auto）")
            conn.close()
            return
        roster = finmind.fetch_twse_roster()
        print(f"Roster: TaiwanStockInfo twse = {len(roster)} 檔（含部分下市股；raw-only、跳股利）")
        symbol_specs = [SymbolConfig(code=c, exchange="TSE", instrument_type="STOCK") for c in roster]
    elif args.symbols:
        codes = [c.strip() for c in args.symbols.split(",") if c.strip()]
        known = {s.code: s for s in list(settings.universe.symbols) + list(settings.universe.indices)}
        symbol_specs = [known.get(c, SymbolConfig(code=c, exchange="TSE", instrument_type="STOCK")) for c in codes]
    else:
        symbol_specs = list(settings.universe.indices) + list(settings.universe.symbols)

    excluded_symbols = []
    fetched_counts = {}

    for spec in symbol_specs:
        symbol, exchange, instrument_type = spec.code, spec.exchange, spec.instrument_type
        print(f"Backfilling {symbol} ({exchange}/{instrument_type}) {start_date} ~ {end_date}...")
        bars_by_date = {}

        if finmind is not None:
            for bar in finmind.fetch_raw_price(symbol, start_date, end_date, exchange, instrument_type):
                bars_by_date[bar.trade_date] = bar

        missing = sessions - set(bars_by_date.keys())
        if twse is not None and (args.source == "twse" or missing):
            for bar in twse.fetch_range(symbol, start_date, end_date, exchange, instrument_type):
                bars_by_date.setdefault(bar.trade_date, bar)

        for bar in bars_by_date.values():
            repo.upsert_canonical(bar)
        fetched_counts[symbol] = len(bars_by_date)
        if not bars_by_date:
            excluded_symbols.append(symbol)

        if finmind is not None and not roster_mode:
            for action in finmind.fetch_dividend_events(symbol, start_date, end_date):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO corporate_actions
                    (action_id, symbol, action_type, ex_date, cash_per_share, stock_ratio,
                     source, effective_date, known_at, ingested_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                    """,
                    (
                        action["action_id"], action["symbol"], action["action_type"], action["ex_date"],
                        action.get("cash_per_share"), action.get("stock_ratio"), action["source"],
                        action.get("effective_date"), action.get("known_at"),
                    )
                )
            conn.commit()

    print(f"\n回補完成：{len(symbol_specs)} 檔 x {len(sessions)} 交易日窗。")
    for symbol, count in fetched_counts.items():
        print(f"  {symbol}: {count}/{len(sessions)} 筆")
    print(f"剔除清單（整窗無任何資料）：{', '.join(excluded_symbols) if excluded_symbols else '（無）'}")
    conn.close()


def cmd_market_build_universe(args):
    """建 PIT 流動性 top-N universe_policy（R-T4b Track 2）：月再平衡、成交額排序、known_at<=as_of。"""
    from src.market_data.liquidity_universe import build_liquidity_policy

    db_path = args.db or "data/research.db"
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        stats = build_liquidity_policy(
            conn,
            policy_version=args.policy_version,
            start_date=date.fromisoformat(args.start_date),
            end_date=date.fromisoformat(args.end_date),
            top_n=args.top_n,
            lookback_sessions=args.lookback,
        )
    except ValueError as e:
        print(f"ERROR: {e}")
        conn.close()
        sys.exit(1)
    print(
        f"universe_policy 建立完成：policy='{stats['policy_version']}' "
        f"再平衡 {stats['rebalances']} 次、寫入 {stats['rows']} 列、涵蓋 {stats['distinct_symbols']} 檔不同標的"
        + (f"（{stats.get('first_rebalance')} ~ {stats.get('last_rebalance')}）" if stats['rebalances'] else "")
    )
    conn.close()


def cmd_market_sync(args):
    settings = common.get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    provider = ShioajiMarketDataProvider(settings.shioaji_api_key, settings.shioaji_secret_key)
    runner = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=provider,
        market_repo=repo, projection=PortfolioProjection(conn),
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals
    )
    
    sync_date = date.fromisoformat(args.date) if args.date else date.today()
    strategy_symbols = [s.code for s in settings.universe.symbols]
    symbols = common.get_valuation_universe(conn, strategy_symbols)
    sync_specs = symbols + list(settings.universe.indices)

    print(f"Syncing market data for {sync_date}...")
    if runner.sync_market_data(sync_date, sync_specs):
        print(f"Market data for {sync_date} sync'd successfully.")
    else:
        print(f"Failed to sync market data for {sync_date}.")
        sys.exit(1)
    conn.close()


def cmd_market_validate(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    
    today_dt = date.today()
    sessions = calendar.sessions_between(today_dt - timedelta(days=args.last_sessions * 2), today_dt)
    sessions = sessions[-args.last_sessions:]
    
    symbols = [s.code for s in settings.universe.symbols] + [s.code for s in settings.universe.indices]

    print(f"Validating last {len(sessions)} trading sessions...")
    missing_count = 0
    for s in sessions:
        for symbol in symbols:
            bar = repo.find(symbol, s)
            if not bar:
                print(f"Missing data: {symbol} on {s}")
                missing_count += 1
                
    if missing_count == 0:
        print("Validation successful: all sessions are complete.")
    else:
        print(f"Validation failed: {missing_count} missing bars.")
        sys.exit(1)
    conn.close()


