import sqlite3
import uuid
from datetime import datetime

# Strategy bucket for manually recorded fills (record-fill); excluded from risk_exit monitoring.
MANUAL_STRATEGY_ID = "MANUAL"

class PortfolioProjection:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_cash_balance(self, account_id: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT balance FROM cash_balances WHERE account_id = ?", (account_id,))
        row = cursor.fetchone()
        return row["balance"] if row else 0

    def get_position_lots(self, account_id: str, symbol: str) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT lot_id, symbol, quantity, price, acquired_at, fill_id, is_long_term, strategy_id
            FROM position_lots
            WHERE account_id = ? AND symbol = ?
            ORDER BY acquired_at ASC
            """,
            (account_id, symbol)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_realized_pnl(self, account_id: str) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT pnl_id, symbol, realized_amount, tax_amount, fee_amount, occurred_at
            FROM realized_pnl
            WHERE account_id = ?
            ORDER BY occurred_at ASC
            """,
            (account_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def apply_fill_transaction(self, fill: dict) -> None:
        with self.conn:
            cursor = self.conn.cursor()
            self._apply_fill_ops(cursor, fill)

    def rebuild_from_ledger(self, account_id: str) -> None:
        with self.conn:
            cursor = self.conn.cursor()
            
            # Fetch all fills for this account chronologically first
            cursor.execute(
                """
                SELECT fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, is_long_term, source, strategy_id
                FROM fills
                WHERE account_id = ?
                ORDER BY filled_at ASC
                """,
                (account_id,)
            )
            fills_rows = cursor.fetchall()
            
            # Fetch INITIAL_DEPOSIT amount
            cursor.execute(
                """
                SELECT SUM(amount) as initial_cash FROM cash_ledger
                WHERE account_id = ? AND event_type = 'INITIAL_DEPOSIT'
                """,
                (account_id,)
            )
            initial_row = cursor.fetchone()
            initial_cash = initial_row["initial_cash"] if initial_row["initial_cash"] is not None else 0
            
            # Clear all projection tables and fills/ledger for FILL source
            cursor.execute("DELETE FROM position_lots WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM fifo_matches WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM realized_pnl WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM cash_balances WHERE account_id = ?", (account_id,))
            cursor.execute("DELETE FROM cash_ledger WHERE account_id = ? AND source_type = 'FILL'", (account_id,))
            cursor.execute("DELETE FROM fills WHERE account_id = ?", (account_id,))
            
            # Re-write initial balance
            cursor.execute(
                """
                INSERT INTO cash_balances (account_id, balance, currency, updated_at)
                VALUES (?, ?, 'TWD', datetime('now'))
                """,
                (account_id, initial_cash)
            )
            
            # Re-apply each fill
            for f in fills_rows:
                fill_dict = {
                    "fill_id": f["fill_id"],
                    "account_id": f["account_id"],
                    "run_id": f["run_id"],
                    "order_id": f["order_id"],
                    "execution_key": f["execution_key"],
                    "symbol": f["symbol"],
                    "side": f["side"],
                    "quantity": f["quantity"],
                    "price": f["price"],
                    "filled_at": f["filled_at"],
                    "is_long_term": f["is_long_term"],
                    "source": f["source"],
                    "strategy_id": f["strategy_id"]
                }
                self._apply_fill_ops(cursor, fill_dict)

    def reconcile(self, account_id: str) -> dict:
        cursor = self.conn.cursor()
        
        # 1. Cash Balance check
        cursor.execute("SELECT SUM(amount) as ledger_total FROM cash_ledger WHERE account_id = ?", (account_id,))
        ledger_total = cursor.fetchone()["ledger_total"]
        if ledger_total is None:
            ledger_total = 0
            
        cursor.execute("SELECT balance FROM cash_balances WHERE account_id = ?", (account_id,))
        balance_row = cursor.fetchone()
        balance_snapshot = balance_row["balance"] if balance_row else 0
        
        if ledger_total != balance_snapshot:
            return {
                "status": "CASH_BALANCE_MISMATCH",
                "ledger_total": ledger_total,
                "balance_snapshot": balance_snapshot
            }
            
        # 2. Position Lots check
        # Get net fill quantities (grouped by symbol)
        cursor.execute(
            """
            SELECT symbol, SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) as net_qty
            FROM fills
            WHERE account_id = ?
            GROUP BY symbol
            """,
            (account_id,)
        )
        fill_net = {r["symbol"]: r["net_qty"] for r in cursor.fetchall()}
        
        # Get current active lot quantities (grouped by symbol)
        cursor.execute(
            """
            SELECT symbol, SUM(quantity) as lot_qty
            FROM position_lots
            WHERE account_id = ?
            GROUP BY symbol
            """,
            (account_id,)
        )
        lot_qty = {r["symbol"]: r["lot_qty"] for r in cursor.fetchall()}
        
        # Compare
        all_symbols = set(fill_net.keys()).union(lot_qty.keys())
        for symbol in all_symbols:
            expected = fill_net.get(symbol, 0)
            actual = lot_qty.get(symbol, 0)
            if expected != actual:
                return {
                    "status": "POSITION_QUANTITY_MISMATCH",
                    "symbol": symbol,
                    "expected": expected,
                    "actual": actual
                }

        # 3. Per-strategy bucket check (FIFO isolation integrity)
        cursor.execute(
            """
            SELECT symbol, strategy_id, SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) as net_qty
            FROM fills
            WHERE account_id = ?
            GROUP BY symbol, strategy_id
            """,
            (account_id,)
        )
        fill_bucket_net = {(r["symbol"], r["strategy_id"]): r["net_qty"] for r in cursor.fetchall()}

        cursor.execute(
            """
            SELECT symbol, strategy_id, SUM(quantity) as lot_qty
            FROM position_lots
            WHERE account_id = ?
            GROUP BY symbol, strategy_id
            """,
            (account_id,)
        )
        lot_bucket_qty = {(r["symbol"], r["strategy_id"]): r["lot_qty"] for r in cursor.fetchall()}

        all_buckets = set(fill_bucket_net.keys()).union(lot_bucket_qty.keys())
        for bucket in all_buckets:
            expected = fill_bucket_net.get(bucket, 0)
            actual = lot_bucket_qty.get(bucket, 0)
            if expected != actual:
                return {
                    "status": "STRATEGY_POSITION_MISMATCH",
                    "symbol": bucket[0],
                    "strategy_id": bucket[1],
                    "expected": expected,
                    "actual": actual
                }

        return {"status": "RECONCILE_OK"}

    def get_strategy_positions(self, account_id: str, include_long_term: bool = False) -> dict:
        """
        Aggregate open lots into per-(strategy_id, symbol) positions with
        quantity-weighted average entry price and first acquisition timestamp.
        """
        cursor = self.conn.cursor()
        long_term_filter = "" if include_long_term else "AND is_long_term = 0"
        cursor.execute(
            f"""
            SELECT strategy_id, symbol,
                   SUM(quantity) as qty,
                   CAST(SUM(CAST(quantity AS REAL) * price) / SUM(quantity) AS INTEGER) as wavg_price,
                   MIN(acquired_at) as first_acquired_at,
                   MAX(is_long_term) as is_long_term
            FROM position_lots
            WHERE account_id = ? {long_term_filter}
            GROUP BY strategy_id, symbol
            HAVING SUM(quantity) > 0
            """,
            (account_id,)
        )
        return {
            (row["strategy_id"], row["symbol"]): {
                "strategy_id": row["strategy_id"],
                "symbol": row["symbol"],
                "quantity": row["qty"],
                "wavg_price": row["wavg_price"],
                "first_acquired_at": row["first_acquired_at"],
                "is_long_term": bool(row["is_long_term"])
            }
            for row in cursor.fetchall()
        }

    def upsert_high_watermark(self, account_id: str, strategy_id: str, symbol: str, trade_date: str, close_price: int) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO position_high_watermarks (account_id, strategy_id, symbol, trade_date, highest_close, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account_id, strategy_id, symbol, trade_date) DO UPDATE SET
                highest_close = excluded.highest_close
            """,
            (account_id, strategy_id, symbol, trade_date, close_price)
        )
        self.conn.commit()

    def get_position_high(self, account_id: str, strategy_id: str, symbol: str, since_date: str) -> int | None:
        """
        Highest recorded close for the position window starting at the first
        acquisition date of the currently open lots (since_date, 'YYYY-MM-DD').
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT MAX(highest_close) as high FROM position_high_watermarks
            WHERE account_id = ? AND strategy_id = ? AND symbol = ? AND trade_date >= ?
            """,
            (account_id, strategy_id, symbol, since_date)
        )
        row = cursor.fetchone()
        return row["high"] if row and row["high"] is not None else None

    def _apply_fill_ops(self, cursor, fill: dict) -> None:
        fill_id = fill["fill_id"]
        account_id = fill["account_id"]
        run_id = fill["run_id"]
        order_id = fill["order_id"]
        execution_key = fill["execution_key"]
        symbol = fill["symbol"]
        side = fill["side"].upper()
        quantity = fill["quantity"]
        price = fill["price"]  # price x 10000
        filled_at = fill["filled_at"]

        trade_value = int(round(quantity * price / 10000.0))
        broker_fee = max(20, int(round(trade_value * 0.001425)))
        tax = int(round(trade_value * 0.003)) if side == "SELL" else 0

        # 1. Insert fill
        is_long_term = fill.get("is_long_term", 0)
        source = fill.get("source", "STRATEGY")
        strategy_id = fill.get("strategy_id")
        if not strategy_id:
            raise ValueError(
                f"FILL_MISSING_STRATEGY_ID: fill {fill_id} for {symbol} has no strategy_id; "
                f"run scripts/migrate_multi_strategy.py to backfill legacy facts."
            )
        cursor.execute(
            """
            INSERT INTO fills (
                fill_id, account_id, run_id, order_id, execution_key,
                symbol, side, quantity, price, filled_at, reverses_fill_id, created_at, is_long_term, source, strategy_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, datetime('now'), ?, ?, ?)
            """,
            (fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, is_long_term, source, strategy_id)
        )

        # 2. Append cash ledger events
        notional_id = f"led-{uuid.uuid4().hex[:8]}"
        event_type = "BUY_NOTIONAL" if side == "BUY" else "SELL_PROCEEDS"
        notional_amount = -trade_value if side == "BUY" else trade_value
        cursor.execute(
            """
            INSERT INTO cash_ledger (
                ledger_id, account_id, run_id, event_type, amount, currency,
                source_type, source_id, occurred_at, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, 'TWD', 'FILL', ?, ?, ?, datetime('now'))
            """,
            (notional_id, account_id, run_id, event_type, notional_amount, fill_id, filled_at, f"not-{fill_id}")
        )
        
        fee_id = f"led-{uuid.uuid4().hex[:8]}"
        cursor.execute(
            """
            INSERT INTO cash_ledger (
                ledger_id, account_id, run_id, event_type, amount, currency,
                source_type, source_id, occurred_at, idempotency_key, created_at
            ) VALUES (?, ?, ?, 'BROKER_FEE', ?, 'TWD', 'FILL', ?, ?, ?, datetime('now'))
            """,
            (fee_id, account_id, run_id, -broker_fee, fill_id, filled_at, f"fee-{fill_id}")
        )

        if tax > 0:
            tax_id = f"led-{uuid.uuid4().hex[:8]}"
            cursor.execute(
                """
                INSERT INTO cash_ledger (
                    ledger_id, account_id, run_id, event_type, amount, currency,
                    source_type, source_id, occurred_at, idempotency_key, created_at
                ) VALUES (?, ?, ?, 'TRANSACTION_TAX', ?, 'TWD', 'FILL', ?, ?, ?, datetime('now'))
                """,
                (tax_id, account_id, run_id, -tax, fill_id, filled_at, f"tax-{fill_id}")
            )

        # 3. Apply position lot and cash projections
        cursor.execute("SELECT balance FROM cash_balances WHERE account_id = ?", (account_id,))
        row = cursor.fetchone()
        current_balance = row["balance"] if row else 0

        if side == "BUY":
            lot_id = f"lot-{uuid.uuid4().hex[:8]}"
            cursor.execute(
                """
                INSERT INTO position_lots (
                    lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at, is_long_term, strategy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                (lot_id, account_id, symbol, quantity, price, filled_at, fill_id, is_long_term, strategy_id)
            )
            new_balance = current_balance - (trade_value + broker_fee)

        else:  # SELL
            # FIFO deduction is isolated per strategy bucket so concurrent
            # strategies holding the same symbol never consume each other's lots.
            cursor.execute(
                """
                SELECT lot_id, quantity, price, fill_id FROM position_lots
                WHERE account_id = ? AND symbol = ? AND strategy_id = ? AND is_long_term = ?
                ORDER BY acquired_at ASC
                """,
                (account_id, symbol, strategy_id, is_long_term)
            )
            lots = cursor.fetchall()
            
            remaining_sell = quantity
            total_realized_pnl = 0
            
            for lot in lots:
                if remaining_sell <= 0:
                    break
                    
                lot_id = lot["lot_id"]
                lot_qty = lot["quantity"]
                buy_price = lot["price"]
                buy_fill_id = lot["fill_id"]
                
                if lot_qty > remaining_sell:
                    matched_qty = remaining_sell
                    new_lot_qty = lot_qty - remaining_sell
                    cursor.execute(
                        "UPDATE position_lots SET quantity = ? WHERE lot_id = ?",
                        (new_lot_qty, lot_id)
                    )
                    remaining_sell = 0
                else:
                    matched_qty = lot_qty
                    cursor.execute("DELETE FROM position_lots WHERE lot_id = ?", (lot_id,))
                    remaining_sell -= lot_qty
                    
                match_pnl = int((price - buy_price) * matched_qty // 10000)
                total_realized_pnl += match_pnl
                
                match_id = f"mat-{uuid.uuid4().hex[:8]}"
                cursor.execute(
                    """
                    INSERT INTO fifo_matches (
                        match_id, account_id, symbol, buy_fill_id, sell_fill_id,
                        quantity, buy_price, sell_price, matched_at, realized_pnl, created_at, strategy_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                    """,
                    (match_id, account_id, symbol, buy_fill_id, fill_id, matched_qty, buy_price, price, filled_at, match_pnl, strategy_id)
                )
            
            if remaining_sell > 0:
                matched = quantity - remaining_sell
                if matched == 0:
                    # Check if this symbol has long-term lots that blocked the sale
                    cursor.execute(
                        "SELECT COUNT(*) as cnt FROM position_lots WHERE account_id = ? AND symbol = ? AND is_long_term = 1",
                        (account_id, symbol)
                    )
                    lt_count = cursor.fetchone()["cnt"]
                    if lt_count > 0:
                        raise ValueError(
                            f"LONG_TERM_PROTECTED: SELL {quantity} shares of {symbol} blocked — "
                            f"position is marked as long-term and cannot be automatically sold."
                        )
                raise ValueError(
                    f"SELL_WITHOUT_POSITION: Attempted to sell {quantity} shares of {symbol} "
                    f"(strategy {strategy_id}), but only matched {matched}"
                )


            new_balance = current_balance + (trade_value - broker_fee - tax)

            pnl_id = f"pnl-{uuid.uuid4().hex[:8]}"
            cursor.execute(
                """
                INSERT INTO realized_pnl (
                    pnl_id, account_id, run_id, symbol, realized_amount, tax_amount, fee_amount, occurred_at, created_at, strategy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (pnl_id, account_id, run_id, symbol, total_realized_pnl, tax, broker_fee, filled_at, strategy_id)
            )

        # Update cash balance
        cursor.execute(
            """
            INSERT INTO cash_balances (account_id, balance, currency, updated_at)
            VALUES (?, ?, 'TWD', datetime('now'))
            ON CONFLICT(account_id) DO UPDATE SET balance = excluded.balance, updated_at = datetime('now')
            """,
            (account_id, new_balance)
        )
