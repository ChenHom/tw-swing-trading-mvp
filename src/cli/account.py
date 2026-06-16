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


def cmd_account_init(args):
    settings = common.get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    
    account_id = common.resolve_account_id(conn, args.account)
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    
    run_id = f"init-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    ledger.deposit(account_id, run_id, args.initial_cash, "TWD", date.today())
    projection.rebuild_from_ledger(account_id)
    
    print(f"Account '{account_id}' initialized with {args.initial_cash} TWD.")
    conn.close()


def cmd_account_adjust_cash(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    cursor = conn.cursor()
    
    account_id = common.resolve_account_id(conn, args.account)
    amount = args.amount
    
    try:
        # Delete all existing INITIAL_DEPOSIT records for this account
        cursor.execute(
            "DELETE FROM cash_ledger WHERE account_id = ? AND event_type = 'INITIAL_DEPOSIT'",
            (account_id,)
        )
        
        # Insert new single INITIAL_DEPOSIT
        from src.portfolio.ledger import PortfolioLedger
        from src.portfolio.projection import PortfolioProjection
        
        ledger = PortfolioLedger(conn)
        projection = PortfolioProjection(conn)
        
        run_id = f"adjust-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        ledger.deposit(account_id, run_id, amount, "TWD", date.today())
        projection.rebuild_from_ledger(account_id)
        
        print(f"成功將帳戶 '{account_id}' 的初始金調整為：{amount:,} TWD (所有先前的初始入金已清除，可用現金已重新對帳更新)")
    except Exception as e:
        print(f"調整剩餘金額失敗: {e}")
        sys.exit(1)
    finally:
        conn.close()


def cmd_account_adjust(args):
    """Append-only 現金異動：寫一筆 CASH_ADJUSTMENT 事件（提領為負、補入為正），
    不改寫既有初始入金。與 adjust-cash（重設初始金）不同。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)

    account_id = common.resolve_account_id(conn, args.account)
    amount = args.amount

    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)

    before = projection.get_cash_balance(account_id)
    run_id = f"adjust-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        ledger.adjust_cash(account_id, run_id, amount, "TWD", date.today(), memo=args.reason)
        projection.rebuild_from_ledger(account_id)
        after = projection.get_cash_balance(account_id)
        verb = "提領" if amount < 0 else "補入"
        print(f"已對帳戶 '{account_id}' {verb} {abs(amount):,} TWD（原因：{args.reason}）")
        print(f"可用現金：{before:,} → {after:,} TWD")

        result = projection.reconcile(account_id)
        status = result.get("status")
        if status == "RECONCILE_OK":
            print("✅ 對帳通過：現金流水與投影一致。")
        else:
            print(f"⚠ 對帳異常：{result}")
            sys.exit(1)
    except Exception as e:
        print(f"現金異動失敗: {e}")
        sys.exit(1)
    finally:
        conn.close()


