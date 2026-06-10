from datetime import datetime, date
import sqlite3
from typing import Optional
from src.contracts.models import DailySignalBundle, ExecutionContext, StrategyApprovalManifest
from src.approval.validator import ManifestValidator
from src.trading.planner import OrderPlanner, PortfolioState
from src.broker.fake_broker import FakeBroker
from src.portfolio.projection import PortfolioProjection
from src.market_data.repository import SqliteMarketBarRepository

class TradeExecutionEngine:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        market_repo: SqliteMarketBarRepository,
        projection: PortfolioProjection,
        allowed_issuers: list[str],
        revoked_approvals: list[str],
        manifest: StrategyApprovalManifest,
        strategy_budget: int,
        slippage_bps: int = 10
    ):
        self.db_conn = db_conn
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifest = manifest
        self.strategy_budget = strategy_budget
        self.slippage_bps = slippage_bps

    def execute_bundle(self, context: ExecutionContext, bundle: DailySignalBundle) -> dict:
        cursor = self.db_conn.cursor()
        
        # 1. Validate Strategy Approval Manifest
        execution_time = datetime.fromisoformat(f"{context.execution_date.isoformat()}T09:00:00+08:00")
        validator = ManifestValidator(self.allowed_issuers, self.revoked_approvals)
        
        # Execution modes in manifest are lowercase/uppercase compatible
        mode_req = "simulation" if context.run_type == "DAILY_SIMULATION" else "backtest"
        validator.validate(self.manifest, execution_time, mode_req)
        
        # Verify bundle matches manifest bindings
        if bundle.approval_id != self.manifest.approval_id:
            raise ValueError(
                f"APPROVAL_ID_MISMATCH: Bundle approval_id ({bundle.approval_id}) "
                f"does not match Manifest approval_id ({self.manifest.approval_id})"
            )
            
        if bundle.strategy.params_hash != self.manifest.strategy.params_hash:
            raise ValueError(
                f"PARAMS_HASH_MISMATCH: Bundle strategy.params_hash ({bundle.strategy.params_hash}) "
                f"does not match Manifest params_hash ({self.manifest.strategy.params_hash})"
            )
            
        # 2. Gather Portfolio State
        cash_balance = self.projection.get_cash_balance(context.account_id)
        
        cursor.execute(
            """
            SELECT symbol, SUM(quantity) as total_qty FROM position_lots
            WHERE account_id = ?
            GROUP BY symbol
            """,
            (context.account_id,)
        )
        positions = {row["symbol"]: row["total_qty"] for row in cursor.fetchall() if row["total_qty"] > 0}
        
        cursor.execute(
            """
            SELECT SUM(ABS(amount)) as spent FROM cash_ledger
            WHERE account_id = ? AND event_type = 'BUY_NOTIONAL' AND occurred_at LIKE ?
            """,
            (context.account_id, f"{context.execution_date.isoformat()}%")
        )
        row = cursor.fetchone()
        daily_buy_value_spent = row["spent"] if row["spent"] is not None else 0
        
        portfolio_state = PortfolioState(
            available_cash=cash_balance,
            positions=positions,
            daily_buy_value_spent=daily_buy_value_spent
        )
        
        # 3. Plan Orders
        planned_orders = []
        for signal in bundle.signals:
            orders = OrderPlanner.plan_order(
                signal=signal,
                portfolio=portfolio_state,
                strategy_budget=self.strategy_budget,
                manifest_limits=self.manifest.limits
            )
            planned_orders.extend(orders)
            
        # 4. Execute with FakeBroker
        broker = FakeBroker(self.market_repo)
        fills, status = broker.execute_orders(planned_orders, context.execution_date, self.slippage_bps)
        
        if status == "WAITING_MARKET_DATA":
            return {
                "status": "WAITING_MARKET_DATA",
                "error_code": "WAITING_MARKET_DATA",
                "fills": []
            }
            
        # 5. Apply Fills to Portfolio Projection
        for fill in fills:
            # Add account_id, run_id and execution_key which are needed for fill entity
            fill_payload = {
                "fill_id": fill["fill_id"],
                "account_id": context.account_id,
                "run_id": context.run_id,
                "order_id": f"ord-{uuid_like()}",
                "execution_key": f"{context.account_id}-{bundle.bundle_id}-{fill['signal_id']}-{context.execution_date.isoformat()}",
                "symbol": fill["symbol"],
                "side": fill["side"],
                "quantity": fill["quantity"],
                "price": fill["price"],
                "filled_at": fill["filled_at"]
            }
            self.projection.apply_fill_transaction(fill_payload)
            
        return {
            "status": "COMPLETED",
            "fills": fills
        }

def uuid_like() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
