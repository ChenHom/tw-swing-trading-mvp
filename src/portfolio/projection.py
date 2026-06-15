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

    def apply_corporate_action(self, account_id: str, action: dict) -> None:
        """套用公司行動調整（除息、配股、分割等）。

        Args:
            account_id: 帳戶 ID
            action: {
                "action_id": "uuid",
                "symbol": "2330",
                "action_type": "CASH_DIVIDEND" | "STOCK_DIVIDEND",
                "cash_per_share": 100000 (for CASH_DIVIDEND, 整數×10000),
                "stock_ratio": 0.1 (for STOCK_DIVIDEND),
                "ex_date": "2026-06-20"
            }

        冪等性：同 action_id 套用多次只套用一次。
        """
        with self.conn:
            cursor = self.conn.cursor()

            # 1. 檢查是否已套用（冪等）
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM position_cost_adjustments WHERE action_id = ?",
                (action["action_id"],)
            )
            if cursor.fetchone()["cnt"] > 0:
                return  # 已套用過，直接返回

            # 2. 寫 corporate_actions 事實表
            cursor.execute(
                """
                INSERT INTO corporate_actions
                (action_id, symbol, action_type, ex_date, cash_per_share, stock_ratio, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'MANUAL', datetime('now'))
                """,
                (
                    action["action_id"],
                    action["symbol"],
                    action["action_type"],
                    action["ex_date"],
                    action.get("cash_per_share"),
                    action.get("stock_ratio"),
                )
            )

            # 3. 根據 action_type 調整
            if action["action_type"] == "CASH_DIVIDEND":
                self._apply_cash_dividend(cursor, account_id, action)
            elif action["action_type"] == "STOCK_DIVIDEND":
                self._apply_stock_dividend(cursor, account_id, action)

    def _apply_cash_dividend(self, cursor, account_id: str, action: dict) -> None:
        """套用現金股利：price -= cash_per_share，watermark -= cash_per_share，現金 += qty * cash_per_share。"""
        symbol = action["symbol"]
        cash_per_share = action["cash_per_share"]  # 整數×10000
        action_id = action["action_id"]

        # 查詢該帳戶該標的所有持倉 lots
        cursor.execute(
            "SELECT lot_id, quantity, price, strategy_id FROM position_lots WHERE account_id = ? AND symbol = ?",
            (account_id, symbol)
        )
        lots = [dict(row) for row in cursor.fetchall()]

        for lot in lots:
            lot_id = lot["lot_id"]
            qty = lot["quantity"]
            old_price = lot["price"]
            new_price = old_price - cash_per_share
            strategy_id = lot["strategy_id"]

            # 3a. 更新 position_lots.price
            cursor.execute(
                "UPDATE position_lots SET price = ? WHERE lot_id = ?",
                (new_price, lot_id)
            )

            # 記錄調整
            cursor.execute(
                """
                INSERT INTO position_cost_adjustments
                (adjustment_id, action_id, account_id, strategy_id, symbol, lot_id, field,
                 before_value, after_value, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'LOT_PRICE', ?, ?, datetime('now'))
                """,
                (uuid.uuid4().hex, action_id, account_id, strategy_id, symbol, lot_id, old_price, new_price)
            )

        # 3b. 更新 position_high_watermarks（該 symbol 所有記錄）
        cursor.execute(
            """
            SELECT account_id, strategy_id, symbol, trade_date, highest_close
            FROM position_high_watermarks
            WHERE account_id = ? AND symbol = ?
            """,
            (account_id, symbol)
        )
        watermarks = [dict(row) for row in cursor.fetchall()]

        for wm in watermarks:
            old_close = wm["highest_close"]
            new_close = old_close - cash_per_share
            cursor.execute(
                """
                UPDATE position_high_watermarks
                SET highest_close = ?
                WHERE account_id = ? AND strategy_id = ? AND symbol = ? AND trade_date = ?
                """,
                (new_close, wm["account_id"], wm["strategy_id"], wm["symbol"], wm["trade_date"])
            )
            cursor.execute(
                """
                INSERT INTO position_cost_adjustments
                (adjustment_id, action_id, account_id, strategy_id, symbol, field,
                 before_value, after_value, created_at)
                VALUES (?, ?, ?, ?, ?, 'WATERMARK', ?, ?, datetime('now'))
                """,
                (
                    uuid.uuid4().hex,
                    action_id,
                    wm["account_id"],
                    wm["strategy_id"],
                    symbol,
                    old_close,
                    new_close,
                )
            )

        # 3c. 新增現金分錄（配息入帳）+ 同步餘額快照
        # 單位換算：quantity=股、cash_per_share=元×10000、cash_ledger/cash_balances=整數元。
        # 配息(元) = Σ(股數 × 每股股利×10000) ÷ 10000。
        if lots:
            total_dividend = sum(lot["quantity"] * cash_per_share for lot in lots) // 10000
            if total_dividend > 0:
                ledger_id = uuid.uuid4().hex
                cursor.execute(
                    """
                    INSERT INTO cash_ledger
                    (ledger_id, account_id, run_id, event_type, amount, currency,
                     source_type, source_id, occurred_at, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        ledger_id,
                        account_id,
                        "CORP_ACTION",
                        "DIVIDEND",
                        total_dividend,
                        "TWD",
                        "CORPORATE_ACTION",
                        action_id,
                        action["ex_date"],
                        f"{action_id}:{symbol}:{account_id}",
                    )
                )

                # 同步更新 cash_balances，使 reconcile 第一關 (SUM(ledger)==balance) 維持平衡
                cursor.execute(
                    """
                    INSERT INTO cash_balances (account_id, balance, currency, updated_at)
                    VALUES (?, ?, 'TWD', datetime('now'))
                    ON CONFLICT(account_id) DO UPDATE SET
                        balance = balance + ?, updated_at = datetime('now')
                    """,
                    (account_id, total_dividend, total_dividend)
                )

                # 記錄現金調整稽核
                cursor.execute(
                    """
                    INSERT INTO position_cost_adjustments
                    (adjustment_id, action_id, account_id, strategy_id, symbol, field,
                     before_value, after_value, created_at)
                    VALUES (?, ?, ?, '', ?, 'CASH', 0, ?, datetime('now'))
                    """,
                    (uuid.uuid4().hex, action_id, account_id, symbol, total_dividend)
                )

    def _apply_stock_dividend(self, cursor, account_id: str, action: dict) -> None:
        """套用股票股利：price /= (1+ratio)，qty *= (1+ratio)，watermark /= (1+ratio)。"""
        symbol = action["symbol"]
        ratio = action["stock_ratio"]  # 例 0.1
        action_id = action["action_id"]
        factor = 1 + ratio

        # 查詢該帳戶該標的所有持倉 lots
        cursor.execute(
            "SELECT lot_id, quantity, price, strategy_id, is_long_term FROM position_lots WHERE account_id = ? AND symbol = ?",
            (account_id, symbol)
        )
        lots = [dict(row) for row in cursor.fetchall()]

        for lot in lots:
            lot_id = lot["lot_id"]
            old_qty = lot["quantity"]
            old_price = lot["price"]
            new_price = int(old_price / factor)
            new_qty = int(old_qty * factor)
            strategy_id = lot["strategy_id"]
            is_long_term = lot["is_long_term"]

            # 3a. 新增無償配股 lot（而非直接修改）+ 對應合成 fill
            # 配股數量（股，取整；碎股殘值→零股款屬 MVP 已知小缺口）
            bonus_qty = int(old_qty * ratio)
            new_lot_id = uuid.uuid4().hex
            synth_fill_id = f"STOCK_DIVIDEND_{action_id}_{lot_id}"

            # 合成 fill：使 reconcile 的 fills 淨額 ↔ position_lots 數量在總量與策略桶兩層平衡。
            # raw insert（不走 apply_fill_transaction）故不動現金；price 用調整後價，PnL 基準延續。
            cursor.execute(
                """
                INSERT INTO fills
                (fill_id, account_id, run_id, order_id, execution_key, symbol, side,
                 quantity, price, filled_at, created_at, is_long_term, source, strategy_id)
                VALUES (?, ?, ?, ?, ?, ?, 'BUY', ?, ?, ?, datetime('now'), ?, 'CORP_ACTION', ?)
                """,
                (
                    synth_fill_id,
                    account_id,
                    f"corp-{action_id}",
                    f"corp-order-{action_id}-{lot_id}",
                    synth_fill_id,
                    symbol,
                    bonus_qty,
                    new_price,
                    action["ex_date"],
                    is_long_term,
                    strategy_id,
                )
            )

            cursor.execute(
                """
                INSERT INTO position_lots
                (lot_id, account_id, symbol, quantity, price, acquired_at, fill_id,
                 is_long_term, strategy_id, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, datetime('now'))
                """,
                (
                    new_lot_id,
                    account_id,
                    symbol,
                    bonus_qty,  # 新增的配股數量
                    new_price,  # 配股 lot 價格採調整後價，與原 lot 一致
                    synth_fill_id,
                    is_long_term,
                    strategy_id,
                )
            )

            # 3b. 原 lot 的價格調整（分割調整價格以維持市值）
            cursor.execute(
                "UPDATE position_lots SET price = ? WHERE lot_id = ?",
                (new_price, lot_id)
            )

            # 記錄調整
            cursor.execute(
                """
                INSERT INTO position_cost_adjustments
                (adjustment_id, action_id, account_id, strategy_id, symbol, lot_id, field,
                 before_value, after_value, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'LOT_PRICE', ?, ?, datetime('now'))
                """,
                (uuid.uuid4().hex, action_id, account_id, strategy_id, symbol, lot_id, old_price, new_price)
            )

        # 3c. 更新 position_high_watermarks
        cursor.execute(
            """
            SELECT account_id, strategy_id, symbol, trade_date, highest_close
            FROM position_high_watermarks
            WHERE account_id = ? AND symbol = ?
            """,
            (account_id, symbol)
        )
        watermarks = [dict(row) for row in cursor.fetchall()]

        for wm in watermarks:
            old_close = wm["highest_close"]
            new_close = int(old_close / factor)
            cursor.execute(
                """
                UPDATE position_high_watermarks
                SET highest_close = ?
                WHERE account_id = ? AND strategy_id = ? AND symbol = ? AND trade_date = ?
                """,
                (new_close, wm["account_id"], wm["strategy_id"], wm["symbol"], wm["trade_date"])
            )
            cursor.execute(
                """
                INSERT INTO position_cost_adjustments
                (adjustment_id, action_id, account_id, strategy_id, symbol, field,
                 before_value, after_value, created_at)
                VALUES (?, ?, ?, ?, ?, 'WATERMARK', ?, ?, datetime('now'))
                """,
                (
                    uuid.uuid4().hex,
                    action_id,
                    wm["account_id"],
                    wm["strategy_id"],
                    symbol,
                    old_close,
                    new_close,
                )
            )
