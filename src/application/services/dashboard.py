"""唯讀儀表板資料 service（service 層的讀取側雛形）。

純讀 projection / SQLite，回傳結構化 dict 供 Web 模板或其他前端渲染；
無副作用、可單元測試。寫入操作之後另立 service，但與本層共用 projection。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.contracts.stock_names import stock_name
from src.contracts.reason_codes import signal_reason_text, block_reason_text
from src.contracts.strategy_names import strategy_name, strategy_desc

ORCHESTRATOR_STRATEGY_ID = "MULTI"
REPORT_DIR = "artifacts/reports/daily"
BACKTEST_REPORT_DIR = "artifacts/reports/backtest"

# execution_events.event_type 代碼 → 使用者可讀中文（來源：engine._validate_buy_gate
# 的 block_reason 前綴）。未收錄者於 _events 退回顯示原碼。
EVENT_TYPE_LABELS = {
    "APPROVAL_NOT_FOUND": "查無有效授權",
    "APPROVAL_INVALID": "授權無效（過期/模式不符）",
    "APPROVAL_ID_MISMATCH": "授權 ID 不符",
    "PARAMS_HASH_MISMATCH": "策略參數與授權不符",
    "NETTING_SUPPRESSED": "同標反向訊號互抵，未送單",
}


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


def _positions(projection, account_id, exit_strategy_ids=None, market_repo=None, view_date=None):
    """全部策略持倉（含長期/MANUAL），標記是否受 risk_exit 監控。

    監控資格須與 RiskExitEngine 一致：非 MANUAL、非長期、且 strategy_id 屬具
    exit 區塊的策略（exit_strategy_ids）。exit_strategy_ids=None 表示呼叫端未提供
    （沿用寬鬆判定，僅排除 MANUAL/長期），正式 server 會帶入實際集合。

    market_repo + view_date 有給時附 view_date 當日收盤價 last_close（查無 bar → None，
    UI 顯示「—」，不以均價 fallback 以免誤讀為收盤）。
    """
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    vd = None
    if view_date is not None:
        vd = view_date if isinstance(view_date, date) else date.fromisoformat(str(view_date))
    out = []
    for (sid, symbol), pos in sorted(positions.items()):
        monitored = (
            sid != MANUAL_STRATEGY_ID
            and not pos["is_long_term"]
            and (exit_strategy_ids is None or sid in exit_strategy_ids)
        )
        last_close = None
        if market_repo is not None and vd is not None:
            bar = market_repo.find(symbol, vd)
            if bar is not None:
                last_close = _p(bar.close)
        out.append({
            "strategy_id": sid,
            "symbol": symbol,
            "name": stock_name(symbol),
            "quantity": pos["quantity"],
            "wavg_price": _p(pos["wavg_price"]),
            "last_close": last_close,
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
            "strategy_name": strategy_name(sid),
            "strategy_desc": strategy_desc(sid),
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
    return [{"side": r["side"], "symbol": r["symbol"], "name": stock_name(r["symbol"]),
             "quantity": r["quantity"], "price": _p(r["price"]),
             "strategy_id": r["strategy_id"], "source": r["source"]} for r in rows]


def _next_execution_signals(conn, account_id):
    """下次執行的待執行訊號：**與檢視日期解耦**，永遠取最新一批產生的訊號
    （`MAX(signal_date)`）。其 target_execution_date 即下一個交易日的執行計畫，
    故使用者在交易日盤前也看得到「今天開盤要執行什麼」，不因日期欄停在他日而變空。

    LEFT JOIN order_intents（run-daily Stage 3c 以同一規劃路徑 dry-run 後落檔）取得
    規劃股數與狀態；金額由 quantity × reference_price 計算。理由與被擋原因 humanize。
    """
    rows = conn.execute(
        """
        SELECT si.action, si.symbol, si.reason_code, si.reference_price,
               si.user_override, si.override_reason,
               sb.strategy_id, sb.bundle_id, sb.target_execution_date,
               oi.quantity AS planned_qty, oi.status AS intent_status, oi.reason AS block_reason
        FROM signal_items si JOIN signal_bundles sb ON si.bundle_id = sb.bundle_id
        LEFT JOIN order_intents oi
               ON oi.signal_id = si.signal_id AND oi.account_id = ?
              AND oi.target_execution_date = sb.target_execution_date
        WHERE sb.signal_date = (SELECT MAX(signal_date) FROM signal_bundles)
        ORDER BY sb.target_execution_date, sb.strategy_id, si.action, si.symbol
        """, (account_id,)).fetchall()
    out = []
    for r in rows:
        qty = r["planned_qty"]
        price = (r["reference_price"] or 0) / 10000.0  # 每股參考買入價位
        rejected = r["user_override"] == "REJECTED"
        blocked = (not rejected) and r["intent_status"] == "BLOCKED"
        out.append({
            "action": r["action"], "symbol": r["symbol"], "name": stock_name(r["symbol"]),
            "reason_text": signal_reason_text(r["reason_code"]),
            "strategy_id": r["strategy_id"],
            "strategy_name": strategy_name(r["strategy_id"]),
            "strategy_desc": strategy_desc(r["strategy_id"]),
            "is_exit": str(r["bundle_id"]).endswith("-exit"),
            "target_date": r["target_execution_date"],
            "quantity": qty,
            "price": price,
            "rejected": rejected,
            "reject_reason": r["override_reason"] if rejected else None,
            "blocked": blocked,
            "block_text": block_reason_text(r["block_reason"]) if blocked else None,
        })
    return out


def _events(conn, account_id, d):
    rows = conn.execute(
        """
        SELECT event_type, strategy_id, symbol, detail FROM execution_events
        WHERE account_id = ? AND occurred_at = ? ORDER BY event_type
        """, (account_id, d)).fetchall()
    out = []
    for r in rows:
        e = dict(r)
        e["event_label"] = EVENT_TYPE_LABELS.get(e["event_type"], e["event_type"])
        e["name"] = stock_name(e["symbol"]) if e["symbol"] else ""
        out.append(e)
    return out


def _corporate_actions(conn, account_id, positions, d) -> list:
    """近窗（檢視日 ±30 日）的公司行動事件，標已套用/未套用、是否持倉中。

    供儀表板提醒除息日前登錄並套用調整（否則 watermark/停損基準失真）。
    舊 DB 未 migration 時容錯回空。
    """
    held = {p["symbol"] for p in positions}
    try:
        rows = conn.execute(
            """
            SELECT ca.symbol, ca.action_type, ca.ex_date, ca.cash_per_share, ca.stock_ratio,
                   (SELECT COUNT(*) FROM position_cost_adjustments pca WHERE pca.action_id = ca.action_id) AS applied_cnt
            FROM corporate_actions ca
            WHERE ca.ex_date BETWEEN date(?, '-30 day') AND date(?, '+30 day')
            ORDER BY ca.ex_date
            """, (d, d)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        r = dict(r)
        if r["action_type"] == "CASH_DIVIDEND":
            detail = f"現金股利 {(r['cash_per_share'] or 0) / 10000:.2f} 元/股"
        else:
            detail = f"配股 {(r['stock_ratio'] or 0):.2%}"
        out.append({
            "symbol": r["symbol"],
            "name": stock_name(r["symbol"]),
            "ex_date": r["ex_date"],
            "detail_zh": detail,
            "applied": r["applied_cnt"] > 0,
            "held": r["symbol"] in held,
        })
    return out


def _reconcile_summary(recon) -> dict:
    """把 projection.reconcile() 的 dict 轉成使用者可讀摘要。

    回 {ok, code, detail_zh}：ok=是否通過；detail_zh 為通過/失敗的中文說明
    （失敗時含具體差異數字，對應 reconcile() 的三層檢查）。
    """
    code = recon.get("status") if isinstance(recon, dict) else None
    if code == "RECONCILE_OK":
        return {"ok": True, "code": code, "detail_zh": "帳本流水、持倉數量、策略桶均與投影一致。"}
    if code == "CASH_BALANCE_MISMATCH":
        detail = (f"現金：帳本流水合計 {recon.get('ledger_total'):,} "
                  f"≠ 餘額快照 {recon.get('balance_snapshot'):,}")
    elif code == "POSITION_QUANTITY_MISMATCH":
        detail = (f"持倉 {recon.get('symbol')}：成交淨額 {recon.get('expected'):,} "
                  f"≠ 庫存 {recon.get('actual'):,}")
    elif code == "STRATEGY_POSITION_MISMATCH":
        detail = (f"策略桶 {recon.get('symbol')}/{recon.get('strategy_id')}："
                  f"成交淨額 {recon.get('expected'):,} ≠ 庫存 {recon.get('actual'):,}")
    else:
        detail = str(recon)
    return {"ok": False, "code": code, "detail_zh": detail}


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
                    account_id: str, view_date, exit_strategy_ids=None, market_repo=None) -> dict:
    d = view_date.isoformat() if isinstance(view_date, date) else str(view_date)
    cash = projection.get_cash_balance(account_id)
    positions = _positions(projection, account_id, exit_strategy_ids, market_repo, view_date)
    monitored = [p for p in positions if p["monitored"]]
    reconcile = _reconcile_summary(projection.reconcile(account_id))
    return {
        "account_id": account_id,
        "date": d,
        "cash": cash,
        "run_status": _run_status(conn, account_id, d),
        "positions": positions,
        "monitored_count": len(monitored),
        "pnl": _pnl_by_strategy(conn, projection, account_id),
        "fills_today": _fills_today(conn, account_id, d),
        "next_execution": (_ne := _next_execution_signals(conn, account_id)),
        "next_execution_date": _ne[0]["target_date"] if _ne else None,
        "events": _events(conn, account_id, d),
        "reconcile": reconcile,
        "corporate_actions": _corporate_actions(conn, account_id, positions, d),
        # 向後相容：保留舊鍵供既有測試/消費者（reconcile_ok 布林）。
        "reconcile_ok": reconcile["ok"],
    }


def _resolve_close(repo, symbol, view_date, wavg_x10000):
    """回 (close_x10000:int, stale:bool)。

    view_date 當天有 bar → 用 bar.close；否則 fallback 用持倉均價當現價、stale=True
    （比照 src/cli/report.py 的 fallback，避免圓環少一塊、市值落空）。
    """
    bar = repo.find(symbol, view_date)
    if bar is not None:
        return bar.close, False
    return wavg_x10000, True


def _initial_deposit(conn, account_id) -> int:
    """淨投入本金 = INITIAL_DEPOSIT 合計（SQL 同 projection.rebuild_from_ledger）。

    DIVIDEND 不計入（它已反映在現金餘額，屬報酬而非投入）。
    """
    row = conn.execute(
        "SELECT SUM(amount) AS s FROM cash_ledger "
        "WHERE account_id = ? AND event_type = 'INITIAL_DEPOSIT'",
        (account_id,),
    ).fetchone()
    return row["s"] if row and row["s"] is not None else 0


def build_capital_overview(conn, projection, account_id, view_date, market_repo) -> dict:
    """資金總覽卡 + 持倉資產配置圓環資料（純讀）。

    - 市值 = int(qty * close // 10000)（整數整除，與 projection/backtest 對齊）；
      當日無 bar 以持倉加權均價 fallback 並標 stale。
    - 圓環含「現金」一塊，各塊分母 = 總權益（與卡片一致）。
    - 總報酬率分母 = 淨投入本金（INITIAL_DEPOSIT 合計）；分母 0 → None（UI 顯示「—」）。
    - 跨策略持有同一 symbol 在圓環聚合為一塊（使用者看的是「這檔佔多少」）。
    """
    vd = view_date if isinstance(view_date, date) else date.fromisoformat(str(view_date))
    cash = projection.get_cash_balance(account_id)

    # 依 symbol 聚合跨策略持倉：qty 加總、cost=Σ(qty*wavg) 供算數量加權均價。
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    by_symbol: dict = {}
    for (_sid, symbol), pos in positions.items():
        agg = by_symbol.setdefault(symbol, {"qty": 0, "cost": 0})
        agg["qty"] += pos["quantity"]
        agg["cost"] += pos["quantity"] * pos["wavg_price"]

    holdings = []
    any_stale = False
    positions_value = 0
    for symbol, agg in by_symbol.items():
        qty = agg["qty"]
        if qty <= 0:
            continue
        wavg = int(agg["cost"] / qty)  # 截斷，與 projection 的 CAST(... AS INTEGER) 一致
        close, stale = _resolve_close(market_repo, symbol, vd, wavg)
        value = int(qty * close // 10000)
        any_stale = any_stale or stale
        positions_value += value
        holdings.append({"symbol": symbol, "value": value, "stale": stale})

    total_equity = cash + positions_value
    net_principal = _initial_deposit(conn, account_id)
    total_return = total_equity - net_principal
    return_pct = (total_return / net_principal * 100.0) if net_principal > 0 else None

    # allocation：現金一塊（>0 才放）+ 各持倉塊（依 value 由大到小）；total_equity=0 → 空。
    allocation = []
    if total_equity > 0:
        if cash > 0:
            allocation.append({
                "label": "現金", "symbol": None, "value": cash,
                "ratio": cash / total_equity, "kind": "cash", "stale": False,
            })
        for h in sorted(holdings, key=lambda x: x["value"], reverse=True):
            if h["value"] <= 0:
                continue
            allocation.append({
                "label": h["symbol"], "symbol": h["symbol"], "value": h["value"],
                "ratio": h["value"] / total_equity, "kind": "position", "stale": h["stale"],
            })

    return {
        "account_id": account_id,
        "as_of_date": vd.isoformat(),
        "net_principal": net_principal,
        "cash": cash,
        "positions_value": positions_value,
        "total_equity": total_equity,
        "total_return": total_return,
        "return_pct": return_pct,
        "any_stale": any_stale,
        "allocation": allocation,
    }


def list_backtest_results(base_dir: str = BACKTEST_REPORT_DIR, limit: int = 30) -> list[dict]:
    """從 INDEX.tsv 讀回測結果清單（新到舊）。"""
    index_path = Path(base_dir) / "INDEX.tsv"
    if not index_path.exists():
        return []
    out = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 9:
            out.append({
                "created_at": parts[0],
                "strategy_id": parts[1],
                "run_id": parts[2],
                "start_date": parts[3],
                "end_date": parts[4],
                "final_equity": int(parts[5]),
                "total_pnl_bps": int(parts[6]),
                "max_drawdown": float(parts[7]),
                "name": f"{parts[1]}_{parts[2]}.json",
            })
    out.reverse()
    return out[:limit]


def read_backtest_result(name: str, base_dir: str = BACKTEST_REPORT_DIR) -> Optional[dict]:
    """安全讀取單一回測結果 JSON（防目錄穿越）。"""
    safe = Path(name).name  # 去掉任何路徑成分
    path = Path(base_dir) / safe
    if path.suffix != ".json" or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
