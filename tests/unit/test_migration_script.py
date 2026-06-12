"""migrate_multi_strategy 回填腳本：冪等性、來源歸屬、watermark 初始化。"""
from datetime import date
from src.portfolio.db import init_db, get_db_connection
from scripts.migrate_multi_strategy import migrate


def _seed_legacy_db(db_path):
    """模擬升級前的存量資料：strategy_id 皆為空字串。"""
    init_db(db_path)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('acc-1', 100000, 'TWD', datetime('now'))"
    )
    cursor.execute(
        """
        INSERT INTO cash_ledger (ledger_id, account_id, run_id, event_type, amount, currency, source_type, source_id, occurred_at, idempotency_key, created_at)
        VALUES ('dep-1', 'acc-1', 'init', 'INITIAL_DEPOSIT', 100000, 'TWD', 'SYSTEM', 's1', datetime('now'), 'i1', datetime('now'))
        """
    )

    # 策略買入（source=STRATEGY, strategy_id 空）
    cursor.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, created_at, source)
        VALUES ('f-strat', 'acc-1', 'r1', 'o1', 'k1', '2330', 'BUY', 100, 1000000, '2026-06-01T09:00:00', datetime('now'), 'STRATEGY')
        """
    )
    cursor.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at)
        VALUES ('lot-strat', 'acc-1', '2330', 100, 1000000, '2026-06-01T09:00:00', 'f-strat', datetime('now'))
        """
    )
    # 手動錄入（source=MANUAL_IMPORT）
    cursor.execute(
        """
        INSERT INTO fills (fill_id, account_id, run_id, order_id, execution_key, symbol, side, quantity, price, filled_at, created_at, source)
        VALUES ('f-man', 'acc-1', 'r2', 'o2', 'k2', '2317', 'BUY', 50, 500000, '2026-06-02T09:00:00', datetime('now'), 'MANUAL_IMPORT')
        """
    )
    cursor.execute(
        """
        INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, fill_id, created_at)
        VALUES ('lot-man', 'acc-1', '2317', 50, 500000, '2026-06-02T09:00:00', 'f-man', datetime('now'))
        """
    )
    # 持有期間的行情（供 watermark 回填）
    cursor.execute(
        """
        INSERT INTO market_bars (symbol, exchange, instrument_type, trade_date, open, high, low, close, volume, amount, source, source_timezone, is_complete, source_fetched_at, raw_payload_checksum, created_at, updated_at)
        VALUES ('2330', 'TSE', 'STOCK', '2026-06-05', 1000000, 1210000, 990000, 1200000, 100, 1000, 'test', 'Asia/Taipei', 1, 'now', 'chk', datetime('now'), datetime('now'))
        """
    )
    conn.commit()
    conn.close()


def test_migration_backfill_and_idempotency(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _seed_legacy_db(db_path)

    report = migrate(db_path, "trend_pullback")

    assert report["fills_manual"] == 1
    assert report["fills_legacy"] == 1
    assert report["lots_from_fills"] == 2
    assert report["watermarks"] == 1  # 只有策略部位需要 watermark；MANUAL 排除
    assert all(s == "RECONCILE_OK" for s in report["reconcile"].values())

    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT strategy_id FROM fills WHERE fill_id = 'f-strat'")
    assert cursor.fetchone()["strategy_id"] == "trend_pullback"
    cursor.execute("SELECT strategy_id FROM fills WHERE fill_id = 'f-man'")
    assert cursor.fetchone()["strategy_id"] == "MANUAL"
    cursor.execute("SELECT strategy_id FROM position_lots WHERE lot_id = 'lot-strat'")
    assert cursor.fetchone()["strategy_id"] == "trend_pullback"
    cursor.execute("SELECT strategy_id FROM position_lots WHERE lot_id = 'lot-man'")
    assert cursor.fetchone()["strategy_id"] == "MANUAL"

    # watermark 取持有期間最高收盤（120 > 買入價 100）
    cursor.execute("SELECT highest_close FROM position_high_watermarks WHERE symbol = '2330' AND strategy_id = 'trend_pullback'")
    assert cursor.fetchone()["highest_close"] == 1200000
    cursor.execute("SELECT COUNT(*) as c FROM position_high_watermarks WHERE strategy_id = 'MANUAL'")
    assert cursor.fetchone()["c"] == 0
    conn.close()

    # 冪等：第二次執行不得再改任何資料
    report2 = migrate(db_path, "trend_pullback")
    assert report2["fills_manual"] == 0
    assert report2["fills_legacy"] == 0
    assert report2["lots_from_fills"] == 0
    assert report2["watermarks"] == 0
    assert all(s == "RECONCILE_OK" for s in report2["reconcile"].values())
