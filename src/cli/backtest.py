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
from src.application.reporting.backtest_report import write_backtest_result
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
    # --db：研究回測指向 data/research.db（與 live app.db 隔離）；預設仍用 settings 的 app.db。
    db_path = getattr(args, "db", None) or settings.trading.database_path
    init_db(db_path)
    conn = get_db_connection(db_path)

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

    # PIT 流動性 universe：--universe-policy 指定 policy_version 時，策略改吃 per-date 成分股；
    # runner 的 universe_symbols（缺檔/benchmark/fingerprint 用）取該 policy 成分股的聯集。
    policy_version = getattr(args, "universe_policy", None)
    if policy_version:
        from src.strategy.universe import PolicyUniverseProvider
        universe_arg = PolicyUniverseProvider(conn, policy_version)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM universe_policy WHERE policy_version = ? ORDER BY symbol",
            (policy_version,),
        ).fetchall()
        symbols = [r["symbol"] for r in rows]
        if not symbols:
            print(f"Error: universe_policy '{policy_version}' 無成分股，請先執行 'market build-universe'。")
            sys.exit(1)
        print(f"PIT universe: policy='{policy_version}', {len(symbols)} 檔成分股聯集")
    else:
        universe_arg = symbols

    entry_spec = EntryStrategySpec(
        definition=defn,
        strategy=common.strategy_registry.build_entry_strategy(defn, universe_arg, index_symbol)
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

    print(f"Running backtest [{strategy_id}] from {start_date} to {end_date} (db={db_path})...")
    result = runner.run(
        start_date=start_date,
        end_date=end_date,
        initial_cash=args.initial_cash,
        universe_symbols=symbols,
        entry_spec=entry_spec,
        universe_policy_version=policy_version,
    )
    
    stats = result["statistics"]
    print("\n--- Backtest Statistics ---")
    print(f"Final Equity: {stats['final_equity']:,} TWD")
    print(f"Total PnL: {stats['total_pnl']:+,} TWD ({stats['total_pnl_bps']/100:+.2f}%)")
    print(f"Max Drawdown: {stats['max_drawdown']*100:.2f}%")
    print(f"Trades count: {stats['trade_count']}")
    print(f"Win Rate: {stats['win_rate']*100:.2f}%")
    print(f"Profit Factor: {stats['profit_factor']:.2f}")

    verdict = result["verdict"]
    print(f"Verdict: {verdict['verdict']}" + (f" ({verdict['diagnostic_result']})" if verdict["diagnostic_result"] else ""))

    result_path = write_backtest_result(
        result,
        strategy_id=strategy_id,
        start_date=start_date,
        end_date=end_date,
        initial_cash=args.initial_cash,
    )
    print(f"BACKTEST_RESULT_PATH={result_path}")
    conn.close()


