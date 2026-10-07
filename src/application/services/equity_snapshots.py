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


def read_equity_curve(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    """供 Web 混合圖：回傳全部每日權益，並附資金流校正後的每日損益與區間已實現淨損益（realized_pnl）。"""
    rows = conn.execute(
        """
        SELECT snapshot_date, cash, positions_value, total_equity
        FROM equity_snapshots
        WHERE account_id = ?
        ORDER BY snapshot_date
        """,
        (account_id,),
    ).fetchall()
    if not rows:
        return []

    capital_flows = conn.execute(
        """
        SELECT substr(occurred_at, 1, 10) AS flow_date, SUM(amount) AS amount
        FROM cash_ledger
        WHERE account_id = ?
          AND event_type IN ('INITIAL_DEPOSIT', 'CASH_ADJUSTMENT')
        GROUP BY substr(occurred_at, 1, 10)
        ORDER BY flow_date
        """,
        (account_id,),
    ).fetchall()

    # 已實現損益：與交易紀錄頁同樣以 substr(matched_at,1,10) 歸日；單次掃描、依日期排序
    matches = conn.execute(
        """
        SELECT substr(matched_at, 1, 10) AS match_date,
               SUM(net_realized_pnl) AS net,
               COUNT(*) - COUNT(net_realized_pnl) AS null_count
        FROM fifo_matches
        WHERE account_id = ?
        GROUP BY substr(matched_at, 1, 10)
        ORDER BY match_date
        """,
        (account_id,),
    ).fetchall()

    out = []
    flow_index = 0
    match_index = 0
    previous_date = None
    previous_equity = None
    for r in rows:
        current_date = r["snapshot_date"]
        interval_flow = 0
        while flow_index < len(capital_flows) and capital_flows[flow_index]["flow_date"] <= current_date:
            flow = capital_flows[flow_index]
            if previous_date is not None and flow["flow_date"] > previous_date:
                interval_flow += flow["amount"]
            flow_index += 1

        interval_realized = 0
        while match_index < len(matches) and matches[match_index]["match_date"] <= current_date:
            m = matches[match_index]
            if previous_date is None or m["match_date"] > previous_date:
                if m["null_count"] or interval_realized is None:
                    interval_realized = None
                else:
                    interval_realized += m["net"]
            match_index += 1

        out.append({
            "date": r["snapshot_date"],
            "cash": r["cash"],
            "position_value": r["positions_value"],
            "equity": r["total_equity"],
            "daily_pnl": (
                None if previous_equity is None
                else r["total_equity"] - previous_equity - interval_flow
            ),
            "realized_pnl": interval_realized,
        })
        previous_date = current_date
        previous_equity = r["total_equity"]

    return out
