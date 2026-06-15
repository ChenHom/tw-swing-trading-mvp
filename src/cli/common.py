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


STOCK_NAMES = {
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2308": "台達電",
    "2382": "廣達",
    "2881": "富邦金",
    "2882": "國泰金",
    "2301": "光寶科",
    "2324": "仁寶",
    "3231": "緯創",
    "2357": "華碩",
    "2891": "中信金",
    "2886": "兆豐金",
    "2603": "長榮",
    "2609": "陽明",
    "00400A": "主動國泰動能高息",
    "00981A": "主動統一台股增長",
    "00994A": "主動第一金台股優",
    "2327": "國巨",
    "2360": "致茂",
    "3090": "日電貿",
    "3691": "碩禾",
    "6805": "富世達",
    "TSE": "加權指數"
}


def get_settings() -> AppSettings:
    return AppSettings()


def resolve_account_id(conn, specified_account: str | None) -> str:
    if specified_account:
        return specified_account
    if not sys.stdin.isatty() and "pytest" not in sys.modules:
        print("錯誤：在非互動式環境中（如排程、cron 或 CI），必須明確使用 --account 參數指定目標帳戶，禁止隱式自動解析。")
        sys.exit(1)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cash_balances'")
        if not cursor.fetchone():
            return "simulation-main"
            
        cursor.execute("SELECT DISTINCT account_id FROM cash_balances")
        balances_accs = [row["account_id"] for row in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT account_id FROM cash_ledger")
        ledger_accs = [row["account_id"] for row in cursor.fetchall()]
        
        all_accs = list(set(balances_accs + ledger_accs))
    except Exception:
        all_accs = []
        
    if len(all_accs) == 1:
        return all_accs[0]
    elif len(all_accs) > 1:
        print(f"錯誤：偵測到資料庫中有多個帳戶 {all_accs}，請使用 --account 參數指定目標帳戶。")
        sys.exit(1)
    else:
        return "simulation-main"


def sign_manifest(manifest_dict: dict) -> dict:
    manifest_dict["integrity"]["digest"] = ""
    manifest_obj = StrategyApprovalManifest(**manifest_dict)
    
    dump_dict = json.loads(manifest_obj.model_dump_json())
    dump_dict["integrity"]["digest"] = ""
    
    canonical_str = json.dumps(
        dump_dict,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":")
    )
    calculated_digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    dump_dict["integrity"]["digest"] = f"sha256:{calculated_digest}"
    return dump_dict


def build_global_limits(settings: AppSettings) -> GlobalLimits:
    g = settings.trading.global_limits
    return GlobalLimits(
        max_open_positions=g.max_open_positions,
        max_daily_buy_value=g.max_daily_buy_value,
        max_new_positions_per_day=g.max_new_positions_per_day
    )


def build_pipeline(settings: AppSettings, universe_symbols: list[str]):
    """Load entry strategy specs (in configured order) and exit-managed definitions."""
    index_symbol = settings.trading.pipeline.index_symbol
    entry_specs = []
    for sid in settings.trading.pipeline.entry_strategies:
        defn = strategy_registry.load_strategy_definition(settings, sid)
        entry_specs.append(EntryStrategySpec(
            definition=defn,
            strategy=strategy_registry.build_entry_strategy(defn, universe_symbols, index_symbol)
        ))
    exit_definitions = strategy_registry.load_exit_managed_definitions(settings)
    return entry_specs, exit_definitions


def get_valuation_universe(conn, strategy_symbols: list[str]) -> list[str]:
    cursor = conn.cursor()
    # 1. Open positions
    cursor.execute("SELECT DISTINCT symbol FROM position_lots WHERE quantity > 0")
    open_symbols = [row["symbol"] for row in cursor.fetchall()]
    
    # 2. Strategy signals (buy/sell targets in fills)
    cursor.execute("SELECT DISTINCT symbol FROM fills")
    fill_symbols = [row["symbol"] for row in cursor.fetchall()]
    
    # Union them
    val_set = set(strategy_symbols).union(open_symbols).union(fill_symbols)
    return sorted(list(val_set))


def _resolve_strategy_id_from_path(settings, path_or_id: str) -> str:
    """Accept either a strategy_id or a YAML path/filename; return the strategy_id."""
    candidate = Path(path_or_id)
    if candidate.suffix in (".yaml", ".yml"):
        return candidate.stem
    return path_or_id


def _make_preflight_runner(settings) -> DailySimulationRunner:
    calendar = ExchangeCalendarsTradingCalendar()
    return DailySimulationRunner(
        db_conn=None, calendar=calendar, market_provider=None,
        market_repo=None, projection=None,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        expiry_warning_sessions=settings.trading.approval.expiry_warning_sessions
    )


def uuid_like() -> str:
    import uuid
    return uuid.uuid4().hex[:8]

