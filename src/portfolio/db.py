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
    # PK 維持原 4 欄（live upsert() 的 ON CONFLICT 目標不變，行為不受影響）。canonical bar
    # 不變式改用獨立 UNIQUE INDEX：(symbol, trade_date, price_basis) 唯一——research 寫入路徑
    # upsert_canonical() 以此為 ON CONFLICT 目標；任一來源重複寫入同一 (symbol,date,basis) 視為更新。
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
        price_basis TEXT NOT NULL DEFAULT 'raw',
        adjustment_factor REAL NOT NULL DEFAULT 1.0,
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
        created_at TEXT NOT NULL,
        account_id TEXT
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
        effective_date TEXT,
        known_at TEXT,
        ingested_at TEXT,
        source_payload_hash TEXT,
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

    # Migration: per-account scoping for signal_bundles.
    # NULL = global (entry signals are market facts, shared across accounts);
    # a set value = private exit bundle owned by that account. See risk_exit /
    # _find_bundles_for_execution. NULL stays "global" so pre-existing bundles
    # keep executing for whichever account holds the position.
    cursor.execute("PRAGMA table_info(signal_bundles);")
    bundle_columns = [row["name"] for row in cursor.fetchall()]
    if "account_id" not in bundle_columns:
        cursor.execute("ALTER TABLE signal_bundles ADD COLUMN account_id TEXT;")

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

    # Migration: free-text memo on cash_ledger (e.g. reason for a CASH_ADJUSTMENT withdrawal/deposit)
    cursor.execute("PRAGMA table_info(cash_ledger);")
    cash_ledger_columns = [row["name"] for row in cursor.fetchall()]
    if "memo" not in cash_ledger_columns:
        cursor.execute("ALTER TABLE cash_ledger ADD COLUMN memo TEXT;")

    # Migration: dual-price model on market_bars (raw vs adjusted canonical bars).
    # Pre-existing rows default to price_basis='raw', adjustment_factor=1.0 — identical to
    # current live semantics (Shioaji bars are always raw), so no behavior change.
    cursor.execute("PRAGMA table_info(market_bars);")
    market_bar_columns = [row["name"] for row in cursor.fetchall()]
    if "price_basis" not in market_bar_columns:
        cursor.execute("ALTER TABLE market_bars ADD COLUMN price_basis TEXT NOT NULL DEFAULT 'raw';")
    if "adjustment_factor" not in market_bar_columns:
        cursor.execute("ALTER TABLE market_bars ADD COLUMN adjustment_factor REAL NOT NULL DEFAULT 1.0;")
    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_market_bars_canonical
    ON market_bars (symbol, trade_date, price_basis);
    """)

    # Migration: PIT timestamps + integrity hash on corporate_actions (research ledger needs
    # known_at to gate "as of D" replay against events not yet public on D).
    cursor.execute("PRAGMA table_info(corporate_actions);")
    corp_action_columns = [row["name"] for row in cursor.fetchall()]
    if "effective_date" not in corp_action_columns:
        cursor.execute("ALTER TABLE corporate_actions ADD COLUMN effective_date TEXT;")
    if "known_at" not in corp_action_columns:
        cursor.execute("ALTER TABLE corporate_actions ADD COLUMN known_at TEXT;")
    if "ingested_at" not in corp_action_columns:
        cursor.execute("ALTER TABLE corporate_actions ADD COLUMN ingested_at TEXT;")
    if "source_payload_hash" not in corp_action_columns:
        cursor.execute("ALTER TABLE corporate_actions ADD COLUMN source_payload_hash TEXT;")

    # Migration: human-readable reason / status note on order_intents (revived for the
    # "next execution" plan persisted at signal-generation time; BLOCKED rows carry the
    # block reason here).
    cursor.execute("PRAGMA table_info(order_intents);")
    order_intent_columns = [row["name"] for row in cursor.fetchall()]
    if "reason" not in order_intent_columns:
        cursor.execute("ALTER TABLE order_intents ADD COLUMN reason TEXT;")

    # universe_policy (research.db)：PIT 標的池治理（policy 驅動，非固定清單）。
    # 今日固定 21 檔只能以 policy_version 含 'diagnostic' 入帳 — 該前綴是 S1 的程式層
    # 防線：RESEARCH_PASS 必須改用非 diagnostic 的 PIT policy（見 universe_policy.py）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS universe_policy (
        policy_version TEXT NOT NULL,
        symbol TEXT NOT NULL,
        effective_from TEXT NOT NULL,
        effective_to TEXT,
        known_at TEXT NOT NULL,
        inclusion_reason TEXT,
        exclusion_reason TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (policy_version, symbol, effective_from)
    );
    """)

    # run_fingerprints：每次 backtest run 落地 14 欄版本指紋（P0-T8），
    # 同指紋應重現同結果；可追溯結果差異來自資料修正/程式變動/參數變動。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS run_fingerprints (
        run_id TEXT PRIMARY KEY,
        dataset_hash TEXT NOT NULL,
        data_cutoff TEXT NOT NULL,
        universe_snapshot_id TEXT NOT NULL,
        corporate_action_version TEXT NOT NULL,
        cost_model_version TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        code_commit TEXT NOT NULL,
        random_seed INTEGER,
        engine_version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        trading_calendar_version TEXT NOT NULL,
        source_payload_manifest_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # regime_gate_thresholds：S1 裁決（P1-T7）的 regime gate 門檻，須在看到任何 backtest
    # 結果前寫入——事後依結果回填等於沒有 gate。無對應列的策略一律 INVALID。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS regime_gate_thresholds (
        strategy_id TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        regime_definition_version TEXT NOT NULL,
        max_regime_drawdown REAL NOT NULL,
        min_expectancy_ci_lower REAL NOT NULL,
        max_bear_underperformance REAL NOT NULL,
        min_effective_sample_size INTEGER NOT NULL,
        max_profit_concentration REAL NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (strategy_id, strategy_version)
    );
    """)

    # research_ledger：append-only 研究嘗試紀錄（P2-T2）——失敗/棄用版本不刪，
    # 餵 DSR（backtest.py _deflated_sharpe_ratio）的 num_trials。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS research_ledger (
        entry_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        run_id TEXT,
        status TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    """)

    # data_partition_policy + lockbox_openings：四種資料分離 + 家族級 Final lockbox（P2-T3）。
    # strategy_family 沿用 strategy_id（同策略不同版本天然同家族）。切界日期看結果前寫死，
    # 同 family 第二次 INSERT 不覆寫既有切界；lockbox_openings 以 PK 擋「只開一次」。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS data_partition_policy (
        strategy_family TEXT PRIMARY KEY,
        training_end_date TEXT NOT NULL,
        walkforward_end_date TEXT NOT NULL,
        lockbox_end_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS lockbox_openings (
        strategy_family TEXT PRIMARY KEY,
        opened_at TEXT NOT NULL,
        opened_by_strategy_version TEXT NOT NULL,
        opened_by_run_id TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    );
    """)

    # finmind_cache：FinMind API 原始回應快取（記錄 api 回應資料）。盤後排程一次抓回，
    # 之後 LLM 顧問/聚合一律讀 DB（chip_* 表），不再即時打 API。response_json=原始列 JSON。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS finmind_cache (
        dataset TEXT NOT NULL,
        data_id TEXT NOT NULL,
        response_json TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (dataset, data_id)
    );
    """)

    # 籌碼（FinMind，免費 register 層即可取）。供 LLM 進場顧問提示詞補多因子。
    # chip_institutional：三大法人「合計買賣超」聚合，單位＝股（外資=Foreign_Investor+
    # Foreign_Dealer_Self；投信=Investment_Trust；自營=Dealer_self+Dealer_Hedging；net=buy−sell）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chip_institutional (
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        foreign_net INTEGER NOT NULL,
        trust_net INTEGER NOT NULL,
        dealer_net INTEGER NOT NULL,
        total_net INTEGER NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (symbol, trade_date)
    );
    """)
    # chip_margin：融資/融券餘額，單位＝張（FinMind 原值，TWSE 同口徑）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chip_margin (
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        margin_balance INTEGER NOT NULL,
        short_balance INTEGER NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (symbol, trade_date)
    );
    """)

    # signal_llm_reviews：LLM 進場顧問的 forward 記帳。系統不呼叫 LLM——它組好
    # PIT-safe 提示詞、人手動丟去 LLM、把回應與決定回填這裡。出場後既有 fifo_matches
    # realized_pnl 接得回，構成「訊號 → LLM 判斷 → 決定 → 結果」可查鏈，供日後驗證
    # 「LLM 說進 vs 說不進 vs 全收」誰賺。一個訊號每帳號一筆（重填覆寫）。
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_llm_reviews (
        signal_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        prompt TEXT NOT NULL,
        llm_response TEXT,
        decision TEXT,
        model_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (signal_id, account_id)
    );
    """)

    conn.commit()
    conn.close()
