import argparse
import sys
import os
import json
import hashlib
import sqlite3
from datetime import date, datetime, timezone
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
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    
    run_id = f"init-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    ledger.deposit(args.account, run_id, args.initial_cash, "TWD", date.today())
    projection.rebuild_from_ledger(args.account)
    
    print(f"Account '{args.account}' initialized with {args.initial_cash} TWD.")
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
    
    print(f"Running daily simulation workflow for {run_date}...")
    status = runner.run_daily(
        run_date=run_date,
        account_id=args.account,
        strategy_id="trend_pullback",
        strategy_params=params,
        universe_symbols=symbols
    )
    
    print(f"Simulation runner finished with status: {status}")
    if status == "FAILED":
        sys.exit(1)
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
    
    context = ExecutionContext(
        run_id=f"exec-only-{datetime.now().strftime('%H%M%S')}",
        run_type="DAILY_SIMULATION",
        as_of_date=bundle.signal_date,
        execution_date=run_date,
        account_id=args.account
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
    available_cash = projection.get_cash_balance(args.account)
    positions = {}
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty, AVG(price) as avg_price FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (args.account,)
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

def cmd_trade_plan(args):
    settings = get_settings()
    filepath = Path(args.bundle)
    if not filepath.exists():
        print(f"Signal bundle file not found: {args.bundle}")
        sys.exit(1)
        
    with open(filepath, "r", encoding="utf-8") as f:
        bundle_dict = json.load(f)
    
    # Reconstruct bundle object
    bundle = DailySignalBundle(**bundle_dict)
    
    manifest = load_active_manifest(settings)
    if not manifest:
        print("Warning: No active manifest loaded. Standard limits will apply.")
        
    # Print planning preview
    print(f"Planning preview for bundle {bundle.bundle_id}:")
    for sig in bundle.signals:
        print(f"  Signal: {sig.symbol} {sig.action} @ {sig.reference_price}")
        
def cmd_portfolio_reconcile(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    print(f"Reconciling account {args.account}...")
    errors = projection.reconcile(args.account)
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
    
    print(f"Rebuilding projections for account {args.account}...")
    projection.rebuild_from_ledger(args.account)
    print("Projections successfully rebuilt from ledger facts.")
    conn.close()

def cmd_report_pnl(args):
    settings = get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    
    report_date = date.fromisoformat(args.date) if args.date else date.today()
    cash = projection.get_cash_balance(args.account)
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty, AVG(price) as avg_price FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (args.account,)
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
            
    print(f"\n--- PnL Report for Account {args.account} on {report_date} ---")
    print(f"Available Cash: {cash:,} TWD")
    print(f"Position Value: {total_pos_value:,} TWD")
    print(f"Total Equity  : {cash + total_pos_value:,} TWD")
    print("\nPositions:")
    if not positions:
        print("  No open positions.")
    for pos in positions:
        print(f"  {pos['symbol']}: {pos['quantity']} shares @ avg {pos['entry_price']:.2f} (current: {pos['current_price']:.2f}) - Value: {pos['value']:,} TWD")
        
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
    parser = argparse.ArgumentParser(description="Taiwan Stock Swing Trading MVP CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # 1. market group
    parser_market = subparsers.add_parser("market", help="Market data management")
    market_subs = parser_market.add_subparsers(dest="subcommand", required=True)
    
    parser_backfill = market_subs.add_parser("backfill", help="Backfill historical market bars")
    parser_backfill.add_argument("--calendar-days", type=int, default=100, help="Number of calendar days to look back")
    
    parser_sync = market_subs.add_parser("sync", help="Sync market bars for a specific date")
    parser_sync.add_argument("--date", type=str, help="Specific date YYYY-MM-DD")
    
    parser_validate = market_subs.add_parser("validate", help="Validate market bars in database")
    parser_validate.add_argument("--last-sessions", type=int, default=60, help="Number of trading sessions to validate")
    
    # 2. strategy group
    parser_strategy = subparsers.add_parser("strategy", help="Strategy helper commands")
    strategy_subs = parser_strategy.add_subparsers(dest="subcommand", required=True)
    
    parser_inspect = strategy_subs.add_parser("inspect", help="Inspect strategy configuration and print parameter hash")
    parser_inspect.add_argument("config_path", type=str, help="Path to strategy config yaml")
    
    # 3. approval group
    parser_approval = subparsers.add_parser("approval", help="Strategy approval manifest management")
    approval_subs = parser_approval.add_subparsers(dest="subcommand", required=True)
    
    parser_app_create = approval_subs.add_parser("create", help="Create signed StrategyApprovalManifest")
    parser_app_create.add_argument("--strategy", type=str, required=True, help="Strategy config yaml file")
    parser_app_create.add_argument("--expires-at", type=str, required=True, help="Expiration date ISO string")
    parser_app_create.add_argument("--output", type=str, required=True, help="Output manifest JSON file")
    parser_app_create.add_argument("--issuer", type=str, default="manual-research-review", help="Issuer ID")
    parser_app_create.add_argument("--max-order-value", type=int, default=35000, help="Max TWD per order")
    parser_app_create.add_argument("--max-daily-buy-value", type=int, default=150000, help="Max TWD daily buy value")
    parser_app_create.add_argument("--max-open-positions", type=int, default=5, help="Max number of open positions")
    parser_app_create.add_argument("--valid-from", type=str, help="Valid from datetime ISO string")
    
    parser_app_val = approval_subs.add_parser("validate", help="Validate manifest integrity signature")
    parser_app_val.add_argument("manifest_path", type=str, help="Path to manifest JSON")
    
    parser_app_act = approval_subs.add_parser("activate", help="Activate approval manifest")
    parser_app_act.add_argument("manifest_path", type=str, help="Path to manifest JSON")
    
    approval_subs.add_parser("status", help="Show active manifest preflight status")
    
    # 4. account group
    parser_account = subparsers.add_parser("account", help="Account management")
    account_subs = parser_account.add_subparsers(dest="subcommand", required=True)
    
    parser_acc_init = account_subs.add_parser("init", help="Initialize a portfolio account")
    parser_acc_init.add_argument("--account", type=str, required=True, help="Account name")
    parser_acc_init.add_argument("--initial-cash", type=int, required=True, help="Initial TWD cash amount")
    
    # 5. backtest group
    parser_backtest = subparsers.add_parser("backtest", help="Backtest execution")
    backtest_subs = parser_backtest.add_subparsers(dest="subcommand", required=True)
    
    parser_bt_run = backtest_subs.add_parser("run", help="Run backtest")
    parser_bt_run.add_argument("--from", dest="start", type=str, required=True, help="Start date YYYY-MM-DD")
    parser_bt_run.add_argument("--to", type=str, required=True, help="End date YYYY-MM-DD")
    parser_bt_run.add_argument("--initial-cash", type=int, default=300000, help="Initial cash amount")
    
    # 6. simulation group
    parser_sim = subparsers.add_parser("simulation", help="Simulation runner commands")
    sim_subs = parser_sim.add_subparsers(dest="subcommand", required=True)
    
    parser_sim_daily = sim_subs.add_parser("run-daily", help="Run daily simulation workflow")
    parser_sim_daily.add_argument("--date", type=str, help="Run date YYYY-MM-DD")
    parser_sim_daily.add_argument("--account", type=str, default="simulation-main", help="Target account name")
    
    parser_sim_exec = sim_subs.add_parser("execute-pending", help="Execute pending signal bundle")
    parser_sim_exec.add_argument("--execution-date", type=str, help="Execution date YYYY-MM-DD")
    parser_sim_exec.add_argument("--account", type=str, default="simulation-main", help="Target account name")
    
    # 7. signal group
    parser_sig = subparsers.add_parser("signal", help="Signal generator commands")
    sig_subs = parser_sig.add_subparsers(dest="subcommand", required=True)
    
    parser_sig_gen = sig_subs.add_parser("generate", help="Generate closing signals manually")
    parser_sig_gen.add_argument("--as-of-date", type=str, help="As of date YYYY-MM-DD")
    parser_sig_gen.add_argument("--account", type=str, default="simulation-main", help="Target account name")
    
    # 8. trade group
    parser_trade = subparsers.add_parser("trade", help="Trade and order commands")
    trade_subs = parser_trade.add_subparsers(dest="subcommand", required=True)
    
    parser_trade_plan = trade_subs.add_parser("plan", help="Generate order planning preview")
    parser_trade_plan.add_argument("--bundle", type=str, required=True, help="Path to signal bundle JSON")
    
    parser_trade_close = trade_subs.add_parser("close-all", help="Close all open positions (Emergency exit)")
    parser_trade_close.add_argument("--broker", type=str, default="fake", help="Broker name")
    parser_trade_close.add_argument("--reason", type=str, required=True, help="Reason for emergency close")
    
    # 9. portfolio group
    parser_portfolio = subparsers.add_parser("portfolio", help="Portfolio reconciliation and rebuilding")
    portfolio_subs = parser_portfolio.add_subparsers(dest="subcommand", required=True)
    
    parser_port_rec = portfolio_subs.add_parser("reconcile", help="Reconcile projections with cash ledger facts")
    parser_port_rec.add_argument("--account", type=str, required=True, help="Account name")
    
    parser_port_reb = portfolio_subs.add_parser("rebuild-projections", help="Rebuild projections from transaction log facts")
    parser_port_reb.add_argument("--account", type=str, required=True, help="Account name")
    
    # 10. report group
    parser_report = subparsers.add_parser("report", help="PnL and equity curve reports")
    report_subs = parser_report.add_subparsers(dest="subcommand", required=True)
    
    parser_rep_pnl = report_subs.add_parser("pnl", help="Show PnL summary for account")
    parser_rep_pnl.add_argument("--account", type=str, required=True, help="Account name")
    parser_rep_pnl.add_argument("--date", type=str, help="Specific date YYYY-MM-DD")
    
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
        ("backtest", "run"): cmd_backtest_run,
        ("simulation", "run-daily"): cmd_simulation_run_daily,
        ("simulation", "execute-pending"): cmd_simulation_execute_pending,
        ("signal", "generate"): cmd_signal_generate,
        ("trade", "plan"): cmd_trade_plan,
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
