import sqlite3
import uuid
from datetime import date, datetime

class PortfolioLedger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def deposit(self, account_id: str, run_id: str, amount: int, currency: str, date_val: date) -> None:
        cursor = self.conn.cursor()
        ledger_id = f"led-{uuid.uuid4().hex[:8]}"
        idempotency_key = f"dep-{account_id}-{run_id}-{date_val.isoformat()}"
        
        cursor.execute(
            """
            INSERT INTO cash_ledger (
                ledger_id, account_id, run_id, event_type, amount, currency,
                source_type, source_id, occurred_at, idempotency_key, created_at
            ) VALUES (?, ?, ?, 'INITIAL_DEPOSIT', ?, ?, 'SYSTEM', 'DEPOSIT', ?, ?, datetime('now'))
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                ledger_id,
                account_id,
                run_id,
                amount,
                currency,
                date_val.isoformat() + "T00:00:00+08:00",
                idempotency_key
            )
        )
        self.conn.commit()
