"""多策略升級 Migration：回填存量資料的 strategy_id 與移動停利 watermark。

冪等設計：所有回填僅針對 strategy_id = '' 的列；watermark 僅在無既有列時寫入。
執行：python3 -m scripts.migrate_multi_strategy [--db data/app.db] [--legacy-strategy trend_pullback]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID


def migrate(db_path: str, legacy_strategy_id: str) -> dict:
    init_db(db_path)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    report = {"anomalies": []}

    with conn:
        # 1. fills.strategy_id backfill
        cursor.execute(
            "UPDATE fills SET strategy_id = ? WHERE strategy_id = '' AND source = 'MANUAL_IMPORT'",
            (MANUAL_STRATEGY_ID,)
        )
        report["fills_manual"] = cursor.rowcount
        cursor.execute(
            "UPDATE fills SET strategy_id = ? WHERE strategy_id = ''",
            (legacy_strategy_id,)
        )
        report["fills_legacy"] = cursor.rowcount

        # 2. position_lots.strategy_id backfill (via originating fill)
        cursor.execute(
            """
            UPDATE position_lots SET strategy_id = (
                SELECT f.strategy_id FROM fills f WHERE f.fill_id = position_lots.fill_id
            )
            WHERE strategy_id = '' AND fill_id IN (SELECT fill_id FROM fills)
            """
        )
        report["lots_from_fills"] = cursor.rowcount
        cursor.execute("SELECT lot_id, symbol FROM position_lots WHERE strategy_id = ''")
        orphan_lots = cursor.fetchall()
        for lot in orphan_lots:
            report["anomalies"].append(
                f"lot {lot['lot_id']} ({lot['symbol']}) 無對應 fill，回填為 {legacy_strategy_id}"
            )
        cursor.execute(
            "UPDATE position_lots SET strategy_id = ? WHERE strategy_id = ''",
            (legacy_strategy_id,)
        )
        report["lots_orphan"] = cursor.rowcount

        # 3. fifo_matches via sell fill
        cursor.execute(
            """
            UPDATE fifo_matches SET strategy_id = (
                SELECT f.strategy_id FROM fills f WHERE f.fill_id = fifo_matches.sell_fill_id
            )
            WHERE strategy_id = '' AND sell_fill_id IN (SELECT fill_id FROM fills)
            """
        )
        report["fifo_matches"] = cursor.rowcount
        cursor.execute(
            "UPDATE fifo_matches SET strategy_id = ? WHERE strategy_id = ''",
            (legacy_strategy_id,)
        )
        report["fifo_matches_fallback"] = cursor.rowcount

        # 4. realized_pnl via matching SELL fill (account, symbol, occurred_at)
        cursor.execute(
            """
            UPDATE realized_pnl SET strategy_id = (
                SELECT f.strategy_id FROM fills f
                WHERE f.account_id = realized_pnl.account_id
                  AND f.symbol = realized_pnl.symbol
                  AND f.side = 'SELL'
                  AND f.filled_at = realized_pnl.occurred_at
                LIMIT 1
            )
            WHERE strategy_id = '' AND EXISTS (
                SELECT 1 FROM fills f
                WHERE f.account_id = realized_pnl.account_id
                  AND f.symbol = realized_pnl.symbol
                  AND f.side = 'SELL'
                  AND f.filled_at = realized_pnl.occurred_at
            )
            """
        )
        report["realized_pnl"] = cursor.rowcount
        cursor.execute("SELECT pnl_id, symbol FROM realized_pnl WHERE strategy_id = ''")
        for r in cursor.fetchall():
            report["anomalies"].append(
                f"realized_pnl {r['pnl_id']} ({r['symbol']}) 無法對應 SELL fill，回填為 {legacy_strategy_id}"
            )
        cursor.execute(
            "UPDATE realized_pnl SET strategy_id = ? WHERE strategy_id = ''",
            (legacy_strategy_id,)
        )
        report["realized_pnl_fallback"] = cursor.rowcount

        # 5. Watermark backfill: open strategy positions (MANUAL excluded — not risk_exit managed)
        cursor.execute(
            """
            SELECT account_id, strategy_id, symbol,
                   SUM(quantity) as qty,
                   CAST(SUM(CAST(quantity AS REAL) * price) / SUM(quantity) AS INTEGER) as wavg_price,
                   MIN(acquired_at) as first_acquired_at
            FROM position_lots
            WHERE is_long_term = 0 AND strategy_id != ?
            GROUP BY account_id, strategy_id, symbol
            HAVING SUM(quantity) > 0
            """,
            (MANUAL_STRATEGY_ID,)
        )
        positions = cursor.fetchall()
        report["watermarks"] = 0
        for pos in positions:
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM position_high_watermarks WHERE account_id = ? AND strategy_id = ? AND symbol = ?",
                (pos["account_id"], pos["strategy_id"], pos["symbol"])
            )
            if cursor.fetchone()["cnt"] > 0:
                continue  # already maintained by the daily update loop
            since = pos["first_acquired_at"][:10]
            cursor.execute(
                "SELECT MAX(close) as high, MAX(trade_date) as last_date FROM market_bars WHERE symbol = ? AND trade_date >= ?",
                (pos["symbol"], since)
            )
            bar_row = cursor.fetchone()
            observed_high = bar_row["high"]
            mark_date = bar_row["last_date"] or date.today().isoformat()
            if observed_high is None:
                report["anomalies"].append(
                    f"watermark {pos['symbol']} ({pos['strategy_id']})：{since} 起無任何 market_bars，以加權買入均價初始化"
                )
                observed_high = pos["wavg_price"]
            high = max(observed_high, pos["wavg_price"])
            cursor.execute(
                """
                INSERT OR IGNORE INTO position_high_watermarks
                    (account_id, strategy_id, symbol, trade_date, highest_close, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (pos["account_id"], pos["strategy_id"], pos["symbol"], mark_date, high)
            )
            report["watermarks"] += cursor.rowcount

    # 6. Post-migration reconcile per account
    cursor.execute("SELECT DISTINCT account_id FROM fills")
    accounts = [r["account_id"] for r in cursor.fetchall()]
    projection = PortfolioProjection(conn)
    report["reconcile"] = {acc: projection.reconcile(acc)["status"] for acc in accounts}

    conn.close()
    return report


def main():
    parser = argparse.ArgumentParser(description="多策略升級存量資料回填 (冪等)")
    parser.add_argument("--db", default="data/app.db", help="SQLite 資料庫路徑")
    parser.add_argument("--legacy-strategy", default="trend_pullback", help="升級前唯一策略的 strategy_id")
    args = parser.parse_args()

    report = migrate(args.db, args.legacy_strategy)

    print("=== 多策略 Migration 回填報告 ===")
    print(f"fills（手動 MANUAL）        : {report['fills_manual']}")
    print(f"fills（既有策略）           : {report['fills_legacy']}")
    print(f"position_lots（經 fill 對應）: {report['lots_from_fills']}")
    print(f"position_lots（孤兒回填）    : {report['lots_orphan']}")
    print(f"fifo_matches               : {report['fifo_matches']} (+fallback {report['fifo_matches_fallback']})")
    print(f"realized_pnl               : {report['realized_pnl']} (+fallback {report['realized_pnl_fallback']})")
    print(f"watermark 初始化           : {report['watermarks']}")
    print("\n對帳結果：")
    for acc, status in report["reconcile"].items():
        print(f"  {acc}: {status}")
    if report["anomalies"]:
        print("\n異常清單：")
        for a in report["anomalies"]:
            print(f"  - {a}")
    else:
        print("\n無異常。")

    failed = [s for s in report["reconcile"].values() if s != "RECONCILE_OK"]
    if failed:
        print("\n錯誤：對帳未通過，請檢查上方異常清單。")
        sys.exit(1)


if __name__ == "__main__":
    main()
