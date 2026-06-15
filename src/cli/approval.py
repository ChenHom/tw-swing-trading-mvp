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


def cmd_approval_create(args):
    settings = common.get_settings()
    strategy_id_arg = common._resolve_strategy_id_from_path(settings, args.strategy)
    try:
        defn = common.strategy_registry.load_strategy_definition(settings, strategy_id_arg)
    except (FileNotFoundError, ValueError) as e:
        print(f"載入策略設定失敗: {e}")
        sys.exit(1)

    strategy_id = defn.strategy_id
    strategy_version = defn.strategy_version
    params_hash = defn.params_hash

    valid_from = args.valid_from if args.valid_from else datetime.now(timezone.utc).astimezone().isoformat()
    
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": f"approval-{strategy_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "issuer_id": args.issuer,
        "strategy": {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "params_canonicalization": "strategy-params-v1",
            "params_hash": params_hash
        },
        "permissions": {
            "execution_modes": ["backtest", "simulation"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": args.max_order_value,
            "max_daily_buy_value": args.max_daily_buy_value,
            "max_open_positions": args.max_open_positions
        },
        "validity": {
            "valid_from": valid_from,
            "expires_at": args.expires_at
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": ""
        }
    }
    
    signed = common.sign_manifest(manifest_dict)
    
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(signed, f, indent=2, ensure_ascii=False)
        
    print(f"Manifest created and signed at {output_path}")


def cmd_approval_validate(args):
    settings = common.get_settings()
    filepath = Path(args.manifest_path)
    if not filepath.exists():
        print(f"Manifest file not found: {args.manifest_path}")
        sys.exit(1)
        
    with open(filepath, "r", encoding="utf-8") as f:
        manifest_dict = json.load(f)
        
    manifest = StrategyApprovalManifest(**manifest_dict)
    validator = ManifestValidator(settings.issuer_allowlist, settings.revoked_approvals)
    
    # Preflight validation in simulation mode
    now_time = datetime.now(timezone.utc).astimezone()
    try:
        validator.validate(manifest, now_time, "simulation")
        print("Validation successful: Manifest is valid.")
    except ValueError as e:
        print(f"Validation failed: {e}")
        sys.exit(1)


def cmd_approval_activate(args):
    settings = common.get_settings()
    filepath = Path(args.manifest_path)
    if not filepath.exists():
        print(f"Manifest file not found: {args.manifest_path}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        manifest_dict = json.load(f)
    manifest = StrategyApprovalManifest(**manifest_dict)

    activate_manifest(settings, manifest, manifest_dict)
    print(f"Manifest {manifest.approval_id} activated for strategy '{manifest.strategy.strategy_id}'.")


def cmd_approval_deactivate(args):
    settings = common.get_settings()
    if deactivate_strategy(settings, args.strategy):
        print(f"策略 '{args.strategy}' 的有效授權已停用。該策略的 BUY 訊號將被系統阻擋（SELL 不受影響）。")
    else:
        print(f"策略 '{args.strategy}' 目前沒有有效授權，無需停用。")


def cmd_approval_list(args):
    settings = common.get_settings()
    manifests = common.load_active_manifests(settings)
    if not manifests:
        print("目前沒有任何策略擁有有效授權。")
        return
    runner = common._make_preflight_runner(settings)
    print(f"{'策略':<20} {'授權識別碼':<45} {'到期日':<28} 狀態")
    print("-" * 110)
    for strategy_id in sorted(manifests):
        m = manifests[strategy_id]
        preflight = runner.get_preflight_status(date.today(), m)
        print(f"{strategy_id:<20} {m.approval_id:<45} {m.validity.expires_at:<28} {preflight}")


def cmd_approval_status(args):
    settings = common.get_settings()
    manifests = common.load_active_manifests(settings)
    if not manifests:
        print("Status: MISSING (No active manifest found for any strategy)")
        return

    runner = common._make_preflight_runner(settings)
    for strategy_id in sorted(manifests):
        manifest = manifests[strategy_id]
        preflight = runner.get_preflight_status(date.today(), manifest)
        print(f"[{strategy_id}]")
        print(f"  approval_id: {manifest.approval_id}")
        print(f"  valid_from: {manifest.validity.valid_from}")
        print(f"  expires_at: {manifest.validity.expires_at}")
        print(f"  Preflight status for today: {preflight}")


