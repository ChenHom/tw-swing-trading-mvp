import uuid
import sqlite3
import math
from datetime import date, datetime
from typing import Optional
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

class BacktestRunner:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        calendar: TradingCalendar,
        market_repo: SqliteMarketBarRepository,
        projection: PortfolioProjection,
        allowed_issuers: list[str],
        revoked_approvals: list[str],
        manifest: StrategyApprovalManifest,
        strategy_budget: int,
        slippage_bps: int = 10
    ):
        self.db_conn = db_conn
        self.calendar = calendar
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifest = manifest
        self.strategy_budget = strategy_budget
        self.slippage_bps = slippage_bps

    def run(
        self,
        start_date: date,
        end_date: date,
        initial_cash: int,
        universe_symbols: list[str],
        strategy_params: TrendPullbackParams
    ) -> dict:
        run_id = f"bt-{uuid.uuid4().hex[:8]}"
        account_id = f"backtest:{run_id}"
        
        # 1. Initialize cash deposit
        ledger = PortfolioLedger(self.db_conn)
        ledger.deposit(account_id, run_id, initial_cash, "TWD", start_date)
        self.projection.rebuild_from_ledger(account_id)
        
        sessions = self.calendar.sessions_between(start_date, end_date)
        if not sessions:
            raise ValueError(f"No trading days found between {start_date} and {end_date}")
            
        strategy = TrendPullbackStrategy(
            params=strategy_params,
            universe_symbols=universe_symbols
        )
        
        equity_curve = []
        
        for D in sessions:
            # Validate D's market bar exists (dataset completeness rule)
            for symbol in universe_symbols:
                bar = self.market_repo.find(symbol, D)
                if not bar:
                    raise ValueError(f"DATASET_INCOMPLETE: Missing market bar for {symbol} on {D}")
            
            # A. Execute any PENDING bundle target_execution_date == D
            bundle = self._find_bundle_by_execution_date(account_id, D)
            if bundle:
                context = ExecutionContext(
                    run_id=run_id,
                    run_type="BACKTEST",
                    as_of_date=bundle.signal_date,
                    execution_date=D,
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
                engine.execute_bundle(context, bundle)
                
            # B. Generate new signals as of D close
            pit_data = self.market_repo.as_of(D)
            
            # Fetch current portfolio state for strategy generate call
            available_cash = self.projection.get_cash_balance(account_id)
            positions = {}
            cursor = self.db_conn.cursor()
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
            portfolio_snapshot = PortfolioSnapshot(
                available_cash=available_cash,
                positions=positions
            )
            
            sig_ctx = SignalGenerationContext(
                as_of_date=D,
                strategy_id=self.manifest.strategy.strategy_id,
                strategy_version=self.manifest.strategy.strategy_version,
                run_id=run_id,
                approval_id=self.manifest.approval_id,
                params_hash=self.manifest.strategy.params_hash
            )
            
            new_bundle = strategy.generate(sig_ctx, pit_data, portfolio_snapshot)
            
            # Calculate target execution date (next trading day)
            target_execution_date = self.calendar.next_trading_day(D)
            
            # Save the new bundle to database
            self._save_bundle(new_bundle, target_execution_date)
            
            # C. Record Equity for session D
            # Equity = cash + sum(qty * D_close)
            pos_value = 0
            for symbol, pos in positions.items():
                bar = self.market_repo.find(symbol, D)
                assert bar is not None
                pos_value += int(pos.quantity * bar.close // 10000)
            equity_curve.append({
                "date": D,
                "cash": available_cash,
                "position_value": pos_value,
                "equity": available_cash + pos_value
            })
            
        # 3. Calculate Backtest Statistics
        stats = self._calculate_statistics(account_id, initial_cash, equity_curve)
        return {
            "run_id": run_id,
            "account_id": account_id,
            "equity_curve": equity_curve,
            "statistics": stats
        }

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

    def _find_bundle_by_execution_date(self, account_id: str, execution_date: date) -> Optional[DailySignalBundle]:
        cursor = self.db_conn.cursor()
        # Parse run_id from account_id (e.g. backtest:bt-xxxx -> bt-xxxx)
        run_id = account_id.split(":")[-1]
        
        cursor.execute(
            """
            SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
            FROM signal_bundles
            WHERE target_execution_date = ? AND run_id = ?
            """,
            (execution_date.isoformat(), run_id)
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

    def _calculate_statistics(self, account_id: str, initial_cash: int, equity_curve: list[dict]) -> dict:
        cursor = self.db_conn.cursor()
        
        cursor.execute(
            """
            SELECT realized_pnl FROM fifo_matches
            WHERE account_id = ?
            """,
            (account_id,)
        )
        matches = [r["realized_pnl"] for r in cursor.fetchall()]
        
        trade_count = len(matches)
        win_count = sum(1 for p in matches if p > 0)
        loss_count = sum(1 for p in matches if p < 0)
        win_rate = win_count / trade_count if trade_count > 0 else 0.0
        
        total_profit = sum(p for p in matches if p > 0)
        total_loss = abs(sum(p for p in matches if p < 0))
        
        profit_factor = total_profit / total_loss if total_loss > 0 else (float('inf') if total_profit > 0 else 0.0)
        
        avg_profit = total_profit / win_count if win_count > 0 else 0.0
        avg_loss = total_loss / loss_count if loss_count > 0 else 0.0
        
        # Drawdown calculation
        peak = -1
        max_dd = 0.0
        for eq_point in equity_curve:
            eq = eq_point["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                
        final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
        total_pnl = final_equity - initial_cash
        total_pnl_bps = int(round(total_pnl / initial_cash * 10000)) if initial_cash > 0 else 0
        
        return {
            "initial_cash": initial_cash,
            "final_equity": final_equity,
            "total_pnl": total_pnl,
            "total_pnl_bps": total_pnl_bps,
            "max_drawdown": max_dd,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_profit": avg_profit,
            "avg_loss": avg_loss,
            "trade_count": trade_count
        }
