#!/usr/bin/env python3
"""一次性回填歷史每日權益快照（equity_snapshots）。

用法：
    python3 scripts/backfill_equity_snapshots.py [--account ACCT]

對該帳號每個 status='COMPLETED' 的 daily_runs 日期，重播 cash_ledger/fills
算出當日 cash/持倉市值並落 equity_snapshots（upsert，可重跑）。往後每日快照由
DailySimulationRunner.run_daily() 自動寫入，本腳本僅補齊部署前已累積的歷史。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppSettings
from src.portfolio.db import get_db_connection, init_db
from src.market_data.repository import SqliteMarketBarRepository
from src.application.services.equity_snapshots import backfill_equity_snapshots


def main() -> int:
    p = argparse.ArgumentParser(description="回填歷史每日權益快照")
    p.add_argument("--account", default="simulation-main", help="帳戶 ID（預設 simulation-main）")
    args = p.parse_args()

    settings = AppSettings()
    init_db(settings.trading.database_path)
    conn = get_db_connection(settings.trading.database_path)
    try:
        market_repo = SqliteMarketBarRepository(conn)
        count = backfill_equity_snapshots(conn, market_repo, args.account)
        print(f"回填完成：{args.account} 共 {count} 個交易日")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
