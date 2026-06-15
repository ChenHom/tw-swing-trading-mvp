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


def cmd_strategy_inspect(args):
    settings = common.get_settings()
    strategy_id = common._resolve_strategy_id_from_path(settings, args.config_path)
    try:
        defn = common.strategy_registry.load_strategy_definition(settings, strategy_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"載入策略設定失敗: {e}")
        sys.exit(1)

    print(f"strategy_id: {defn.strategy_id}")
    print(f"strategy_version: {defn.strategy_version}")
    print("canonicalization: strategy-params-v1")
    print(f"params_hash: {defn.params_hash}  (含 exit: 區塊)")
    print(f"order_budget_twd: {defn.order_budget_twd}")
    if defn.exit_params:
        print(f"exit: {defn.exit_params.model_dump()}")
    else:
        print("exit: 無（不受 risk_exit 監控）")


