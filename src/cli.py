import argparse
import sys
import os
import json
import hashlib
import sqlite3
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.config import AppSettings
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.portfolio.ledger import PortfolioLedger
from src.contracts.models import (
    TrendPullbackParams, StrategyApprovalManifest, StrategyInfo, LimitsInfo,
    ValidityInfo, IntegrityInfo, PermissionsInfo, DailySignalBundle, SignalItem, ExecutionContext
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.market_data.provider import ShioajiMarketDataProvider
from src.application.runners.backtest import BacktestRunner
from src.application.runners.simulation import DailySimulationRunner
from src.approval.validator import ManifestValidator
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.strategy.trend_pullback import TrendPullbackStrategy
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.trading.planner import OrderPlanner, PortfolioState
from src.broker.fake_broker import FakeBroker
from src.application.execution.engine import TradeExecutionEngine

def get_settings() -> AppSettings:
    return AppSettings()

def resolve_account_id(conn, specified_account: str | None) -> str:
    if specified_account:
        return specified_account
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
    "6805": "富世達"
}

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

def load_active_manifest(settings: AppSettings) -> Optional[StrategyApprovalManifest]:
    active_pointer_path = Path(settings.trading.approval.active_pointer_path)
    if not active_pointer_path.exists():
        return None
    with open(active_pointer_path, "r", encoding="utf-8") as f:
        pointer = json.load(f)
    
    approval_id = pointer.get("approval_id")
    if not approval_id:
        return None
        
    manifest_path = Path(settings.trading.approval.approvals_dir) / f"{approval_id}.json"
    if not manifest_path.exists():
        # Fallback to checking active pointer path parent folder or custom name
        manifest_path = Path("artifacts/approvals") / f"{approval_id}.json"
    if not manifest_path.exists():
        return None
        
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_dict = json.load(f)
    return StrategyApprovalManifest(**manifest_dict)

# Subcommand Handlers
def cmd_market_backfill(args):
    settings = get_settings()
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
    
    symbols = [s.code for s in settings.universe.symbols]
    
    success_count = 0
    for s_date in sessions:
        print(f"Syncing market data for {s_date}...")
        if runner.sync_market_data(s_date, symbols):
            success_count += 1
            
    print(f"Successfully sync'd {success_count} / {len(sessions)} days.")
    conn.close()

def cmd_market_sync(args):
    settings = get_settings()
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
    symbols = [s.code for s in settings.universe.symbols]
    
    print(f"Syncing market data for {sync_date}...")
    if runner.sync_market_data(sync_date, symbols):
        print(f"Market data for {sync_date} sync'd successfully.")
    else:
        print(f"Failed to sync market data for {sync_date}.")
        sys.exit(1)
    conn.close()

def cmd_market_validate(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    
    today_dt = date.today()
    sessions = calendar.sessions_between(today_dt - timedelta(days=args.last_sessions * 2), today_dt)
    sessions = sessions[-args.last_sessions:]
    
    symbols = [s.code for s in settings.universe.symbols]
    
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

def cmd_strategy_inspect(args):
    settings = get_settings()
    # If file name, check in strategies directory
    filepath = Path(args.config_path)
    if not filepath.exists():
        filepath = Path(settings.config_dir) / "strategies" / args.config_path
    if not filepath.exists():
        print(f"Strategy config file not found: {args.config_path}")
        sys.exit(1)
        
    import yaml
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    strategy_id = data["strategy_id"]
    strategy_version = data["strategy_version"]
    params = TrendPullbackParams(**data["parameters"])
    params_hash = StrategyParameterCanonicalizer.compute_hash(params)
    
    print(f"strategy_id: {strategy_id}")
    print(f"strategy_version: {strategy_version}")
    print("canonicalization: strategy-params-v1")
    print(f"params_hash: {params_hash}")

def cmd_approval_create(args):
    settings = get_settings()
    filepath = Path(args.strategy)
    if not filepath.exists():
        filepath = Path(settings.config_dir) / "strategies" / args.strategy
    if not filepath.exists():
        print(f"Strategy config file not found: {args.strategy}")
        sys.exit(1)
        
    import yaml
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        
    strategy_id = data["strategy_id"]
    strategy_version = data["strategy_version"]
    params = TrendPullbackParams(**data["parameters"])
    params_hash = StrategyParameterCanonicalizer.compute_hash(params)
    
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
    
    signed = sign_manifest(manifest_dict)
    
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(signed, f, indent=2, ensure_ascii=False)
        
    print(f"Manifest created and signed at {output_path}")

def cmd_approval_validate(args):
    settings = get_settings()
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
    settings = get_settings()
    filepath = Path(args.manifest_path)
    if not filepath.exists():
        print(f"Manifest file not found: {args.manifest_path}")
        sys.exit(1)
        
    with open(filepath, "r", encoding="utf-8") as f:
        manifest_dict = json.load(f)
    manifest = StrategyApprovalManifest(**manifest_dict)
    
    # Save manifest into approvals dir
    approvals_dir = Path(settings.trading.approval.approvals_dir)
    approvals_dir.mkdir(parents=True, exist_ok=True)
    stored_manifest_path = approvals_dir / f"{manifest.approval_id}.json"
    with open(stored_manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_dict, f, indent=2, ensure_ascii=False)
        
    # Atomic rename active pointer file
    pointer_path = Path(settings.trading.approval.active_pointer_path)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    
    temp_pointer_path = pointer_path.with_suffix(".tmp")
    with open(temp_pointer_path, "w", encoding="utf-8") as f:
        json.dump({
            "approval_id": manifest.approval_id,
            "activated_at": datetime.now(timezone.utc).astimezone().isoformat()
        }, f, indent=2)
        
    os.replace(temp_pointer_path, pointer_path)
    print(f"Manifest {manifest.approval_id} activated.")

def cmd_approval_status(args):
    settings = get_settings()
    manifest = load_active_manifest(settings)
    if not manifest:
        print("Status: MISSING (No active manifest pointer found)")
        return
        
    calendar = ExchangeCalendarsTradingCalendar()
    mock_provider = None
    conn = None
    # Just temporary objects to init runner
    runner = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=mock_provider,
        market_repo=None, projection=None,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifest=manifest, expiry_warning_sessions=settings.trading.approval.expiry_warning_sessions
    )
    
    preflight = runner.get_preflight_status(date.today())
    print(f"approval_id: {manifest.approval_id}")
    print(f"valid_from: {manifest.validity.valid_from}")
    print(f"expires_at: {manifest.validity.expires_at}")
    print(f"Preflight status for today: {preflight}")

def cmd_account_init(args):
    settings = get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    
    account_id = resolve_account_id(conn, args.account)
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    
    run_id = f"init-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    ledger.deposit(account_id, run_id, args.initial_cash, "TWD", date.today())
    projection.rebuild_from_ledger(account_id)
    
    print(f"Account '{account_id}' initialized with {args.initial_cash} TWD.")
    conn.close()

def cmd_account_adjust_cash(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    cursor = conn.cursor()
    
    account_id = resolve_account_id(conn, args.account)
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

def cmd_backtest_run(args):
    settings = get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    
    manifest = load_active_manifest(settings)
    if not manifest:
        print("Error: No active manifest activated. Run 'approval activate' first.")
        sys.exit(1)
        
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    
    runner = BacktestRunner(
        db_conn=conn, calendar=calendar, market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifest=manifest, strategy_budget=manifest.limits.max_order_value,
        slippage_bps=settings.backtest.slippage_bps
    )
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.to)
    symbols = [s.code for s in settings.universe.symbols]
    
    strategy_config = settings.load_strategy_config("trend_pullback")
    params = TrendPullbackParams(**strategy_config.parameters.model_dump())
    
    print(f"Running backtest from {start_date} to {end_date}...")
    result = runner.run(
        start_date=start_date,
        end_date=end_date,
        initial_cash=args.initial_cash,
        universe_symbols=symbols,
        strategy_params=params
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

def cmd_simulation_run_daily(args):
    settings = get_settings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    
    manifest = load_active_manifest(settings)
    if not manifest:
        print("Warning: No active manifest found. Simulation will run using a dummy manifest.")
        
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    provider = ShioajiMarketDataProvider(settings.shioaji_api_key, settings.shioaji_secret_key)
    
    runner = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=provider,
        market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifest=manifest, slippage_bps=settings.backtest.slippage_bps,
        expiry_warning_sessions=settings.trading.approval.expiry_warning_sessions
    )
    
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    symbols = [s.code for s in settings.universe.symbols]
    
    strategy_config = settings.load_strategy_config("trend_pullback")
    params = TrendPullbackParams(**strategy_config.parameters.model_dump())
    
    account_id = resolve_account_id(conn, args.account)
    
    print(f"Running daily simulation workflow for {run_date}...")
    status = runner.run_daily(
        run_date=run_date,
        account_id=account_id,
        strategy_id="trend_pullback",
        strategy_params=params,
        universe_symbols=symbols
    )
    
    print(f"Simulation runner finished with status: {status}")
    if status == "FAILED":
        sys.exit(1)
    conn.close()

def cmd_simulation_reset(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    cursor = conn.cursor()
    
    run_date = args.date
    print(f"Resetting daily simulation status and generated signal data for date {run_date}...")
    
    try:
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
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    manifest = load_active_manifest(settings)
    
    run_date = date.fromisoformat(args.execution_date) if args.execution_date else date.today()
    
    # Query pending signal bundle
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
        FROM signal_bundles WHERE target_execution_date = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (run_date.isoformat(),)
    )
    row = cursor.fetchone()
    if not row:
        print(f"No pending signal bundle found targeting execution date {run_date}")
        conn.close()
        return
        
    bundle_id = row["bundle_id"]
    cursor.execute(
        "SELECT signal_id, symbol, action, reference_price, reason_code FROM signal_items WHERE bundle_id = ?",
        (bundle_id,)
    )
    items = []
    for item_row in cursor.fetchall():
        items.append(
            SignalItem(
                signal_id=item_row["signal_id"],
                symbol=item_row["symbol"],
                action=item_row["action"],
                reference_price=float(item_row["reference_price"] / 10000.0),
                reason_code=item_row["reason_code"]
            )
        )
        
    strategy_info = StrategyInfo(
        strategy_id=row["strategy_id"],
        strategy_version=row["strategy_version"],
        params_canonicalization="strategy-params-v1",
        params_hash=row["params_hash"]
    )
    bundle = DailySignalBundle(
        schema_version="1.0",
        bundle_id=bundle_id,
        run_id=row["run_id"],
        approval_id=row["approval_id"],
        strategy=strategy_info,
        signal_date=date.fromisoformat(row["signal_date"]),
        target_execution_date=date.fromisoformat(row["target_execution_date"]),
        market_data_cutoff=date.fromisoformat(row["market_data_cutoff"]),
        signals=items
    )
    
    account_id = resolve_account_id(conn, args.account)
    
    context = ExecutionContext(
        run_id=f"exec-only-{datetime.now().strftime('%H%M%S')}",
        run_type="DAILY_SIMULATION",
        as_of_date=bundle.signal_date,
        execution_date=run_date,
        account_id=account_id
    )
    engine = TradeExecutionEngine(
        db_conn=conn, market_repo=repo, projection=projection,
        allowed_issuers=settings.issuer_allowlist, revoked_approvals=settings.revoked_approvals,
        manifest=manifest, strategy_budget=manifest.limits.max_order_value if manifest else 30000,
        slippage_bps=settings.backtest.slippage_bps
    )
    
    print(f"Executing pending bundle {bundle_id} on {run_date}...")
    res = engine.execute_bundle(context, bundle)
    print(f"Execution result status: {res['status']}")
    conn.close()

def cmd_signal_generate(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    manifest = load_active_manifest(settings)
    
    as_of_date = date.fromisoformat(args.as_of_date) if args.as_of_date else date.today()
    symbols = [s.code for s in settings.universe.symbols]
    
    strategy_config = settings.load_strategy_config("trend_pullback")
    params = TrendPullbackParams(**strategy_config.parameters.model_dump())
    
    strategy = TrendPullbackStrategy(params=params, universe_symbols=symbols)
    pit_data = repo.as_of(as_of_date)
    
    # Verify complete data
    for symbol in symbols:
        if not repo.find(symbol, as_of_date):
            print(f"Error: Missing market bar for {symbol} on {as_of_date}. Run 'market sync' first.")
            sys.exit(1)
            
    # Gather portfolio snapshot
    account_id = resolve_account_id(conn, args.account)
    available_cash = projection.get_cash_balance(account_id)
    positions = {}
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty, AVG(price) as avg_price FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (account_id,)
    )
    for row in cursor.fetchall():
        qty = row["qty"]
        if qty > 0:
            positions[row["symbol"]] = PositionSnapshot(
                symbol=row["symbol"],
                quantity=qty,
                entry_price=int(row["avg_price"])
            )
    portfolio_snapshot = PortfolioSnapshot(available_cash=available_cash, positions=positions)
    
    sig_ctx = SignalGenerationContext(
        as_of_date=as_of_date,
        strategy_id="trend_pullback",
        strategy_version=manifest.strategy.strategy_version if manifest else "1.0.0",
        run_id=f"manual-sig-{as_of_date.strftime('%Y%m%d')}",
        approval_id=manifest.approval_id if manifest else "app-dummy",
        params_hash=manifest.strategy.params_hash if manifest else "hash-dummy"
    )
    
    print(f"Generating signals as of {as_of_date} close...")
    new_bundle = strategy.generate(sig_ctx, pit_data, portfolio_snapshot)
    
    target_execution_date = calendar.next_trading_day(as_of_date)
    
    # Save bundle
    # Save via helper (reimplemented locally to avoid DailySimulationRunner requirement)
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            new_bundle.bundle_id, new_bundle.run_id, new_bundle.approval_id,
            new_bundle.strategy.strategy_id, new_bundle.strategy.strategy_version,
            new_bundle.strategy.params_hash, new_bundle.signal_date.isoformat(),
            target_execution_date.isoformat(), new_bundle.market_data_cutoff.isoformat()
        )
    )
    for sig in new_bundle.signals:
        cursor.execute(
            """
            INSERT INTO signal_items (
                item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                f"item-{hashlib.sha256(sig.signal_id.encode()).hexdigest()[:8]}",
                new_bundle.bundle_id, sig.signal_id, sig.symbol, sig.action,
                int(round(sig.reference_price * 10000)), sig.reason_code
            )
        )
    conn.commit()
    print(f"Signals generated successfully (Bundle ID: {new_bundle.bundle_id}, Target Execution Date: {target_execution_date})")
    conn.close()

def cmd_signal_list(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    
    query = """
        SELECT 
            b.signal_date, 
            b.target_execution_date, 
            i.symbol, 
            i.action, 
            i.reference_price, 
            i.reason_code,
            b.strategy_id
        FROM signal_items i
        JOIN signal_bundles b ON i.bundle_id = b.bundle_id
    """
    params = []
    if args.date:
        query += " WHERE b.signal_date = ?"
        params.append(args.date)
        
    query += " ORDER BY b.signal_date DESC, i.symbol ASC"
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    headers = ["訊號日期", "執行日期", "代號", "名稱", "動作", "參考價格", "原因", "策略"]
    table_rows = []
    for r in rows:
        ref_price = r["reference_price"] / 10000.0
        symbol = r["symbol"]
        name = STOCK_NAMES.get(symbol, "未知")
        table_rows.append([
            r["signal_date"],
            r["target_execution_date"],
            symbol,
            name,
            r["action"],
            f"{ref_price:.2f}",
            r["reason_code"],
            r["strategy_id"]
        ])
        
    if not table_rows:
        if args.date:
            print(f"查無日期 {args.date} 的訊號紀錄。")
        else:
            print("資料庫中無任何訊號紀錄。")
        conn.close()
        return
        
    # 計算顯示長度的輔助函式（中文字元計為 2 個寬度）
    def display_len(s: str) -> int:
        length = 0
        for char in s:
            if ord(char) > 127:
                length += 2
            else:
                length += 1
        return length

    # 填充顯示寬度的輔助函式（支援中文字元對齊）
    def pad_str(s: str, width: int) -> str:
        cur_len = display_len(s)
        if cur_len >= width:
            return s
        return s + " " * (width - cur_len)
        
    # 計算各欄顯示的最大寬度
    widths = [display_len(h) for h in headers]
    for row in table_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], display_len(str(val)))
            
    header_str = " | ".join(pad_str(h, widths[i]) for i, h in enumerate(headers))
    print(header_str)
    print("-+-".join("-" * w for w in widths))
    for row in table_rows:
        row_str = " | ".join(pad_str(str(val), widths[i]) for i, val in enumerate(row))
        print(row_str)
    print(f"\n共計: {len(table_rows)} 筆訊號。")
    conn.close()


def cmd_trade_plan(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    manifest = load_active_manifest(settings)
    
    # Read bundle
    filepath = Path(args.bundle)
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            bundle_dict = json.load(f)
        bundle = DailySignalBundle(**bundle_dict)
    else:
        # Try to load bundle from database by bundle_id or signal_date
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
            FROM signal_bundles WHERE bundle_id = ? OR signal_date = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (args.bundle, args.bundle)
        )
        row = cursor.fetchone()
        if not row:
            print(f"找不到檔案且資料庫中也無此 Bundle ID 或日期：{args.bundle}")
            conn.close()
            sys.exit(1)
            
        bundle_id = row["bundle_id"]
        cursor.execute(
            "SELECT signal_id, symbol, action, reference_price, reason_code FROM signal_items WHERE bundle_id = ?",
            (bundle_id,)
        )
        items = []
        for item_row in cursor.fetchall():
            items.append(
                SignalItem(
                    signal_id=item_row["signal_id"],
                    symbol=item_row["symbol"],
                    action=item_row["action"],
                    reference_price=float(item_row["reference_price"] / 10000.0),
                    reason_code=item_row["reason_code"]
                )
            )
            
        strategy_info = StrategyInfo(
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            params_canonicalization="strategy-params-v1",
            params_hash=row["params_hash"]
        )
        bundle = DailySignalBundle(
            schema_version="1.0",
            bundle_id=bundle_id,
            run_id=row["run_id"],
            approval_id=row["approval_id"],
            strategy=strategy_info,
            signal_date=date.fromisoformat(row["signal_date"]),
            target_execution_date=date.fromisoformat(row["target_execution_date"]),
            market_data_cutoff=date.fromisoformat(row["market_data_cutoff"]),
            signals=items
        )
        
    # We need to construct PortfolioState
    # Let's get current portfolio state for account
    account_id = resolve_account_id(conn, args.account)
    available_cash = projection.get_cash_balance(account_id)
    
    # Get positions
    positions = {}
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (account_id,)
    )
    for row in cursor.fetchall():
        qty = row["qty"]
        if qty > 0:
            positions[row["symbol"]] = qty
            
    # Daily buy value spent
    cursor.execute(
        "SELECT SUM(quantity * price) as val FROM fills WHERE account_id = ? AND side = 'BUY' AND date(filled_at) = ?",
        (account_id, bundle.target_execution_date.isoformat())
    )
    val_row = cursor.fetchone()
    daily_buy_value_spent = int(val_row["val"] / 10000.0) if val_row["val"] else 0
    
    portfolio_state = PortfolioState(
        available_cash=available_cash,
        positions=positions,
        daily_buy_value_spent=daily_buy_value_spent
    )
    
    if not manifest:
        print("警告：未載入任何啟用授權清單。將使用預設限制。")
        limits = LimitsInfo(
            currency="TWD",
            max_order_value=35000,
            max_daily_buy_value=150000,
            max_open_positions=5
        )
    else:
        limits = manifest.limits
        
    print(f"帳戶：{account_id} | 可用現金：{available_cash:,} 元 | 當日已買入金額：{daily_buy_value_spent:,} 元")
    print(f"委託計畫預覽 (訊號包: {bundle.bundle_id})：\n")
    
    headers = ["代號", "名稱", "動作", "參考價格", "規劃數量", "單位", "預估金額", "規劃狀態"]
    table_rows = []
    
    for sig in bundle.signals:
        symbol = sig.symbol
        name = STOCK_NAMES.get(symbol, "未知")
        action = "買入" if sig.action == "BUY" else "賣出"
        
        try:
            planned_orders = OrderPlanner.plan_order(
                signal=sig,
                portfolio=portfolio_state,
                strategy_budget=manifest.limits.max_order_value if manifest else 35000,
                manifest_limits=limits
            )
            
            if not planned_orders:
                table_rows.append([
                    symbol, name, action, f"{sig.reference_price:.2f}",
                    "0", "股", "0", "無交易 (無持倉)"
                ])
                continue
                
            for order in planned_orders:
                qty = order["quantity"]
                unit = "張" if (qty >= 1000 and qty % 1000 == 0) else "股"
                display_qty = f"{qty // 1000}" if unit == "張" else f"{qty}"
                est_val = int(qty * sig.reference_price)
                
                table_rows.append([
                    symbol, name, action, f"{sig.reference_price:.2f}",
                    display_qty, unit, f"{est_val:,}", "成功 (待執行)"
                ])
        except ValueError as e:
            table_rows.append([
                symbol, name, action, f"{sig.reference_price:.2f}",
                "阻擋", "-", "-", f"風控阻擋: {e}"
            ])
            
    # Print table helper
    def display_len(s: str) -> int:
        length = 0
        for char in s:
            if ord(char) > 127:
                length += 2
            else:
                length += 1
        return length

    def pad_str(s: str, width: int) -> str:
        cur_len = display_len(s)
        if cur_len >= width:
            return s
        return s + " " * (width - cur_len)
        
    widths = [display_len(h) for h in headers]
    for row in table_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], display_len(str(val)))
            
    header_str = " | ".join(pad_str(h, widths[i]) for i, h in enumerate(headers))
    print(header_str)
    print("-+-".join("-" * w for w in widths))
    for row in table_rows:
        row_str = " | ".join(pad_str(str(val), widths[i]) for i, val in enumerate(row))
        print(row_str)
    conn.close()

def cmd_trade_record_fill(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    symbol = args.symbol
    side = args.side.upper()
    qty = args.quantity
    price = args.price
    account_id = resolve_account_id(conn, args.account)
    
    price_scaled = int(round(price * 10000))
    
    fill_payload = {
        "fill_id": f"fill-manual-{uuid_like()}",
        "account_id": account_id,
        "run_id": f"manual-{date.today().strftime('%Y%m%d')}",
        "order_id": f"ord-manual-{uuid_like()}",
        "execution_key": f"manual-fill-{symbol}-{uuid_like()}",
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "price": price_scaled,
        "filled_at": datetime.now().isoformat()
    }
    
    try:
        projection.apply_fill_transaction(fill_payload)
        
        # Auto-add to universe.yaml if not present
        existing_codes = [s.code for s in settings.universe.symbols]
        if symbol not in existing_codes:
            try:
                import yaml
                universe_path = settings.config_dir / "universe.yaml"
                if universe_path.exists():
                    with open(universe_path, "r", encoding="utf-8") as f:
                        content = yaml.safe_load(f) or {}
                    if "symbols" not in content:
                        content["symbols"] = []
                    # Check again to avoid duplicate in file
                    if not any(s.get("code") == symbol for s in content.get("symbols", [])):
                        content["symbols"].append({
                            "code": symbol,
                            "exchange": "TSE",
                            "instrument_type": "STOCK"
                        })
                        with open(universe_path, "w", encoding="utf-8") as f:
                            yaml.dump(content, f, sort_keys=False, allow_unicode=True)
                        print(f"  - 提示：已自動將新標的 {symbol} 新增至交易宇宙設定檔 (universe.yaml)。")
            except Exception as e:
                print(f"  - 警告：無法自動將新標的 {symbol} 新增至 universe.yaml: {e}")
        
        trade_value = int(round(qty * price_scaled / 10000.0))
        broker_fee = max(20, int(round(trade_value * 0.001425)))
        tax = int(round(trade_value * 0.003)) if side == "SELL" else 0
        
        print("成功錄入成交資料：")
        print(f"  - 帳戶：{account_id}")
        print(f"  - 標的：{symbol}")
        print(f"  - 動作：{side}")
        print(f"  - 數量：{qty} 股")
        print(f"  - 成交單價：{price:.2f} 元 (資料庫整數值: {price_scaled})")
        print(f"  - 成交總額：{trade_value:,} TWD (單價 x 數量)")
        print(f"  - 估計手續費：{broker_fee:,} TWD")
        if tax > 0:
            print(f"  - 估計交易稅：{tax:,} TWD")
            total_net = trade_value - broker_fee - tax
            print(f"  - 估計淨收金額：{total_net:,} TWD")
        else:
            total_cost = trade_value + broker_fee
            print(f"  - 估計總付出成本：{total_cost:,} TWD")
    except Exception as e:
        print(f"錄入成交資料失敗: {e}")
        sys.exit(1)
    finally:
        conn.close()
        
def cmd_portfolio_reconcile(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    account_id = resolve_account_id(conn, args.account)
    print(f"Reconciling account {account_id}...")
    errors = projection.reconcile(account_id)
    if not errors:
        print("Reconciliation successful: cash ledger balances match projections.")
    else:
        print("Reconciliation failed! Errors found:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    conn.close()

def cmd_portfolio_rebuild_projections(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    account_id = resolve_account_id(conn, args.account)
    print(f"Rebuilding projections for account {account_id}...")
    projection.rebuild_from_ledger(account_id)
    print("Projections successfully rebuilt from ledger facts.")
    conn.close()

def cmd_report_pnl(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    account_id = resolve_account_id(conn, args.account)
    report_date = date.fromisoformat(args.date) if args.date else date.today()
    cash = projection.get_cash_balance(account_id)
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty, AVG(price) as avg_price FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (account_id,)
    )
    positions = []
    total_pos_value = 0
    repo = SqliteMarketBarRepository(conn)
    
    for row in cursor.fetchall():
        qty = row["qty"]
        if qty > 0:
            # Try to find closing price on report_date
            bar = repo.find(row["symbol"], report_date)
            close_price = bar.close / 10000.0 if bar else row["avg_price"] / 10000.0
            pos_val = int(qty * close_price)
            total_pos_value += pos_val
            positions.append({
                "symbol": row["symbol"],
                "quantity": qty,
                "entry_price": row["avg_price"] / 10000.0,
                "current_price": close_price,
                "value": pos_val
            })
            
    print(f"\n--- 帳戶 {account_id} 於 {report_date} 的損益報告 ---")
    print(f"可用現金：{cash:,} TWD")
    print(f"部位價值：{total_pos_value:,} TWD")
    print(f"總資產淨值：{cash + total_pos_value:,} TWD")
    print("\n持有部位：")
    if not positions:
        print("  無持有部位。")
    for pos in positions:
        name = STOCK_NAMES.get(pos['symbol'], "未知")
        print(f"  {pos['symbol']} {name}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD")
        
    conn.close()

def cmd_trade_close_all(args):
    # Manual emergency exit
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty FROM position_lots GROUP BY symbol"
    )
    open_positions = {row["symbol"]: row["qty"] for row in cursor.fetchall() if row["qty"] > 0}
    
    if not open_positions:
        print("No open positions to close.")
        conn.close()
        return
        
    print(f"EMERGENCY CLOSE: Closing all {len(open_positions)} positions due to: '{args.reason}'")
    # For each open position, we write a sell fill at today's close or entry price
    # In fake broker mode we simulate immediate closure
    today_dt = date.today()
    repo = SqliteMarketBarRepository(conn)
    
    for symbol, qty in open_positions.items():
        bar = repo.find(symbol, today_dt)
        close_price = bar.close if bar else 1000000 # Default fallback
        
        # Apply fill transaction
        fill_payload = {
            "fill_id": f"fill-emergency-{uuid_like()}",
            "account_id": "simulation-main",
            "run_id": f"emergency-{today_dt.strftime('%Y%m%d')}",
            "order_id": f"ord-emergency-{uuid_like()}",
            "execution_key": f"emergency-close-{symbol}-{today_dt.isoformat()}",
            "symbol": symbol,
            "side": "SELL",
            "quantity": qty,
            "price": close_price,
            "filled_at": datetime.now().isoformat()
        }
        projection.apply_fill_transaction(fill_payload)
        print(f"  Closed {qty} shares of {symbol} at price {close_price/10000.0:.2f}")
        
    conn.close()

def uuid_like() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


# Main CLI Setup
def main():
    parser = argparse.ArgumentParser(description="台股波段量化交易系統 MVP 命令列介面 (CLI)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # 1. market group
    parser_market = subparsers.add_parser("market", help="市場行情數據管理")
    market_subs = parser_market.add_subparsers(dest="subcommand", required=True)
    
    parser_backfill = market_subs.add_parser("backfill", help="回補歷史日 K 線行情")
    parser_backfill.add_argument("--calendar-days", type=int, default=100, help="往回追溯的日曆天數")
    
    parser_sync = market_subs.add_parser("sync", help="同步特定日期的 K 線行情")
    parser_sync.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    
    parser_validate = market_subs.add_parser("validate", help="驗證資料庫中的日 K 線行情")
    parser_validate.add_argument("--last-sessions", type=int, default=60, help="驗證最近幾筆交易日的行情數據")
    
    # 2. strategy group
    parser_strategy = subparsers.add_parser("strategy", help="策略輔助指令")
    strategy_subs = parser_strategy.add_subparsers(dest="subcommand", required=True)
    
    parser_inspect = strategy_subs.add_parser("inspect", help="檢查策略設定並輸出參數 SHA-256 指紋")
    parser_inspect.add_argument("config_path", type=str, help="策略 YAML 設定檔路徑")
    
    # 3. approval group
    parser_approval = subparsers.add_parser("approval", help="策略授權清單 (Manifest) 管理")
    approval_subs = parser_approval.add_subparsers(dest="subcommand", required=True)
    
    parser_app_create = approval_subs.add_parser("create", help="建立已簽署的策略授權清單 JSON 檔")
    parser_app_create.add_argument("--strategy", type=str, required=True, help="策略 YAML 設定檔")
    parser_app_create.add_argument("--expires-at", type=str, required=True, help="授權到期時間 (ISO 格式字串)")
    parser_app_create.add_argument("--output", type=str, required=True, help="輸出的授權清單 JSON 檔案路徑")
    parser_app_create.add_argument("--issuer", type=str, default="manual-research-review", help="發行者識別 ID")
    parser_app_create.add_argument("--max-order-value", type=int, default=35000, help="單筆委託最大金額 (TWD)")
    parser_app_create.add_argument("--max-daily-buy-value", type=int, default=150000, help="單日累計買入最大金額 (TWD)")
    parser_app_create.add_argument("--max-open-positions", type=int, default=5, help="最大持倉部位限制數量")
    parser_app_create.add_argument("--valid-from", type=str, help="授權起始時間 (ISO 格式字串)")
    
    parser_app_val = approval_subs.add_parser("validate", help="驗證授權清單的完整性與簽章")
    parser_app_val.add_argument("manifest_path", type=str, help="授權清單 JSON 檔案路徑")
    
    parser_app_act = approval_subs.add_parser("activate", help="啟用並將授權清單設為當前執行目標")
    parser_app_act.add_argument("manifest_path", type=str, help="授權清單 JSON 檔案路徑")
    
    approval_subs.add_parser("status", help="顯示當前啟用授權清單的每日預檢狀態 (Preflight)")
    
    # 4. account group
    parser_account = subparsers.add_parser("account", help="帳戶管理")
    account_subs = parser_account.add_subparsers(dest="subcommand", required=True)
    
    parser_acc_init = account_subs.add_parser("init", help="初始化投資組合帳戶")
    parser_acc_init.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_acc_init.add_argument("--initial-cash", type=int, required=True, help="初始台幣現金金額")
    
    parser_acc_adjust = account_subs.add_parser("adjust-cash", help="調整/設定帳戶的初始剩餘金額")
    parser_acc_adjust.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_acc_adjust.add_argument("--amount", type=int, required=True, help="設定的新初始台幣現金金額")
    
    # 5. backtest group
    parser_backtest = subparsers.add_parser("backtest", help="歷史回測執行")
    backtest_subs = parser_backtest.add_subparsers(dest="subcommand", required=True)
    
    parser_bt_run = backtest_subs.add_parser("run", help="執行歷史回測")
    parser_bt_run.add_argument("--from", dest="start", type=str, required=True, help="回測開始日期 YYYY-MM-DD")
    parser_bt_run.add_argument("--to", type=str, required=True, help="回測結束日期 YYYY-MM-DD")
    parser_bt_run.add_argument("--initial-cash", type=int, default=300000, help="初始現金金額")
    
    # 6. simulation group
    parser_sim = subparsers.add_parser("simulation", help="模擬交易執行器指令")
    sim_subs = parser_sim.add_subparsers(dest="subcommand", required=True)
    
    parser_sim_daily = sim_subs.add_parser("run-daily", help="執行每日模擬交易工作流")
    parser_sim_daily.add_argument("--date", type=str, help="執行日期 YYYY-MM-DD")
    parser_sim_daily.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_sim_exec = sim_subs.add_parser("execute-pending", help="執行待處理的交易訊號包")
    parser_sim_exec.add_argument("--execution-date", type=str, help="執行委託的日期 YYYY-MM-DD")
    parser_sim_exec.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_sim_reset = sim_subs.add_parser("reset", help="重置特定日期的模擬狀態與已產生的交易訊號")
    parser_sim_reset.add_argument("--date", type=str, required=True, help="指定重置的日期 YYYY-MM-DD")
    
    # 7. signal group
    parser_sig = subparsers.add_parser("signal", help="交易訊號產生器指令")
    sig_subs = parser_sig.add_subparsers(dest="subcommand", required=True)
    
    parser_sig_gen = sig_subs.add_parser("generate", help="手動產生收盤交易訊號")
    parser_sig_gen.add_argument("--as-of-date", type=str, help="作為基準的收盤日期 YYYY-MM-DD")
    parser_sig_gen.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_sig_list = sig_subs.add_parser("list", help="查詢並列出已產生的交易訊號")
    parser_sig_list.add_argument("--date", type=str, help="過濾特定的訊號產生日期 YYYY-MM-DD")
    
    # 8. trade group
    parser_trade = subparsers.add_parser("trade", help="交易與委託單指令")
    trade_subs = parser_trade.add_subparsers(dest="subcommand", required=True)
    
    parser_trade_plan = trade_subs.add_parser("plan", help="產生委託計畫預覽")
    parser_trade_plan.add_argument("--bundle", type=str, required=True, help="訊號包 JSON 檔案路徑，或資料庫中的日期/Bundle ID")
    parser_trade_plan.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_trade_record = trade_subs.add_parser("record-fill", help="手動錄入成交交易資料")
    parser_trade_record.add_argument("--symbol", type=str, required=True, help="股票代號")
    parser_trade_record.add_argument("--side", type=str, required=True, choices=["BUY", "SELL"], help="交易動作 (BUY/SELL)")
    parser_trade_record.add_argument("--quantity", type=int, required=True, help="交易股數")
    parser_trade_record.add_argument("--price", type=float, required=True, help="每股成交價格")
    parser_trade_record.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_trade_close = trade_subs.add_parser("close-all", help="強制平倉所有持有部位（緊急避險退出）")
    parser_trade_close.add_argument("--broker", type=str, default="fake", help="券商介面名稱")
    parser_trade_close.add_argument("--reason", type=str, required=True, help="緊急平倉的原因")
    
    # 9. portfolio group
    parser_portfolio = subparsers.add_parser("portfolio", help="投資組合對帳與投影重建")
    portfolio_subs = parser_portfolio.add_subparsers(dest="subcommand", required=True)
    
    parser_port_rec = portfolio_subs.add_parser("reconcile", help="進行持倉投影與現金流水帳對帳")
    parser_port_rec.add_argument("--account", type=str, default=None, help="帳戶名稱")
    
    parser_port_reb = portfolio_subs.add_parser("rebuild-projections", help="從交易歷史事實重建持倉投影表")
    parser_port_reb.add_argument("--account", type=str, default=None, help="帳戶名稱")
    
    # 10. report group
    parser_report = subparsers.add_parser("report", help="損益與資產淨值曲線報告")
    report_subs = parser_report.add_subparsers(dest="subcommand", required=True)
    
    parser_rep_pnl = report_subs.add_parser("pnl", help="顯示帳戶的損益對帳單摘要")
    parser_rep_pnl.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_rep_pnl.add_argument("--date", type=str, help="指定報告日期 YYYY-MM-DD")
    
    # Dispatching commands
    args = parser.parse_args()
    
    handlers = {
        ("market", "backfill"): cmd_market_backfill,
        ("market", "sync"): cmd_market_sync,
        ("market", "validate"): cmd_market_validate,
        ("strategy", "inspect"): cmd_strategy_inspect,
        ("approval", "create"): cmd_approval_create,
        ("approval", "validate"): cmd_approval_validate,
        ("approval", "activate"): cmd_approval_activate,
        ("approval", "status"): cmd_approval_status,
        ("account", "init"): cmd_account_init,
        ("account", "adjust-cash"): cmd_account_adjust_cash,
        ("backtest", "run"): cmd_backtest_run,
        ("simulation", "run-daily"): cmd_simulation_run_daily,
        ("simulation", "execute-pending"): cmd_simulation_execute_pending,
        ("simulation", "reset"): cmd_simulation_reset,
        ("signal", "generate"): cmd_signal_generate,
        ("signal", "list"): cmd_signal_list,
        ("trade", "plan"): cmd_trade_plan,
        ("trade", "record-fill"): cmd_trade_record_fill,
        ("trade", "close-all"): cmd_trade_close_all,
        ("portfolio", "reconcile"): cmd_portfolio_reconcile,
        ("portfolio", "rebuild-projections"): cmd_portfolio_rebuild_projections,
        ("report", "pnl"): cmd_report_pnl,
    }
    
    key = (args.command, args.subcommand) if hasattr(args, "subcommand") else (args.command, None)
    
    # Simple fix for status commands that do not have sub-subcommand
    if args.command == "approval" and args.subcommand == "status":
        key = ("approval", "status")
        
    handler = handlers.get(key)
    if handler:
        handler(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
