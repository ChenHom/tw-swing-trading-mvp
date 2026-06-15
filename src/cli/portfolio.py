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


def cmd_portfolio_reconcile(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    account_id = common.resolve_account_id(conn, args.account)
    print(f"Reconciling account {account_id}...")
    result = projection.reconcile(account_id)
    # reconcile() 成功回傳 {"status": "RECONCILE_OK"}，失敗回傳含具體不一致欄位的 dict。
    # （舊版誤用 `if not errors:` 判斷，非空的成功 dict 恆為真而永遠誤報失敗。）
    if not result or (isinstance(result, dict) and result.get("status") == "RECONCILE_OK"):
        print("Reconciliation successful: cash ledger balances match projections.")
    else:
        print("Reconciliation failed! Errors found:")
        print(f"  - {result}")
        sys.exit(1)
    conn.close()


def cmd_portfolio_rebuild_projections(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    account_id = common.resolve_account_id(conn, args.account)
    print(f"Rebuilding projections for account {account_id}...")
    projection.rebuild_from_ledger(account_id)
    print("Projections successfully rebuilt from ledger facts.")
    conn.close()


