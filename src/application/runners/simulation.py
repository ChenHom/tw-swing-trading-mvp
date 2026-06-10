import sqlite3
import uuid
import hashlib
from datetime import date, datetime
from typing import Optional, Any
from src.contracts.models import (
    DailySignalBundle, SignalItem, ExecutionContext, StrategyApprovalManifest, StrategyInfo, TrendPullbackParams
)
from src.calendar.calendar import TradingCalendar
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.strategy.trend_pullback import TrendPullbackStrategy
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.application.execution.engine import TradeExecutionEngine
from src.approval.validator import ManifestValidator
from src.market_data.aggregator import DailyBarAggregator, MarketBarValidator

class DailySimulationRunner:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        calendar: TradingCalendar,
        market_provider, # MarketDataProvider Protocol
        market_repo: SqliteMarketBarRepository,
        projection: PortfolioProjection,
        allowed_issuers: list[str],
        revoked_approvals: list[str],
        manifest: Optional[StrategyApprovalManifest] = None,
        strategy_budget: int = 30000,
        slippage_bps: int = 10,
        expiry_warning_sessions: int = 3
    ):
        self.db_conn = db_conn
        self.calendar = calendar
        self.market_provider = market_provider
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifest = manifest
        self.strategy_budget = strategy_budget
        self.slippage_bps = slippage_bps
        self.expiry_warning_sessions = expiry_warning_sessions

    def get_preflight_status(self, current_date: date) -> str:
        """
        Check active manifest validity and return one of the preflight status codes:
        'ACTIVE', 'EXPIRING_SOON', 'NOT_YET_VALID', 'EXPIRED', 'REVOKED', 'MISSING', 'INVALID'
        """
        if not self.manifest:
            return "MISSING"
            
        if self.manifest.approval_id in self.revoked_approvals:
            return "REVOKED"
            
        # Verify integrity and allowed issuers
        validator = ManifestValidator(self.allowed_issuers, self.revoked_approvals)
        current_time = datetime.fromisoformat(f"{current_date.isoformat()}T09:00:00+08:00")
        try:
            # Mode required is simulation
            validator.validate(self.manifest, current_time, "simulation")
        except ValueError as e:
            err_msg = str(e)
            if "INTEGRITY_INVALID" in err_msg:
                return "INVALID"
            elif "ISSUER_NOT_TRUSTED" in err_msg:
                return "INVALID"
            elif "MANIFEST_REVOKED" in err_msg:
                return "REVOKED"
            elif "MANIFEST_NOT_YET_VALID" in err_msg:
                return "NOT_YET_VALID"
            elif "MANIFEST_EXPIRED" in err_msg:
                return "EXPIRED"
            elif "EXECUTION_MODE_NOT_ALLOWED" in err_msg:
                return "INVALID"
            else:
                return "INVALID"
                
        # Parse expiration date
        try:
            expires_at = datetime.fromisoformat(self.manifest.validity.expires_at).date()
            valid_from = datetime.fromisoformat(self.manifest.validity.valid_from).date()
        except Exception:
            return "INVALID"
            
        if current_date < valid_from:
            return "NOT_YET_VALID"
            
        if current_date >= expires_at:
            return "EXPIRED"
            
        # Check if expiring soon
        sessions = self.calendar.sessions_between(current_date, expires_at)
        if len(sessions) <= self.expiry_warning_sessions:
            return "EXPIRING_SOON"
            
        return "ACTIVE"

    def run_daily(
        self,
        run_date: date,
        account_id: str,
        strategy_id: str,
        strategy_params: TrendPullbackParams,
        universe_symbols: list[str]
    ) -> str:
        """
        Run the daily simulation workflow.
        Returns the final status: 'COMPLETED', 'WAITING', 'FAILED', or 'SKIPPED'.
        """
        # 0. Check if it's a trading day
        if not self.calendar.is_trading_day(run_date):
            self._upsert_run(
                run_date=run_date,
                account_id=account_id,
                strategy_id=strategy_id,
                status="SKIPPED",
                market_sync_status="PENDING",
                execution_status="PENDING",
                signal_generation_status="PENDING",
                report_status="PENDING",
                last_error="NON_TRADING_DAY"
            )
            return "SKIPPED"
            
        # Get or create daily run record
        run_record = self._get_or_create_run(run_date, account_id, strategy_id)
        run_id = run_record["run_id"]
        
        # If already COMPLETED, skip and return
        if run_record["status"] == "COMPLETED":
            return "COMPLETED"
            
        try:
            # Stage 1: Market Sync & Aggregation
            if run_record["market_sync_status"] != "COMPLETED":
                self._update_run_status(run_date, account_id, strategy_id, status="STARTED")
                
                valuation_symbols = self._get_valuation_universe(universe_symbols)
                success = self.sync_market_data(run_date, valuation_symbols)
                if not success:
                    self._upsert_run(
                        run_date=run_date,
                        account_id=account_id,
                        strategy_id=strategy_id,
                        status="WAITING",
                        market_sync_status="PENDING",
                        execution_status=run_record["execution_status"],
                        signal_generation_status=run_record["signal_generation_status"],
                        report_status=run_record["report_status"],
                        last_error="WAITING_MARKET_DATA"
                    )
                    return "WAITING"
                
                self._update_run_sub_status(run_date, account_id, strategy_id, "market_sync_status", "COMPLETED")
                
            # Refresh run record
            run_record = self._get_or_create_run(run_date, account_id, strategy_id)
            
            # Preflight manifest verification
            preflight = self.get_preflight_status(run_date)
            print(f"[run_daily] Active manifest preflight check: {preflight}")
            # If preflight status is blocker (EXPIRED, REVOKED, MISSING, INVALID)
            # we block BUY orders, but we can still run engine which handles this order planning.
            # However, if MISSING or INVALID, we can log a warning.
            
            # Stage 2: Execution of pending signals
            if run_record["execution_status"] != "COMPLETED":
                # Find signal bundle generated on the previous trading day targeting run_date
                bundle = self._find_bundle_for_execution(account_id, run_date)
                if bundle:
                    context = ExecutionContext(
                        run_id=run_id,
                        run_type="DAILY_SIMULATION",
                        as_of_date=bundle.signal_date,
                        execution_date=run_date,
                        account_id=account_id
                    )
                    engine = TradeExecutionEngine(
                        db_conn=self.db_conn,
                        market_repo=self.market_repo,
                        projection=self.projection,
                        allowed_issuers=self.allowed_issuers,
                        revoked_approvals=self.revoked_approvals,
                        manifest=self.manifest,
                        strategy_budget=self.strategy_budget,
                        slippage_bps=self.slippage_bps
                    )
                    exec_result = engine.execute_bundle(context, bundle)
                    if exec_result.get("status") == "WAITING_MARKET_DATA":
                        self._upsert_run(
                            run_date=run_date,
                            account_id=account_id,
                            strategy_id=strategy_id,
                            status="WAITING",
                            market_sync_status="COMPLETED",
                            execution_status="PENDING",
                            signal_generation_status=run_record["signal_generation_status"],
                            report_status=run_record["report_status"],
                            last_error="WAITING_MARKET_DATA"
                        )
                        return "WAITING"
                
                self._update_run_sub_status(run_date, account_id, strategy_id, "execution_status", "COMPLETED")
                
            # Refresh run record
            run_record = self._get_or_create_run(run_date, account_id, strategy_id)
            
            # Stage 3: Signal Generation close of run_date
            if run_record["signal_generation_status"] != "COMPLETED":
                strategy = TrendPullbackStrategy(
                    params=strategy_params,
                    universe_symbols=universe_symbols
                )
                pit_data = self.market_repo.as_of(run_date)
                
                # Verify that run_date market bars are complete for strategy query
                for symbol in universe_symbols:
                    if not self.market_repo.find(symbol, run_date):
                        raise ValueError(f"DATASET_INCOMPLETE: Missing market bar for {symbol} on {run_date}")
                
                # Fetch current portfolio state
                available_cash = self.projection.get_cash_balance(account_id)
                positions = {}
                cursor = self.db_conn.cursor()
                cursor.execute(
                    "SELECT symbol, SUM(quantity) as qty, AVG(price) as avg_price, MAX(is_long_term) as is_long_term FROM position_lots WHERE account_id = ? GROUP BY symbol",
                    (account_id,)
                )
                for row in cursor.fetchall():
                    qty = row["qty"]
                    if qty > 0:
                        positions[row["symbol"]] = PositionSnapshot(
                            symbol=row["symbol"],
                            quantity=qty,
                            entry_price=int(row["avg_price"]),
                            is_long_term=bool(row["is_long_term"])
                        )
                portfolio_snapshot = PortfolioSnapshot(
                    available_cash=available_cash,
                    positions=positions
                )
                
                sig_ctx = SignalGenerationContext(
                    as_of_date=run_date,
                    strategy_id=strategy_id,
                    strategy_version=self.manifest.strategy.strategy_version if self.manifest else "1.0.0",
                    run_id=run_id,
                    approval_id=self.manifest.approval_id if self.manifest else "app-m4-dummy",
                    params_hash=self.manifest.strategy.params_hash if self.manifest else "hash-dummy"
                )
                
                new_bundle = strategy.generate(sig_ctx, pit_data, portfolio_snapshot)
                
                # Calculate target execution date (next trading day)
                target_execution_date = self.calendar.next_trading_day(run_date)
                
                # Save bundle to database
                self._save_bundle(new_bundle, target_execution_date)
                self._update_run_sub_status(run_date, account_id, strategy_id, "signal_generation_status", "COMPLETED")
                
            # Stage 4: Reporting
            if run_record["report_status"] != "COMPLETED":
                # Mark as completed
                self._update_run_sub_status(run_date, account_id, strategy_id, "report_status", "COMPLETED")
                
            # Mark whole run as COMPLETED
            self._upsert_run(
                run_date=run_date,
                account_id=account_id,
                strategy_id=strategy_id,
                status="COMPLETED",
                market_sync_status="COMPLETED",
                execution_status="COMPLETED",
                signal_generation_status="COMPLETED",
                report_status="COMPLETED",
                last_error=None
            )
            return "COMPLETED"
            
        except Exception as e:
            # Mark run as FAILED
            self._upsert_run(
                run_date=run_date,
                account_id=account_id,
                strategy_id=strategy_id,
                status="FAILED",
                market_sync_status=run_record["market_sync_status"],
                execution_status=run_record["execution_status"],
                signal_generation_status=run_record["signal_generation_status"],
                report_status=run_record["report_status"],
                last_error=str(e)
            )
            raise e

    def sync_market_data(self, sync_date: date, symbols: list[str]) -> bool:
        aggregator = DailyBarAggregator()
        validator = MarketBarValidator()
        
        for symbol in symbols:
            bars = self.market_provider.fetch_kbars(symbol, sync_date, sync_date)
            if not bars:
                return False
            
            # Simple dummy fetch time and checksum
            fetched_at = datetime.now().isoformat()
            raw_payload = "".join(b.model_dump_json() for b in bars)
            checksum = hashlib.sha256(raw_payload.encode()).hexdigest()
            
            daily_bar = aggregator.aggregate(
                symbol=symbol,
                exchange="TSE",
                instrument_type="STOCK",
                trade_date=sync_date,
                minute_bars=bars,
                source="shioaji",
                source_fetched_at=fetched_at,
                raw_payload_checksum=checksum
            )
            if not daily_bar:
                return False
                
            validator.validate(daily_bar)
            self.market_repo.upsert(daily_bar)
        return True

    def _get_or_create_run(self, run_date: date, account_id: str, strategy_id: str) -> dict[str, Any]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT run_id, run_date, account_id, strategy_id, status, market_sync_status,
                   execution_status, signal_generation_status, report_status, started_at, completed_at, last_error_code
            FROM daily_runs
            WHERE run_date = ? AND account_id = ? AND strategy_id = ?
            """,
            (run_date.isoformat(), account_id, strategy_id)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
            
        # Create a new run record
        run_id = f"sim-{run_date.strftime('%Y%m%d')}"
        cursor.execute(
            """
            INSERT INTO daily_runs (
                run_id, run_date, account_id, strategy_id, status, market_sync_status,
                execution_status, signal_generation_status, report_status, started_at, completed_at, last_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '', '')
            """,
            (run_id, run_date.isoformat(), account_id, strategy_id, "STARTED", "PENDING", "PENDING", "PENDING", "PENDING")
        )
        self.db_conn.commit()
        
        return {
            "run_id": run_id,
            "run_date": run_date.isoformat(),
            "account_id": account_id,
            "strategy_id": strategy_id,
            "status": "STARTED",
            "market_sync_status": "PENDING",
            "execution_status": "PENDING",
            "signal_generation_status": "PENDING",
            "report_status": "PENDING",
            "started_at": datetime.now().isoformat(),
            "completed_at": "",
            "last_error_code": ""
        }

    def _upsert_run(
        self,
        run_date: date,
        account_id: str,
        strategy_id: str,
        status: str,
        market_sync_status: str,
        execution_status: str,
        signal_generation_status: str,
        report_status: str,
        last_error: Optional[str]
    ) -> None:
        cursor = self.db_conn.cursor()
        
        # Check if exists
        cursor.execute(
            "SELECT run_id FROM daily_runs WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
            (run_date.isoformat(), account_id, strategy_id)
        )
        row = cursor.fetchone()
        
        completed_at = datetime.now().isoformat() if status == "COMPLETED" else ""
        
        if row:
            cursor.execute(
                """
                UPDATE daily_runs SET
                    status = ?,
                    market_sync_status = ?,
                    execution_status = ?,
                    signal_generation_status = ?,
                    report_status = ?,
                    completed_at = ?,
                    last_error_code = ?
                WHERE run_date = ? AND account_id = ? AND strategy_id = ?
                """,
                (status, market_sync_status, execution_status, signal_generation_status, report_status, completed_at, last_error, run_date.isoformat(), account_id, strategy_id)
            )
        else:
            run_id = f"sim-{run_date.strftime('%Y%m%d')}"
            cursor.execute(
                """
                INSERT INTO daily_runs (
                    run_id, run_date, account_id, strategy_id, status, market_sync_status,
                    execution_status, signal_generation_status, report_status, started_at, completed_at, last_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                (run_id, run_date.isoformat(), account_id, strategy_id, status, market_sync_status, execution_status, signal_generation_status, report_status, completed_at, last_error)
            )
        self.db_conn.commit()

    def _update_run_status(self, run_date: date, account_id: str, strategy_id: str, status: str) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute(
            "UPDATE daily_runs SET status = ? WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
            (status, run_date.isoformat(), account_id, strategy_id)
        )
        self.db_conn.commit()

    def _update_run_sub_status(self, run_date: date, account_id: str, strategy_id: str, column: str, status: str) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute(
            f"UPDATE daily_runs SET {column} = ? WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
            (status, run_date.isoformat(), account_id, strategy_id)
        )
        self.db_conn.commit()

    def _find_bundle_for_execution(self, account_id: str, execution_date: date) -> Optional[DailySignalBundle]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
            FROM signal_bundles
            WHERE target_execution_date = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (execution_date.isoformat(),)
        )
        r = cursor.fetchone()
        if not r:
            return None
            
        bundle_id = r["bundle_id"]
        
        cursor.execute(
            """
            SELECT signal_id, symbol, action, reference_price, reason_code
            FROM signal_items
            WHERE bundle_id = ?
            """,
            (bundle_id,)
        )
        items = []
        for row in cursor.fetchall():
            items.append(
                SignalItem(
                    signal_id=row["signal_id"],
                    symbol=row["symbol"],
                    action=row["action"],
                    reference_price=float(row["reference_price"] / 10000.0),
                    reason_code=row["reason_code"]
                )
            )
            
        strategy_info = StrategyInfo(
            strategy_id=r["strategy_id"],
            strategy_version=r["strategy_version"],
            params_canonicalization="strategy-params-v1",
            params_hash=r["params_hash"]
        )
        
        return DailySignalBundle(
            schema_version="1.0",
            bundle_id=bundle_id,
            run_id=r["run_id"],
            approval_id=r["approval_id"],
            strategy=strategy_info,
            signal_date=date.fromisoformat(r["signal_date"]),
            target_execution_date=date.fromisoformat(r["target_execution_date"]),
            market_data_cutoff=date.fromisoformat(r["market_data_cutoff"]),
            signals=items
        )

    def _save_bundle(self, bundle: DailySignalBundle, target_execution_date: date) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO signal_bundles (
                bundle_id, run_id, approval_id, strategy_id, strategy_version,
                params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                bundle.bundle_id,
                bundle.run_id,
                bundle.approval_id,
                bundle.strategy.strategy_id,
                bundle.strategy.strategy_version,
                bundle.strategy.params_hash,
                bundle.signal_date.isoformat(),
                target_execution_date.isoformat(),
                bundle.market_data_cutoff.isoformat()
            )
        )
        for sig in bundle.signals:
            cursor.execute(
                """
                INSERT INTO signal_items (
                    item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    f"item-{uuid.uuid4().hex[:8]}",
                    bundle.bundle_id,
                    sig.signal_id,
                    sig.symbol,
                    sig.action,
                    int(round(sig.reference_price * 10000)),
                    sig.reason_code
                )
            )
        self.db_conn.commit()

    def _get_valuation_universe(self, strategy_symbols: list[str]) -> list[str]:
        cursor = self.db_conn.cursor()
        # 1. Open positions
        cursor.execute("SELECT DISTINCT symbol FROM position_lots WHERE quantity > 0")
        open_symbols = [row["symbol"] for row in cursor.fetchall()]
        
        # 2. Strategy signals (buy/sell targets in fills)
        cursor.execute("SELECT DISTINCT symbol FROM fills")
        fill_symbols = [row["symbol"] for row in cursor.fetchall()]
        
        # Union them
        val_set = set(strategy_symbols).union(open_symbols).union(fill_symbols)
        return sorted(list(val_set))
