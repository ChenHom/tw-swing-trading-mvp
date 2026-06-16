#!/usr/bin/env python3
"""產生並落檔每日影子報告（cron / shadow_daily.sh 入口）。

用法：
    python3 scripts/daily_report.py [--account ACCT] [--date YYYY-MM-DD] [--dir DIR]

落檔位置（預設 artifacts/reports/daily/）：
    <account>_<date>.txt   報告本體
    LATEST.txt             最新報告的絕對路徑
    INDEX.tsv              歷史索引（date / account / status / path）

成功時 stdout 最後一行印出：REPORT_PATH=<絕對路徑>，供 cron 擷取。

實際邏輯與 CLI `python -m app report daily` 共用同一個
src.application.reporting.daily_report.generate_and_write_daily_report，零重複。
本腳本僅保留為既有 cron 入口（不動已上線排程）。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppSettings
from src.portfolio.db import get_db_connection
from src.application.reporting.daily_report import (
    generate_and_write_daily_report, DEFAULT_REPORT_DIR,
)


def main() -> int:
    p = argparse.ArgumentParser(description="每日影子報告產生器")
    p.add_argument("--account", default=None, help="帳戶 ID（預設取 DB 第一個）")
    p.add_argument("--date", default=None, help="報告日期 YYYY-MM-DD（預設今天）")
    p.add_argument("--dir", default=DEFAULT_REPORT_DIR, help="報告輸出目錄")
    args = p.parse_args()

    settings = AppSettings()
    conn = get_db_connection(settings.trading.database_path)

    report_date = date.fromisoformat(args.date) if args.date else None
    text, path, _account, _rdate = generate_and_write_daily_report(
        conn, settings,
        account=args.account, report_date=report_date, base_dir=args.dir,
    )

    print(text)
    print(f"REPORT_PATH={path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
