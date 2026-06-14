"""唯讀儀表板資料 service（service 層的讀取側雛形）。

純讀 projection / SQLite，回傳結構化 dict 供 Web 模板或其他前端渲染；
無副作用、可單元測試。寫入操作之後另立 service，但與本層共用 projection。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID

ORCHESTRATOR_STRATEGY_ID = "MULTI"
REPORT_DIR = "artifacts/reports/daily"


def _p(price: int) -> float:
    return price / 10000.0


def list_accounts(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT account_id FROM cash_balances ORDER BY account_id").fetchall()
    return [r["account_id"] for r in rows]


def latest_run_date(conn: sqlite3.Connection, account_id: Optional[str] = None) -> Optional[str]:
    """最近一個有 daily_run 的日期（預設儀表板日期用，避免落在無資料的假日）。"""
    if account_id:
        row = conn.execute(
            "SELECT MAX(run_date) AS d FROM daily_runs WHERE account_id = ?", (account_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT MAX(run_date) AS d FROM daily_runs").fetchone()
    return row["d"] if row and row["d"] else None


def _run_status(conn, account_id, d) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT status, market_sync_status, execution_status,
               signal_generation_status, report_status, last_error_code
        FROM daily_runs
        WHERE run_date = ? AND account_id = ? AND strategy_id = ?
        """,
        (d, account_id, ORCHESTRATOR_STRATEGY_ID),
    ).fetchone()
    return dict(row) if row else None


def _positions(projection, account_id, exit_strategy_ids=None):
    """全部策略持倉（含長期/MANUAL），標記是否受 risk_exit 監控。

    監控資格須與 RiskExitEngine 一致：非 MANUAL、非長期、且 strategy_id 屬具
    exit 區塊的策略（exit_strategy_ids）。exit_strategy_ids=None 表示呼叫端未提供
    （沿用寬鬆判定，僅排除 MANUAL/長期），正式 server 會帶入實際集合。
    """
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    out = []
    for (sid, symbol), pos in sorted(positions.items()):
        monitored = (
            sid != MANUAL_STRATEGY_ID
            and not pos["is_long_term"]
            and (exit_strategy_ids is None or sid in exit_strategy_ids)
        )
        out.append({
            "strategy_id": sid,
            "symbol": symbol,
            "quantity": pos["quantity"],
            "wavg_price": _p(pos["wavg_price"]),
            "is_long_term": pos["is_long_term"],
            "monitored": monitored,
        })
    return out


def _pnl_by_strategy(conn, projection, account_id):
    gross = {r["strategy_id"]: (r["gross"] or 0) for r in conn.execute(
        "SELECT strategy_id, SUM(realized_pnl) AS gross FROM fifo_matches WHERE account_id = ? GROUP BY strategy_id",
        (account_id,)).fetchall()}
    fees = {r["sid"]: (r["fees"] or 0) for r in conn.execute(
        """
        SELECT f.strategy_id AS sid, SUM(cl.amount) AS fees
        FROM cash_ledger cl JOIN fills f ON cl.source_id = f.fill_id
        WHERE cl.account_id = ? AND cl.event_type IN ('BROKER_FEE','TRANSACTION_TAX')
        GROUP BY f.strategy_id
        """, (account_id,)).fetchall()}
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    sids = sorted(set(gross) | set(fees) | {sid for (sid, _s) in positions})
    out = []
    for sid in sids:
        g = gross.get(sid, 0)
        fe = fees.get(sid, 0)
        out.append({
            "strategy_id": sid,
            "gross": g,
            "fees": fe,
            "net_realized": g + fe,
            "open_positions": sum(1 for (psid, _s) in positions if psid == sid),
        })
    return out


def _fills_today(conn, account_id, d):
    rows = conn.execute(
        """
        SELECT side, symbol, quantity, price, strategy_id, source
        FROM fills WHERE account_id = ? AND filled_at LIKE ? ORDER BY filled_at
        """, (account_id, f"{d}%")).fetchall()
    return [{"side": r["side"], "symbol": r["symbol"], "quantity": r["quantity"],
             "price": _p(r["price"]), "strategy_id": r["strategy_id"], "source": r["source"]} for r in rows]


def _next_signals(conn, d):
    rows = conn.execute(
        """
        SELECT si.action, si.symbol, si.reason_code, sb.strategy_id, sb.bundle_id, sb.target_execution_date
        FROM signal_items si JOIN signal_bundles sb ON si.bundle_id = sb.bundle_id
        WHERE sb.signal_date = ?
        ORDER BY sb.target_execution_date, sb.strategy_id, si.action, si.symbol
        """, (d,)).fetchall()
    return [{"action": r["action"], "symbol": r["symbol"], "reason_code": r["reason_code"],
             "strategy_id": r["strategy_id"], "is_exit": str(r["bundle_id"]).endswith("-exit"),
             "target_date": r["target_execution_date"]} for r in rows]


def _events(conn, account_id, d):
    rows = conn.execute(
        """
        SELECT event_type, strategy_id, symbol, detail FROM execution_events
        WHERE account_id = ? AND occurred_at = ? ORDER BY event_type
        """, (account_id, d)).fetchall()
    return [dict(r) for r in rows]


def list_reports(base_dir: str = REPORT_DIR, limit: int = 30) -> list[dict]:
    """從 INDEX.tsv 讀歷史報告清單（新到舊）。"""
    index_path = Path(base_dir) / "INDEX.tsv"
    if not index_path.exists():
        return []
    out = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            out.append({"date": parts[0], "account": parts[1], "status": parts[2], "path": parts[3]})
    out.reverse()
    return out[:limit]


def read_report(name: str, base_dir: str = REPORT_DIR) -> Optional[str]:
    """安全讀取單一報告檔（防目錄穿越）。"""
    safe = Path(name).name  # 去掉任何路徑成分
    path = Path(base_dir) / safe
    if path.suffix != ".txt" or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def build_dashboard(conn: sqlite3.Connection, projection: PortfolioProjection,
                    account_id: str, view_date, exit_strategy_ids=None) -> dict:
    d = view_date.isoformat() if isinstance(view_date, date) else str(view_date)
    cash = projection.get_cash_balance(account_id)
    positions = _positions(projection, account_id, exit_strategy_ids)
    monitored = [p for p in positions if p["monitored"]]
    recon = projection.reconcile(account_id)
    recon_ok = isinstance(recon, dict) and recon.get("status") == "RECONCILE_OK"
    return {
        "account_id": account_id,
        "date": d,
        "cash": cash,
        "run_status": _run_status(conn, account_id, d),
        "positions": positions,
        "monitored_count": len(monitored),
        "pnl": _pnl_by_strategy(conn, projection, account_id),
        "fills_today": _fills_today(conn, account_id, d),
        "next_signals": _next_signals(conn, d),
        "events": _events(conn, account_id, d),
        "reconcile_ok": recon_ok,
        "reconcile_detail": None if recon_ok else recon,
    }
