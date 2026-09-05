"""每日權益快照：cash_ledger + fills 重播出「截至某日」的現金/持倉市值。

不依賴 PortfolioProjection（那只反映「現在」的狀態）——純 SQL 重播同一份邏輯
同時餵往後每日寫入（DailySimulationRunner.run_daily）與歷史回填
（scripts/backfill_equity_snapshots.py）。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional


def _d(as_of_date) -> str:
    return as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)


def compute_equity_snapshot(conn: sqlite3.Connection, market_repo, account_id: str, as_of_date) -> dict:
    """截至 as_of_date（含當日）重播出 {cash, positions_value, total_equity}。"""
    d = _d(as_of_date)
    vd = as_of_date if isinstance(as_of_date, date) else date.fromisoformat(d)

    cash_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_ledger "
        "WHERE account_id = ? AND substr(occurred_at, 1, 10) <= ?",
        (account_id, d),
    ).fetchone()
    cash = cash_row["s"]

    rows = conn.execute(
        """
        SELECT symbol, SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) AS qty
        FROM fills
        WHERE account_id = ? AND substr(filled_at, 1, 10) <= ?
        GROUP BY symbol
        HAVING qty > 0
        """,
        (account_id, d),
    ).fetchall()

    positions_value = 0
    for r in rows:
        bar = market_repo.as_of(vd).latest(r["symbol"])
        if bar is not None:
            positions_value += int(r["qty"] * bar.close // 10000)

    return {
        "cash": cash,
        "positions_value": positions_value,
        "total_equity": cash + positions_value,
    }


def save_equity_snapshot(conn: sqlite3.Connection, account_id: str, as_of_date, snap: dict) -> None:
    """Upsert 一列快照（同日重跑安全）。"""
    d = _d(as_of_date)
    conn.execute(
        """
        INSERT INTO equity_snapshots (account_id, snapshot_date, cash, positions_value, total_equity, created_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(account_id, snapshot_date) DO UPDATE SET
            cash = excluded.cash,
            positions_value = excluded.positions_value,
            total_equity = excluded.total_equity,
            created_at = excluded.created_at
        """,
        (account_id, d, snap["cash"], snap["positions_value"], snap["total_equity"]),
    )
    conn.commit()


def backfill_equity_snapshots(conn: sqlite3.Connection, market_repo, account_id: str) -> int:
    """回填該帳號每個 COMPLETED 交易日的快照，回傳處理筆數。"""
    dates = [
        r["run_date"]
        for r in conn.execute(
            "SELECT DISTINCT run_date FROM daily_runs WHERE account_id = ? AND status = 'COMPLETED' ORDER BY run_date",
            (account_id,),
        ).fetchall()
    ]
    for d in dates:
        snap = compute_equity_snapshot(conn, market_repo, account_id, date.fromisoformat(d))
        save_equity_snapshot(conn, account_id, d, snap)
    return len(dates)


def read_equity_curve(conn: sqlite3.Connection, account_id: str, limit: int = 180) -> list[dict]:
    """供 Web 折線圖：欄位名對齊既有 backtest-charts.js 讀的 {date, cash, position_value, equity}。"""
    rows = conn.execute(
        """
        SELECT snapshot_date, cash, positions_value, total_equity
        FROM equity_snapshots
        WHERE account_id = ?
        ORDER BY snapshot_date DESC
        LIMIT ?
        """,
        (account_id, limit),
    ).fetchall()
    out = [
        {
            "date": r["snapshot_date"],
            "cash": r["cash"],
            "position_value": r["positions_value"],
            "equity": r["total_equity"],
        }
        for r in rows
    ]
    out.reverse()
    return out
