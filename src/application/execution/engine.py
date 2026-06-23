from datetime import datetime, date
import sqlite3
import uuid
from typing import Optional
from src.contracts.models import DailySignalBundle, ExecutionContext, StrategyApprovalManifest, SignalItem
from src.approval.validator import ManifestValidator
from src.trading.allocator import MultiStrategyAllocator, MultiPortfolioState, GlobalLimits
from src.broker.fake_broker import FakeBroker
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.market_data.repository import SqliteMarketBarRepository


class TradeExecutionEngine:
    """
    多策略交易執行引擎。

    - manifests 以 strategy_id 為鍵路由驗證（§2.7）；查無/無效授權僅阻擋該策略
      的 BUY 並記錄事件，SELL（含 RISK_EXIT）永不受授權閘門阻擋——授權異常期間
      停損照常執行（失效安全）。
    - 多 bundle 依固定管線順序合併（exit bundle 先），交由 Allocator 做同日
      netting、雙層限額與 T+1 現金規則。
    """

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        market_repo: SqliteMarketBarRepository,
        projection: PortfolioProjection,
        allowed_issuers: list[str],
        revoked_approvals: list[str],
        manifests: Optional[dict[str, StrategyApprovalManifest]] = None,
        strategy_budgets: Optional[dict[str, int]] = None,
        global_limits: Optional[GlobalLimits] = None,
        pipeline_order: Optional[list[str]] = None,
        slippage_bps: int = 10
    ):
        self.db_conn = db_conn
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifests = manifests or {}
        self.strategy_budgets = strategy_budgets or {}
        self.global_limits = global_limits or GlobalLimits()
        self.pipeline_order = pipeline_order or []
        self.slippage_bps = slippage_bps

    def execute_bundle(self, context: ExecutionContext, bundle: DailySignalBundle) -> dict:
        return self.execute_bundles(context, [bundle])

    def plan_bundles(self, context: ExecutionContext, bundles: list[DailySignalBundle]) -> tuple:
        """Pure planning pass (no broker, no DB writes): order bundles, run the BUY gate
        and the MultiStrategyAllocator, and return (ordered, planned_orders, signal_results,
        events). Shared by execute_bundles (real execution) and the run-daily dry-run that
        persists next-execution order_intents — so the persisted plan matches reality.

        signal_results maps signal_id -> list[order] (executable, carries quantity) or a
        block-reason string.
        """
        execution_time = datetime.fromisoformat(f"{context.execution_date.isoformat()}T09:00:00+08:00")
        mode_req = "simulation" if context.run_type == "DAILY_SIMULATION" else "backtest"
        validator = ManifestValidator(self.allowed_issuers, self.revoked_approvals)

        ordered = self._order_bundles(bundles)

        sell_signals: list[SignalItem] = []
        buy_signals: list[SignalItem] = []
        signal_results: dict = {}
        events: list[dict] = []

        for bundle in ordered:
            bundle_sid = bundle.strategy.strategy_id
            sells = [s for s in bundle.signals if s.action.upper() == "SELL"]
            buys = [s for s in bundle.signals if s.action.upper() == "BUY"]

            # SELLs always pass through; attribute to the owning strategy.
            for s in sells:
                if not s.strategy_id:
                    s.strategy_id = bundle_sid
                sell_signals.append(s)

            if not buys:
                continue

            # BUY gate: per-strategy manifest routing.
            block_reason = self._validate_buy_gate(bundle, validator, execution_time, mode_req)
            if block_reason:
                for b in buys:
                    signal_results[b.signal_id] = block_reason
                event_type = block_reason.split(":")[0].strip()
                events.append({
                    "event_type": event_type,
                    "strategy_id": bundle_sid,
                    "symbol": None,
                    "detail": f"bundle {bundle.bundle_id}: {block_reason}"
                })
                continue

            buys.sort(key=lambda s: (-(s.ranking_score if s.ranking_score is not None else 0.0), s.symbol))
            for b in buys:
                if not b.strategy_id:
                    b.strategy_id = bundle_sid
                buy_signals.append(b)

        # Portfolio state: strategy buckets only (no long-term, no MANUAL).
        cash_balance = self.projection.get_cash_balance(context.account_id)
        all_positions = self.projection.get_strategy_positions(context.account_id, include_long_term=False)
        strategy_positions = {
            key: pos["quantity"] for key, pos in all_positions.items()
            if key[0] != MANUAL_STRATEGY_ID
        }

        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT SUM(ABS(amount)) as spent FROM cash_ledger
            WHERE account_id = ? AND event_type = 'BUY_NOTIONAL' AND occurred_at LIKE ?
            """,
            (context.account_id, f"{context.execution_date.isoformat()}%")
        )
        row = cursor.fetchone()
        global_spent = row["spent"] if row["spent"] is not None else 0

        cursor.execute(
            """
            SELECT strategy_id, SUM(CAST(quantity AS REAL) * price / 10000.0) as spent
            FROM fills
            WHERE account_id = ? AND side = 'BUY' AND filled_at LIKE ?
            GROUP BY strategy_id
            """,
            (context.account_id, f"{context.execution_date.isoformat()}%")
        )
        strategy_spent = {r["strategy_id"]: int(r["spent"]) for r in cursor.fetchall() if r["spent"]}

        portfolio_state = MultiPortfolioState(
            available_cash=cash_balance,
            strategy_positions=strategy_positions,
            global_daily_buy_spent=global_spent,
            strategy_daily_buy_spent=strategy_spent
        )

        strategy_limits = {sid: m.limits for sid, m in self.manifests.items()}

        planned_orders, alloc_results, alloc_events = MultiStrategyAllocator.plan(
            sell_signals=sell_signals,
            buy_signals_in_order=buy_signals,
            portfolio=portfolio_state,
            strategy_budgets=self.strategy_budgets,
            strategy_limits=strategy_limits,
            global_limits=self.global_limits,
        )
        signal_results.update(alloc_results)
        events.extend(alloc_events)

        return ordered, planned_orders, signal_results, events

    def execute_bundles(self, context: ExecutionContext, bundles: list[DailySignalBundle]) -> dict:
        ordered, planned_orders, signal_results, events = self.plan_bundles(context, bundles)

        # Execute with FakeBroker
        broker = FakeBroker(self.market_repo)
        fills, status, unfilled_orders = broker.execute_orders(planned_orders, context.execution_date, self.slippage_bps)

        if status == "WAITING_MARKET_DATA":
            return {
                "status": "WAITING_MARKET_DATA",
                "error_code": "WAITING_MARKET_DATA",
                "fills": [],
                "signal_results": signal_results,
                "events": events
            }

        # Apply fills (strategy attribution flows from order -> fill -> lots)
        order_strategy = {}
        order_source = {}
        for o in planned_orders:
            order_strategy[(o["signal_id"], o["symbol"])] = o["strategy_id"]
            order_source[(o["signal_id"], o["symbol"])] = o.get("signal_source", "ENTRY")

        bundle_by_signal = {}
        for bundle in ordered:
            for s in bundle.signals:
                bundle_by_signal[s.signal_id] = bundle.bundle_id

        applied_fills = []
        for fill in fills:
            key = (fill["signal_id"], fill["symbol"])
            fill_payload = {
                "fill_id": fill["fill_id"],
                "account_id": context.account_id,
                "run_id": context.run_id,
                "order_id": f"ord-{uuid_like()}",
                "execution_key": f"{context.account_id}-{bundle_by_signal.get(fill['signal_id'], 'na')}-{fill['signal_id']}-{context.execution_date.isoformat()}",
                "symbol": fill["symbol"],
                "side": fill["side"],
                "quantity": fill["quantity"],
                "price": fill["price"],
                "filled_at": fill["filled_at"],
                "strategy_id": order_strategy.get(key, MANUAL_STRATEGY_ID)
            }
            try:
                self.projection.apply_fill_transaction(fill_payload)
                applied_fills.append(fill)
            except ValueError as e:
                err_str = str(e)
                if err_str.startswith("LONG_TERM_PROTECTED"):
                    print(f"[engine] SKIP SELL {fill['symbol']}: {err_str}")
                else:
                    raise

        self._record_events(context, events)

        return {
            "status": "COMPLETED",
            "fills": applied_fills,
            "signal_results": signal_results,
            "events": events,
            "unfilled_orders": unfilled_orders
        }

    def _order_bundles(self, bundles: list[DailySignalBundle]) -> list[DailySignalBundle]:
        """Deterministic order: exit bundles first, then entries, each by pipeline order."""
        def is_exit(b: DailySignalBundle) -> bool:
            return b.bundle_id.endswith("-exit") or (
                bool(b.signals) and all(s.signal_source == "RISK_EXIT" for s in b.signals)
            )
        def pipeline_idx(b: DailySignalBundle) -> int:
            sid = b.strategy.strategy_id
            return self.pipeline_order.index(sid) if sid in self.pipeline_order else len(self.pipeline_order)
        return sorted(bundles, key=lambda b: (0 if is_exit(b) else 1, pipeline_idx(b), b.strategy.strategy_id))

    def _validate_buy_gate(self, bundle, validator, execution_time, mode_req) -> Optional[str]:
        """Return a block reason for the bundle's BUY signals, or None if approved."""
        sid = bundle.strategy.strategy_id
        manifest = self.manifests.get(sid)
        if manifest is None:
            return f"APPROVAL_NOT_FOUND: 策略 {sid} 查無有效授權"
        try:
            validator.validate(manifest, execution_time, mode_req)
        except ValueError as e:
            return f"APPROVAL_INVALID: {e}"
        if bundle.approval_id != manifest.approval_id:
            return (
                f"APPROVAL_ID_MISMATCH: bundle approval_id ({bundle.approval_id}) "
                f"!= manifest approval_id ({manifest.approval_id})"
            )
        if bundle.strategy.params_hash != manifest.strategy.params_hash:
            return (
                f"PARAMS_HASH_MISMATCH: bundle params_hash ({bundle.strategy.params_hash}) "
                f"!= manifest params_hash ({manifest.strategy.params_hash})"
            )
        return None

    def _record_events(self, context: ExecutionContext, events: list[dict]) -> None:
        if not events:
            return
        cursor = self.db_conn.cursor()
        for ev in events:
            cursor.execute(
                """
                INSERT INTO execution_events (
                    event_id, run_id, account_id, event_type, strategy_id, symbol, detail, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    f"evt-{uuid_like()}",
                    context.run_id,
                    context.account_id,
                    ev.get("event_type"),
                    ev.get("strategy_id"),
                    ev.get("symbol"),
                    ev.get("detail"),
                    context.execution_date.isoformat()
                )
            )
        self.db_conn.commit()


def uuid_like() -> str:
    return uuid.uuid4().hex[:8]
