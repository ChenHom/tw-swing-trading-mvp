#!/usr/bin/env python3
"""產生並落檔每日影子報告。

用法：
    python3 scripts/daily_report.py [--account ACCT] [--date YYYY-MM-DD] [--dir DIR]

落檔位置（預設 artifacts/reports/daily/）：
    <account>_<date>.txt   報告本體
    LATEST.txt             最新報告的絕對路徑
    INDEX.tsv              歷史索引（date / account / status / path）

成功時 stdout 最後一行印出：REPORT_PATH=<絕對路徑>，供 cron 擷取。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppSettings
from src.portfolio.db import get_db_connection
from src.portfolio.projection import PortfolioProjection
from src.approval.store import load_active_manifests
from src.application.reporting.daily_report import (
    build_daily_report, write_daily_report, ORCHESTRATOR_STRATEGY_ID, DEFAULT_REPORT_DIR,
)


def _resolve_account(conn, specified):
    if specified:
        return specified
    row = conn.execute(
        "SELECT account_id FROM cash_balances ORDER BY account_id LIMIT 1"
    ).fetchone()
    return row["account_id"] if row else "simulation-main"


def main() -> int:
    p = argparse.ArgumentParser(description="每日影子報告產生器")
    p.add_argument("--account", default=None, help="帳戶 ID（預設取 DB 第一個）")
    p.add_argument("--date", default=None, help="報告日期 YYYY-MM-DD（預設今天）")
    p.add_argument("--dir", default=DEFAULT_REPORT_DIR, help="報告輸出目錄")
    args = p.parse_args()

    settings = AppSettings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)

    account_id = _resolve_account(conn, args.account)
    report_date = date.fromisoformat(args.date) if args.date else date.today()

    manifests = load_active_manifests(settings)
    try:
        exit_ids = set(load_exit_strategy_ids(settings))
    except Exception:
        exit_ids = None

    run_status = ""
    row = conn.execute(
        "SELECT status FROM daily_runs WHERE run_date = ? AND account_id = ? AND strategy_id = ?",
        (report_date.isoformat(), account_id, ORCHESTRATOR_STRATEGY_ID),
    ).fetchone()
    if row:
        run_status = row["status"]

    text = build_daily_report(
        conn, projection, account_id, report_date,
        manifests=manifests, exit_strategy_ids=exit_ids,
    )
    path = write_daily_report(text, account_id, report_date, base_dir=args.dir, run_status=run_status)

    print(text)
    print(f"REPORT_PATH={path}")
    conn.close()
    return 0


def load_exit_strategy_ids(settings):
    """從 config/strategies/*.yaml 收集含 exit: 區塊的 strategy_id。"""
    import yaml
    ids = []
    strat_dir = Path("config/strategies")
    for yml in strat_dir.glob("*.yaml"):
        with open(yml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "exit" in data and data.get("strategy_id"):
            ids.append(data["strategy_id"])
    return ids


if __name__ == "__main__":
    raise SystemExit(main())
