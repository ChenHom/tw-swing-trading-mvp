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


def cmd_simulation_run_daily(args):
    settings = common.get_settings()
    init_db(settings.trading.database_path)
    
    # Process-level file lock to prevent concurrent daily simulation runs
    lock_path = Path(settings.trading.database_path).parent / "simulation_daily.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("錯誤：另一個 simulation run-daily 進程正在執行中，請等待其結束後再重試。(SIMULATION_ALREADY_RUNNING)")
        lock_file.close()
        sys.exit(1)
    
    try:
        conn = get_db_connection(settings.trading.database_path)

        manifests = common.load_active_manifests(settings)
        account_id = common.resolve_account_id(conn, args.account)
        run_date = date.fromisoformat(args.date) if args.date else date.today()
        symbols = common.resolve_run_universe(settings, conn, account_id, run_date)
        try:
            entry_specs, exit_definitions = common.build_pipeline(settings, symbols, account_id)
        except (FileNotFoundError, ValueError) as e:
            print(f"載入策略管線失敗: {e}")
            sys.exit(1)

        repo = SqliteMarketBarRepository(conn)
        projection = PortfolioProjection(conn)
        calendar = ExchangeCalendarsTradingCalendar()
        provider = ShioajiMarketDataProvider(settings.shioaji_api_key, settings.shioaji_secret_key)

        runner = DailySimulationRunner(
            db_conn=conn, calendar=calendar, market_provider=provider,
            market_repo=repo, projection=projection,
            allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
            manifests=manifests,
            entry_specs=entry_specs,
            exit_definitions=exit_definitions,
            global_limits=common.build_global_limits(settings, account_id),
            index_symbols=list(settings.universe.indices),
            slippage_bps=settings.backtest.slippage_bps,
            expiry_warning_sessions=settings.trading.approval.expiry_warning_sessions,
            unlimited_open_positions=common.is_unlimited_positions_account(settings, account_id),
            cash_fraction_per_order=common.get_dynamic_order_sizing_fraction(settings, account_id)
        )

        # Per-strategy manifest preflight check
        print("--- 策略授權 preflight 查驗 ---")
        for spec in entry_specs:
            sid = spec.definition.strategy_id
            manifest = manifests.get(sid)
            preflight_status = runner.get_preflight_status(run_date, manifest)
            print(f"[{sid}] 授權: {manifest.approval_id if manifest else '無'} | 狀態: {preflight_status}")
            if preflight_status in ["EXPIRED", "REVOKED", "MISSING", "INVALID"]:
                print(f"  警告：授權狀態異常 ({preflight_status})！該策略的買入委託 (BUY) 將被阻擋；風險退出 (SELL) 不受影響。")
            elif preflight_status == "EXPIRING_SOON":
                print(f"  提示：授權即將過期。請儘速更新策略授權清單。")
        print("-------------------------------\n")

        print(f"Running daily multi-strategy simulation workflow for {run_date}...")
        status = runner.run_daily(
            run_date=run_date,
            account_id=account_id,
            universe_symbols=symbols,
            auto_execute=not getattr(args, "no_auto_execute", False)
        )

        print(f"Simulation runner finished with status: {status}")
        if status == "FAILED":
            sys.exit(1)
        conn.close()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def cmd_simulation_reset(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    cursor = conn.cursor()
    
    run_date = args.date
    print(f"Resetting daily simulation status and generated signal data for date {run_date}...")
    
    try:
        # Check if execution fills exist on this date
        cursor.execute(
            """
            SELECT COUNT(*) as cnt FROM fills
            WHERE date(filled_at) = ?
            """,
            (run_date,)
        )
        fill_count = cursor.fetchone()["cnt"]
        if fill_count > 0:
            print(f"Error: RESET_BLOCKED_EXECUTION_FACTS_EXIST. Cannot reset simulation state for {run_date} because {fill_count} execution fills already exist on this date.")
            conn.close()
            sys.exit(1)

        # Delete signal items first to avoid orphan rows (even if not strictly constrained)
        cursor.execute(
            "DELETE FROM signal_items WHERE bundle_id IN (SELECT bundle_id FROM signal_bundles WHERE signal_date = ?)",
            (run_date,)
        )
        items_deleted = cursor.rowcount
        
        # Delete signal bundles
        cursor.execute(
            "DELETE FROM signal_bundles WHERE signal_date = ?",
            (run_date,)
        )
        bundles_deleted = cursor.rowcount
        
        # Delete daily runs
        cursor.execute(
            "DELETE FROM daily_runs WHERE run_date = ?",
            (run_date,)
        )
        runs_deleted = cursor.rowcount
        
        conn.commit()
        print(f"Successfully reset database records for {run_date}:")
        print(f"  - Deleted {items_deleted} signal items")
        print(f"  - Deleted {bundles_deleted} signal bundles")
        print(f"  - Deleted {runs_deleted} daily run statuses")
    except Exception as e:
        conn.rollback()
        print(f"Error resetting simulation status: {e}")
        sys.exit(1)
    finally:
        conn.close()


def cmd_simulation_execute_pending(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    manifests = common.load_active_manifests(settings)
    calendar = ExchangeCalendarsTradingCalendar()

    run_date = date.fromisoformat(args.execution_date) if args.execution_date else date.today()

    account_id = common.resolve_account_id(conn, args.account)

    # Reuse the orchestrator's bundle loader (global bundles + this account's own)
    loader = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=None,
        market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals
    )
    bundles = loader._find_bundles_for_execution(run_date, account_id)
    if not bundles:
        print(f"No pending signal bundle found targeting execution date {run_date}")
        conn.close()
        return

    strategy_budgets = {}
    for sid in {b.strategy.strategy_id for b in bundles}:
        try:
            strategy_budgets[sid] = common.strategy_registry.load_strategy_definition(settings, sid).order_budget_twd
        except (FileNotFoundError, ValueError):
            pass

    context = ExecutionContext(
        run_id=f"exec-only-{datetime.now().strftime('%H%M%S')}",
        run_type="DAILY_SIMULATION",
        as_of_date=bundles[0].signal_date,
        execution_date=run_date,
        account_id=account_id
    )
    engine = TradeExecutionEngine(
        db_conn=conn, market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifests=manifests, strategy_budgets=strategy_budgets,
        global_limits=common.build_global_limits(settings, account_id),
        pipeline_order=settings.trading.pipeline.entry_strategies,
        slippage_bps=settings.backtest.slippage_bps,
        unlimited_open_positions=common.is_unlimited_positions_account(settings, account_id),
        cash_fraction_per_order=common.get_dynamic_order_sizing_fraction(settings, account_id)
    )

    print(f"Executing {len(bundles)} pending bundle(s) on {run_date}: {[b.bundle_id for b in bundles]}")
    res = engine.execute_bundles(context, bundles)
    print(f"Execution result status: {res['status']}")
    conn.close()


