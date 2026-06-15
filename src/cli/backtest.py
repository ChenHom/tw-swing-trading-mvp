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


def cmd_backtest_run(args):
    settings = common.get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)

    strategy_id = args.strategy
    manifests = common.load_active_manifests(settings)
    manifest = manifests.get(strategy_id)
    if not manifest:
        print(f"Error: 策略 '{strategy_id}' 無有效授權。請先執行 'approval create' + 'approval activate'。")
        sys.exit(1)

    try:
        defn = common.strategy_registry.load_strategy_definition(settings, strategy_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"載入策略設定失敗: {e}")
        sys.exit(1)

    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    symbols = [s.code for s in settings.universe.symbols]
    index_symbol = settings.trading.pipeline.index_symbol

    entry_spec = EntryStrategySpec(
        definition=defn,
        strategy=common.strategy_registry.build_entry_strategy(defn, symbols, index_symbol)
    )
    exit_definitions = common.strategy_registry.load_exit_managed_definitions(settings)

    runner = BacktestRunner(
        db_conn=conn, calendar=calendar, market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifest=manifest, strategy_budget=defn.order_budget_twd,
        slippage_bps=settings.backtest.slippage_bps,
        exit_definitions=exit_definitions,
        index_symbols=list(settings.universe.indices)
    )

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.to)

    print(f"Running backtest [{strategy_id}] from {start_date} to {end_date}...")
    result = runner.run(
        start_date=start_date,
        end_date=end_date,
        initial_cash=args.initial_cash,
        universe_symbols=symbols,
        entry_spec=entry_spec
    )
    
    stats = result["statistics"]
    print("\n--- Backtest Statistics ---")
    print(f"Final Equity: {stats['final_equity']:,} TWD")
    print(f"Total PnL: {stats['total_pnl']:+,} TWD ({stats['total_pnl_bps']/100:+.2f}%)")
    print(f"Max Drawdown: {stats['max_drawdown']*100:.2f}%")
    print(f"Trades count: {stats['trade_count']}")
    print(f"Win Rate: {stats['win_rate']*100:.2f}%")
    print(f"Profit Factor: {stats['profit_factor']:.2f}")
    conn.close()


