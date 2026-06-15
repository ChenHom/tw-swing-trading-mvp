import sqlite3
from pathlib import Path
from typing import Optional

def get_db_connection(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(path))
    # Enable foreign key support and dictionary/row-like access
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str) -> None:
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    # 1. market_bars
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS market_bars (
        symbol TEXT NOT NULL,
        exchange TEXT NOT NULL,
        instrument_type TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open INTEGER NOT NULL,
        high INTEGER NOT NULL,
        low INTEGER NOT NULL,
        close INTEGER NOT NULL,
        volume INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        source TEXT NOT NULL,
        source_timezone TEXT NOT NULL,
        is_complete INTEGER NOT NULL,
        source_fetched_at TEXT NOT NULL,
        raw_payload_checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, exchange, trade_date, source)
    );
    """)
    
    # 2. daily_runs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_runs (
        run_id TEXT NOT NULL,
        run_date TEXT NOT NULL,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        status TEXT NOT NULL,
        market_sync_status TEXT NOT NULL,
        execution_status TEXT NOT NULL,
        signal_generation_status TEXT NOT NULL,
        report_status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        last_error_code TEXT,
        PRIMARY KEY (run_date, account_id, strategy_id)
    );
    """)
    
    # 3. fills
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        execution_key TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price INTEGER NOT NULL,
        filled_at TEXT NOT NULL,
        reverses_fill_id TEXT,
        created_at TEXT NOT NULL,
        is_long_term INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'STRATEGY',
        strategy_id TEXT NOT NULL DEFAULT ''
    );
    """)
    
    # 4. cash_ledger
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cash_ledger (
        ledger_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        currency TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        UNIQUE(account_id, source_type, source_id, event_type)
    );
    """)
    
    # 5. position_lots
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS position_lots (
        lot_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price INTEGER NOT NULL,
        acquired_at TEXT NOT NULL,
        fill_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_long_term INTEGER NOT NULL DEFAULT 0,
        strategy_id TEXT NOT NULL DEFAULT ''
    );
    """)

    # 5b. position_high_watermarks (append-only fact table; NOT cleared by rebuild)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS position_high_watermarks (
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        highest_close INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, strategy_id, symbol, trade_date)
    );
    """)
    
    # 6. fifo_matches
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fifo_matches (
        match_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        buy_fill_id TEXT NOT NULL,
        sell_fill_id TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        buy_price INTEGER NOT NULL,
        sell_price INTEGER NOT NULL,
        matched_at TEXT NOT NULL,
        realized_pnl INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        strategy_id TEXT NOT NULL DEFAULT ''
    );
    """)

    # 7. realized_pnl
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS realized_pnl (
        pnl_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        realized_amount INTEGER NOT NULL,
        tax_amount INTEGER NOT NULL,
        fee_amount INTEGER NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        strategy_id TEXT NOT NULL DEFAULT ''
    );
    """)
    
    # 8. cash_balances
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cash_balances (
        account_id TEXT PRIMARY KEY,
        balance INTEGER NOT NULL,
        currency TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    
    # 9. signal_bundles
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_bundles (
        bundle_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        target_execution_date TEXT NOT NULL,
        market_data_cutoff TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    
    # 10. signal_items
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_items (
        item_id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        signal_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        reference_price INTEGER NOT NULL,
        reason_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(bundle_id, symbol)
    );
    """)
    
    # 11. order_intents
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS order_intents (
        intent_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        bundle_id TEXT NOT NULL,
        signal_id TEXT NOT NULL,
        execution_key TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        target_execution_date TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    
    # 12. broker_orders
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS broker_orders (
        client_order_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price INTEGER NOT NULL,
        status TEXT NOT NULL,
        filled_quantity INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    
    # 13. execution_events (audit trail: NETTING_SUPPRESSED, APPROVAL_NOT_FOUND, ...)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS execution_events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        strategy_id TEXT,
        symbol TEXT,
        detail TEXT,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # 14. corporate_actions (append-only fact table for dividend/split/etc)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS corporate_actions (
        action_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        action_type TEXT NOT NULL,
        ex_date TEXT NOT NULL,
        cash_per_share INTEGER,
        stock_ratio REAL,
        source TEXT NOT NULL,
        memo TEXT,
        created_at TEXT NOT NULL
    );
    """)

    # 15. position_cost_adjustments (audit trail for corporate action adjustments)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS position_cost_adjustments (
        adjustment_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        lot_id TEXT,
        field TEXT NOT NULL,
        before_value INTEGER,
        after_value INTEGER,
        before_text TEXT,
        after_text TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (action_id) REFERENCES corporate_actions(action_id)
    );
    """)

    # Migration: Add is_long_term column to fills if it doesn't exist
    cursor.execute("PRAGMA table_info(fills);")
    fill_columns = [row["name"] for row in cursor.fetchall()]
    if "is_long_term" not in fill_columns:
        cursor.execute("ALTER TABLE fills ADD COLUMN is_long_term INTEGER NOT NULL DEFAULT 0;")
    if "source" not in fill_columns:
        cursor.execute("ALTER TABLE fills ADD COLUMN source TEXT NOT NULL DEFAULT 'STRATEGY';")
    if "strategy_id" not in fill_columns:
        cursor.execute("ALTER TABLE fills ADD COLUMN strategy_id TEXT NOT NULL DEFAULT '';")

    # Migration: Add is_long_term column to position_lots if it doesn't exist
    cursor.execute("PRAGMA table_info(position_lots);")
    lot_columns = [row["name"] for row in cursor.fetchall()]
    if "is_long_term" not in lot_columns:
        cursor.execute("ALTER TABLE position_lots ADD COLUMN is_long_term INTEGER NOT NULL DEFAULT 0;")
    if "strategy_id" not in lot_columns:
        cursor.execute("ALTER TABLE position_lots ADD COLUMN strategy_id TEXT NOT NULL DEFAULT '';")

    # Migration: Add strategy_id to fifo_matches / realized_pnl for per-strategy attribution
    cursor.execute("PRAGMA table_info(fifo_matches);")
    match_columns = [row["name"] for row in cursor.fetchall()]
    if "strategy_id" not in match_columns:
        cursor.execute("ALTER TABLE fifo_matches ADD COLUMN strategy_id TEXT NOT NULL DEFAULT '';")

    cursor.execute("PRAGMA table_info(realized_pnl);")
    pnl_columns = [row["name"] for row in cursor.fetchall()]
    if "strategy_id" not in pnl_columns:
        cursor.execute("ALTER TABLE realized_pnl ADD COLUMN strategy_id TEXT NOT NULL DEFAULT '';")

    # Migration: Add user_override column to signal_items if it doesn't exist
    # Allowed values: NULL (no override) | 'REJECTED' (human rejected, skip execution)
    cursor.execute("PRAGMA table_info(signal_items);")
    signal_item_columns = [row["name"] for row in cursor.fetchall()]
    if "user_override" not in signal_item_columns:
        cursor.execute("ALTER TABLE signal_items ADD COLUMN user_override TEXT;")
    if "override_reason" not in signal_item_columns:
        cursor.execute("ALTER TABLE signal_items ADD COLUMN override_reason TEXT;")
    if "overridden_at" not in signal_item_columns:
        cursor.execute("ALTER TABLE signal_items ADD COLUMN overridden_at TEXT;")
    # Allowed values: ENTRY (strategy entry signal) | RISK_EXIT (risk exit engine) | MANUAL
    if "signal_source" not in signal_item_columns:
        cursor.execute("ALTER TABLE signal_items ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'ENTRY';")

    conn.commit()
    conn.close()
