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


