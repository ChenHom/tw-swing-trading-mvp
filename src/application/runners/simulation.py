import sqlite3
import uuid
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Any
from src.contracts.models import (
    DailySignalBundle, SignalItem, ExecutionContext, StrategyApprovalManifest, StrategyInfo
)
from src.calendar.calendar import TradingCalendar
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.registry import StrategyDefinition
from src.strategy.risk_exit import RiskExitEngine
from src.application.execution.engine import TradeExecutionEngine
from src.approval.validator import ManifestValidator
from src.trading.allocator import GlobalLimits
from src.market_data.aggregator import DailyBarAggregator, MarketBarValidator

# daily_runs 採單一 orchestrator run（§2.11）：每日一列，strategy_id 固定為 MULTI。
# Per-strategy 觀測性由 signal_bundles 承擔（bundle 存在與否即為訊號產生的冪等標記）。
ORCHESTRATOR_STRATEGY_ID = "MULTI"


@dataclass
class EntryStrategySpec:
    definition: StrategyDefinition
    strategy: Any  # SignalGenerator protocol


def _normalize_symbol_spec(spec) -> dict:
    if isinstance(spec, str):
        return {"code": spec, "exchange": "TSE", "instrument_type": "STOCK"}
    if isinstance(spec, dict):
        return {
            "code": spec["code"],
            "exchange": spec.get("exchange", "TSE"),
            "instrument_type": spec.get("instrument_type", "STOCK"),
        }
    return {
        "code": spec.code,
        "exchange": getattr(spec, "exchange", "TSE"),
        "instrument_type": getattr(spec, "instrument_type", "STOCK"),
    }


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
        manifests: Optional[dict[str, StrategyApprovalManifest]] = None,
        entry_specs: Optional[list[EntryStrategySpec]] = None,
        exit_definitions: Optional[dict[str, StrategyDefinition]] = None,
        global_limits: Optional[GlobalLimits] = None,
        index_symbols: Optional[list] = None,
        slippage_bps: int = 10,
        expiry_warning_sessions: int = 3,
        unlimited_open_positions: bool = False
    ):
        self.db_conn = db_conn
        self.calendar = calendar
        self.market_provider = market_provider
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifests = manifests or {}
        self.entry_specs = entry_specs or []
        self.exit_definitions = exit_definitions or {}
        self.global_limits = global_limits or GlobalLimits()
        self.index_symbols = [_normalize_symbol_spec(s) for s in (index_symbols or [])]
        self.slippage_bps = slippage_bps
        self.expiry_warning_sessions = expiry_warning_sessions
        self.unlimited_open_positions = unlimited_open_positions

    @property
    def pipeline_order(self) -> list[str]:
        return [spec.definition.strategy_id for spec in self.entry_specs]

    def get_preflight_status(self, current_date: date, manifest: Optional[StrategyApprovalManifest]) -> str:
        """
        Check a manifest's validity and return one of the preflight status codes:
        'ACTIVE', 'EXPIRING_SOON', 'NOT_YET_VALID', 'EXPIRED', 'REVOKED', 'MISSING', 'INVALID'
        """
        if not manifest:
            return "MISSING"

        if manifest.approval_id in self.revoked_approvals:
            return "REVOKED"

        # Verify integrity and allowed issuers
        validator = ManifestValidator(self.allowed_issuers, self.revoked_approvals)
        current_time = datetime.fromisoformat(f"{current_date.isoformat()}T09:00:00+08:00")
        try:
            # Mode required is simulation
            validator.validate(manifest, current_time, "simulation")
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
            expires_at = datetime.fromisoformat(manifest.validity.expires_at).date()
            valid_from = datetime.fromisoformat(manifest.validity.valid_from).date()
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
        universe_symbols: list[str],
        auto_execute: bool = True
    ) -> str:
        """
        Run the daily multi-strategy simulation workflow (single orchestrator run).
        Returns the final status: 'COMPLETED', 'WAITING', 'FAILED', or 'SKIPPED'.
        """
        # 0. Check if it's a trading day
        if not self.calendar.is_trading_day(run_date):
            self._upsert_run(
                run_date=run_date,
                account_id=account_id,
                status="SKIPPED",
                market_sync_status="PENDING",
                execution_status="PENDING",
                signal_generation_status="PENDING",
                report_status="PENDING",
                last_error="NON_TRADING_DAY"
            )
            return "SKIPPED"

        # Get or create daily run record
        run_record = self._get_or_create_run(run_date, account_id)
        run_id = run_record["run_id"]

        # If already COMPLETED, skip and return
        if run_record["status"] == "COMPLETED":
            return "COMPLETED"

        try:
            # Stage 1: Market Sync & Aggregation (valuation universe + indices)
            if run_record["market_sync_status"] != "COMPLETED":
                self._update_run_status(run_date, account_id, status="STARTED")

                sync_specs = [
                    _normalize_symbol_spec(s)
                    for s in self._get_valuation_universe(universe_symbols)
                ] + self.index_symbols
                success = self.sync_market_data(run_date, sync_specs)
                if not success:
                    self._upsert_run(
                        run_date=run_date,
                        account_id=account_id,
                        status="WAITING",
                        market_sync_status="PENDING",
                        execution_status=run_record["execution_status"],
                        signal_generation_status=run_record["signal_generation_status"],
                        report_status=run_record["report_status"],
                        last_error="WAITING_MARKET_DATA"
                    )
                    return "WAITING"

                self._update_run_sub_status(run_date, account_id, "market_sync_status", "COMPLETED")

            # Refresh run record
            run_record = self._get_or_create_run(run_date, account_id)

            # Preflight manifest verification (per strategy)
            for sid in self.pipeline_order:
                preflight = self.get_preflight_status(run_date, self.manifests.get(sid))
                print(f"[run_daily] Manifest preflight [{sid}]: {preflight}")

            # Stage 2: Execution of pending signals (ALL bundles targeting run_date)
            # auto_execute=False（真實帳號）：跳過自動成交，僅標記完成後續跑規劃(Stage 3)。
            if run_record["execution_status"] != "COMPLETED":
                if auto_execute:
                    bundles = self._find_bundles_for_execution(run_date, account_id)
                    if bundles:
                        context = ExecutionContext(
                            run_id=run_id,
                            run_type="DAILY_SIMULATION",
                            as_of_date=bundles[0].signal_date,
                            execution_date=run_date,
                            account_id=account_id
                        )
                        engine = self._build_engine()
                        exec_result = engine.execute_bundles(context, bundles)
                        if exec_result.get("status") == "WAITING_MARKET_DATA":
                            self._upsert_run(
                                run_date=run_date,
                                account_id=account_id,
                                status="WAITING",
                                market_sync_status="COMPLETED",
                                execution_status="PENDING",
                                signal_generation_status=run_record["signal_generation_status"],
                                report_status=run_record["report_status"],
                                last_error="WAITING_MARKET_DATA"
                            )
                            return "WAITING"

                self._update_run_sub_status(run_date, account_id, "execution_status", "COMPLETED")

            # Refresh run record
            run_record = self._get_or_create_run(run_date, account_id)

            # Stage 2.5: Persist trailing-stop high watermarks (after fills, before exits)
            self.update_high_watermarks(account_id, run_date)

            # Stage 3: Signal Generation at run_date close
            if run_record["signal_generation_status"] != "COMPLETED":
                # Dataset completeness covers stocks AND index filters (§2.3)
                for symbol in universe_symbols:
                    if not self.market_repo.find(symbol, run_date):
                        raise ValueError(f"DATASET_INCOMPLETE: Missing market bar for {symbol} on {run_date}")
                for spec in self.index_symbols:
                    if not self.market_repo.find(spec["code"], run_date):
                        raise ValueError(f"DATASET_INCOMPLETE: Missing index bar for {spec['code']} on {run_date}")

                pit_data = self.market_repo.as_of(run_date)
                target_execution_date = self.calendar.next_trading_day(run_date)

                # 3a. risk_exit first (deterministic pipeline order, §2.9)
                risk_exit = RiskExitEngine(self.exit_definitions, self.projection, self.calendar)
                for bundle in risk_exit.generate_exit_bundles(run_date, account_id, pit_data, run_id):
                    # Exit bundles are private to this account (per-account exit facts).
                    self._save_bundle(bundle, target_execution_date, account_id=account_id)

                # 3b. entry strategies in configured order with per-strategy position view
                strategy_positions = self.projection.get_strategy_positions(account_id, include_long_term=True)
                for spec in self.entry_specs:
                    sid = spec.definition.strategy_id
                    manifest = self.manifests.get(sid)
                    positions = {}
                    for (pos_sid, symbol), pos in strategy_positions.items():
                        if pos_sid != sid or pos["quantity"] <= 0:
                            continue
                        positions[symbol] = PositionSnapshot(
                            symbol=symbol,
                            quantity=pos["quantity"],
                            entry_price=pos["wavg_price"],
                            is_long_term=pos["is_long_term"]
                        )
                    portfolio_snapshot = PortfolioSnapshot(
                        available_cash=self.projection.get_cash_balance(account_id),
                        positions=positions
                    )
                    sig_ctx = SignalGenerationContext(
                        as_of_date=run_date,
                        strategy_id=sid,
                        strategy_version=spec.definition.strategy_version,
                        run_id=run_id,
                        approval_id=manifest.approval_id if manifest else f"no-approval-{sid}",
                        params_hash=spec.definition.params_hash
                    )
                    new_bundle = spec.strategy.generate(sig_ctx, pit_data, portfolio_snapshot)
                    self._save_bundle(new_bundle, target_execution_date)

                # 3c. Persist the next-execution plan (revives order_intents): dry-run the
                # just-generated bundles through the SAME planning path the engine uses at
                # execution, so the dashboard's "下次執行" shows real planned shares/amount.
                self._persist_next_execution_intents(account_id, run_id, target_execution_date)

                self._update_run_sub_status(run_date, account_id, "signal_generation_status", "COMPLETED")

            # Stage 4: Reporting
            if run_record["report_status"] != "COMPLETED":
                self._update_run_sub_status(run_date, account_id, "report_status", "COMPLETED")

            # Mark whole run as COMPLETED
            self._upsert_run(
                run_date=run_date,
                account_id=account_id,
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
                status="FAILED",
                market_sync_status=run_record["market_sync_status"],
                execution_status=run_record["execution_status"],
                signal_generation_status=run_record["signal_generation_status"],
                report_status=run_record["report_status"],
                last_error=str(e)
            )
            raise e

    def _build_engine(self) -> TradeExecutionEngine:
        """Construct the multi-strategy execution engine with this runner's config.
        Shared by Stage 2 (real execution) and Stage 3c (dry-run intent persistence)."""
        return TradeExecutionEngine(
            db_conn=self.db_conn,
            market_repo=self.market_repo,
            projection=self.projection,
            allowed_issuers=self.allowed_issuers,
            revoked_approvals=self.revoked_approvals,
            manifests=self.manifests,
            strategy_budgets={
                sid: d.order_budget_twd for sid, d in self.exit_definitions.items()
            } | {
                spec.definition.strategy_id: spec.definition.order_budget_twd
                for spec in self.entry_specs
            },
            global_limits=self.global_limits,
            pipeline_order=self.pipeline_order,
            slippage_bps=self.slippage_bps,
            unlimited_open_positions=self.unlimited_open_positions,
        )

    def _persist_next_execution_intents(self, account_id: str, run_id: str, target_execution_date: date) -> None:
        """Dry-run plan all bundles targeting target_execution_date and persist the result
        to order_intents (idempotent). PENDING rows carry the planned quantity; BLOCKED rows
        carry the block reason. Pure read of projection/db — no broker, no fills, no writes
        outside order_intents. The dashboard reads these for the "下次執行" plan.
        """
        bundles = self._find_bundles_for_execution(target_execution_date, account_id)
        cursor = self.db_conn.cursor()
        # Idempotent refresh: re-running run-daily for the same target date rewrites intents.
        cursor.execute(
            "DELETE FROM order_intents WHERE account_id = ? AND target_execution_date = ?",
            (account_id, target_execution_date.isoformat())
        )
        if not bundles:
            self.db_conn.commit()
            return

        context = ExecutionContext(
            run_id=run_id,
            run_type="DAILY_SIMULATION",
            as_of_date=bundles[0].signal_date,
            execution_date=target_execution_date,
            account_id=account_id,
        )
        engine = self._build_engine()
        ordered, _planned_orders, signal_results, _events = engine.plan_bundles(context, bundles)

        for bundle in ordered:
            for sig in bundle.signals:
                result = signal_results.get(sig.signal_id)
                if isinstance(result, list):
                    qty = sum(o["quantity"] for o in result)
                    status, reason = ("PENDING", None) if qty > 0 else ("BLOCKED", "無交易（無持倉/數量為 0）")
                elif isinstance(result, str):
                    qty, status, reason = 0, "BLOCKED", result
                else:
                    qty, status, reason = 0, "BLOCKED", "未規劃"
                cursor.execute(
                    """
                    INSERT INTO order_intents (
                        intent_id, account_id, bundle_id, signal_id, execution_key,
                        symbol, action, quantity, target_execution_date, status, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(execution_key) DO UPDATE SET
                        quantity = excluded.quantity, status = excluded.status,
                        reason = excluded.reason, created_at = excluded.created_at
                    """,
                    (
                        uuid.uuid4().hex,
                        account_id,
                        bundle.bundle_id,
                        sig.signal_id,
                        f"{account_id}-{bundle.bundle_id}-{sig.signal_id}-{target_execution_date.isoformat()}",
                        sig.symbol,
                        sig.action,
                        qty,
                        target_execution_date.isoformat(),
                        status,
                        reason,
                    )
                )
        self.db_conn.commit()

    def update_high_watermarks(self, account_id: str, as_of_date: date) -> None:
        """
        Persist the day's close into position_high_watermarks for every
        risk_exit-managed position (idempotent via PK upsert, §2.2).
        """
        positions = self.projection.get_strategy_positions(account_id, include_long_term=False)
        for (strategy_id, symbol), pos in positions.items():
            if strategy_id == MANUAL_STRATEGY_ID or strategy_id not in self.exit_definitions:
                continue
            bar = self.market_repo.find(symbol, as_of_date)
            if not bar:
                continue
            self.projection.upsert_high_watermark(
                account_id, strategy_id, symbol, as_of_date.isoformat(), bar.close
            )

    def sync_market_data(
        self, sync_date: date, symbol_specs: list, skip_missing_symbols: bool = False
    ) -> bool:
        """同步指定日期的日 K。

        skip_missing_symbols=False（每日流程）：任一檔缺資料即視為整日未完成，
        交由 WAITING_MARKET_DATA 機制處理。
        skip_missing_symbols=True（歷史回補）：個別商品缺資料（如尚未上市的 ETF）
        僅跳過該檔，不放棄整個日期，避免阻斷排在後面的指數同步。

        同日防護：若 sync_date == 今天且現在尚未過 14:00 Taipei，拒絕同步並回傳 False。
        Shioaji 在盤中（09:00–13:30）的 kbars 第一根棒為昨收「參考價棒」，
        aggregator 會把它當作開盤價，造成 open/low 系統性偏低。
        14:00 之後拿到的是完整結算資料（收盤後約 30 min buffer），不含此棒。
        """
        import pytz as _pytz
        _taipei = _pytz.timezone("Asia/Taipei")
        _now_taipei = datetime.now(_taipei)
        if sync_date == _now_taipei.date() and _now_taipei.hour < 14:
            print(
                f"[sync_market_data] 拒絕盤中同步 {sync_date}（現在 {_now_taipei.strftime('%H:%M')} Taipei < 14:00）"
                "：請於收盤後重試以取得完整日 K。"
            )
            return False

        aggregator = DailyBarAggregator()
        validator = MarketBarValidator()

        for raw_spec in symbol_specs:
            spec = _normalize_symbol_spec(raw_spec)
            symbol = spec["code"]
            bars = self.market_provider.fetch_kbars(symbol, sync_date, sync_date)
            if not bars:
                if skip_missing_symbols:
                    print(f"  [skip] {symbol}: no data on {sync_date}")
                    continue
                return False

            # Simple dummy fetch time and checksum
            fetched_at = datetime.now().isoformat()
            raw_payload = "".join(b.model_dump_json() for b in bars)
            checksum = hashlib.sha256(raw_payload.encode()).hexdigest()

            daily_bar = aggregator.aggregate(
                symbol=symbol,
                exchange=spec["exchange"],
                instrument_type=spec["instrument_type"],
                trade_date=sync_date,
                minute_bars=bars,
                source="shioaji",
                source_fetched_at=fetched_at,
                raw_payload_checksum=checksum
            )
            if not daily_bar:
                if skip_missing_symbols:
                    print(f"  [skip] {symbol}: aggregation produced no bar on {sync_date}")
                    continue
                return False

            validator.validate(daily_bar)
            self.market_repo.upsert(daily_bar)
        return True

    def _get_or_create_run(self, run_date: date, account_id: str) -> dict[str, Any]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT run_id, run_date, account_id, strategy_id, status, market_sync_status,
                   execution_status, signal_generation_status, report_status, started_at, completed_at, last_error_code
            FROM daily_runs
            WHERE run_date = ? AND account_id = ? AND strategy_id = ?
            """,
            (run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID)
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
            (run_id, run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID, "STARTED", "PENDING", "PENDING", "PENDING", "PENDING")
        )
        self.db_conn.commit()

        return {
            "run_id": run_id,
            "run_date": run_date.isoformat(),
            "account_id": account_id,
            "strategy_id": ORCHESTRATOR_STRATEGY_ID,
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
            (run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID)
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
                (status, market_sync_status, execution_status, signal_generation_status, report_status, completed_at, last_error, run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID)
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
                (run_id, run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID, status, market_sync_status, execution_status, signal_generation_status, report_status, completed_at, last_error)
            )
        self.db_conn.commit()

    def _update_run_status(self, run_date: date, account_id: str, status: str) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute(
            "UPDATE daily_runs SET status = ? WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
            (status, run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID)
        )
        self.db_conn.commit()

    def _update_run_sub_status(self, run_date: date, account_id: str, column: str, status: str) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute(
            f"UPDATE daily_runs SET {column} = ? WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
            (status, run_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID)
        )
        self.db_conn.commit()

    def _find_bundles_for_execution(self, execution_date: date, account_id: Optional[str] = None) -> list[DailySignalBundle]:
        """Bundles targeting execution_date, in deterministic creation order.
        The engine re-orders them (exits first, pipeline order) before planning.

        account_id filters to global bundles (account_id IS NULL, e.g. shared entry
        signals) plus this account's own private bundles (exit bundles). Passing None
        returns every bundle (legacy / preview callers)."""
        cursor = self.db_conn.cursor()
        if account_id is None:
            cursor.execute(
                """
                SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
                FROM signal_bundles
                WHERE target_execution_date = ?
                ORDER BY bundle_id ASC
                """,
                (execution_date.isoformat(),)
            )
        else:
            cursor.execute(
                """
                SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
                FROM signal_bundles
                WHERE target_execution_date = ? AND (account_id IS NULL OR account_id = ?)
                ORDER BY bundle_id ASC
                """,
                (execution_date.isoformat(), account_id)
            )
        bundles = []
        for r in cursor.fetchall():
            bundle = self._load_bundle_row(r)
            if bundle:
                bundles.append(bundle)
        return bundles

    def _load_bundle_row(self, r) -> Optional[DailySignalBundle]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT signal_id, symbol, action, reference_price, reason_code, user_override, signal_source
            FROM signal_items
            WHERE bundle_id = ?
            """,
            (r["bundle_id"],)
        )
        items = []
        for row in cursor.fetchall():
            if row["user_override"] == "REJECTED":
                continue
            signal_source = row["signal_source"] or "ENTRY"
            # Per-account entry gate: drop ENTRY signals for a strategy retired from
            # this account's pipeline (e.g. PIT-REJECTED pullback leaking into 國泰 via
            # the shared global entry bundle). RISK_EXIT always survives — a retired
            # strategy's existing positions still need their stops (S5: SELL never blocked).
            # self.pipeline_order is already account-filtered by build_pipeline(account_id);
            # empty => preview / execute-pending loader with no pipeline configured, so skip the gate.
            # ponytail: execute-pending's loader (cli/simulation.py) passes no entry_specs,
            # so its pipeline_order is empty and this gate no-ops there; that manual recovery
            # path stays ungated until it forwards the account's entry_specs.
            if (
                signal_source == "ENTRY"
                and self.pipeline_order
                and r["strategy_id"] not in self.pipeline_order
            ):
                continue
            items.append(
                SignalItem(
                    signal_id=row["signal_id"],
                    symbol=row["symbol"],
                    action=row["action"],
                    reference_price=float(row["reference_price"] / 10000.0),
                    reason_code=row["reason_code"],
                    strategy_id=r["strategy_id"],
                    signal_source=signal_source
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
            bundle_id=r["bundle_id"],
            run_id=r["run_id"],
            approval_id=r["approval_id"],
            strategy=strategy_info,
            signal_date=date.fromisoformat(r["signal_date"]),
            target_execution_date=date.fromisoformat(r["target_execution_date"]),
            market_data_cutoff=date.fromisoformat(r["market_data_cutoff"]),
            signals=items
        )

    def _save_bundle(self, bundle: DailySignalBundle, target_execution_date: date, account_id: Optional[str] = None) -> None:
        # account_id=None => global bundle (entry signals, shared across accounts).
        # account_id set => private exit bundle owned by that account.
        cursor = self.db_conn.cursor()
        # Idempotent re-run: a bundle already saved for this id is left untouched
        # (regeneration is deterministic, so the content is identical).
        cursor.execute("SELECT 1 FROM signal_bundles WHERE bundle_id = ?", (bundle.bundle_id,))
        if cursor.fetchone():
            return
        cursor.execute(
            """
            INSERT INTO signal_bundles (
                bundle_id, run_id, approval_id, strategy_id, strategy_version,
                params_hash, signal_date, target_execution_date, market_data_cutoff, created_at, account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
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
                bundle.market_data_cutoff.isoformat(),
                account_id,
            )
        )
        for sig in bundle.signals:
            cursor.execute(
                """
                INSERT INTO signal_items (
                    item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at, signal_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (
                    f"item-{uuid.uuid4().hex[:8]}",
                    bundle.bundle_id,
                    sig.signal_id,
                    sig.symbol,
                    sig.action,
                    int(round(sig.reference_price * 10000)),
                    sig.reason_code,
                    sig.signal_source
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
