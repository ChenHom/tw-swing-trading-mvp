"""寫入側 service（service 層的寫入側骨架）。

封裝 record-fill / reject-signal / un-reject-signal 三個寫入操作，純資料進出：
不 print、不 sys.exit、不碰 argparse。寫入一律走既有 ``PortfolioProjection`` 與
signal_items 既有 SQL，**不繞過**已驗證的 engine/projection 邏輯（見 ui-development §2 鐵律）。

成功回結構化 dict（presentation 由呼叫端各自渲染：CLI 印中文、未來 Web 渲染表單）；
使用者層級的驗證錯誤拋 ``TradeWriteError``。連線生命週期由呼叫端 own（比照 dashboard，
service 不負責 close）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.strategy import registry as strategy_registry


class TradeWriteError(Exception):
    """使用者可理解的寫入驗證錯誤（CLI/Web 各自渲染）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _uuid_like() -> str:
    return uuid.uuid4().hex[:8]


def record_fill(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    strategy_id: Optional[str] = None,
    is_long_term: bool = False,
    exit_strategy_ids: Optional[set] = None,
    trade_date: Optional[str] = None,
) -> dict:
    """補錄一筆外部券商已成交的 fill，走既有 FIFO/cash/PnL projection。

    - ``strategy_id`` 未指定 → MANUAL（沿用舊行為）。指定則須屬 registry 已登錄策略，
      否則 ``raise TradeWriteError("UNKNOWN_STRATEGY", ...)``（不寫入任何 fill）。
    - ``exit_strategy_ids``：具 exit 區塊（受 risk_exit 監控）的策略集合，由呼叫端決定
      （CLI 用 ``load_exit_managed_definitions``）。傳入集合則據以判監控資格；傳入
      ``None`` 表示無法判定 → ``monitor_status="indeterminate"``。監控判定保持為純函式輸入。
    - ``apply_fill_transaction`` 的 ``ValueError``（SELL_WITHOUT_POSITION /
      LONG_TERM_PROTECTED）往外傳，由呼叫端渲染。

    回傳 dict 含 account_id/symbol/side/quantity/price_scaled/strategy_id/is_long_term、
    trade_value/broker_fee/tax，以及 ``monitor_status`` 枚舉。
    """
    side = side.upper()
    strategy_id = strategy_id or MANUAL_STRATEGY_ID
    known_strategies = set(strategy_registry.PARAMS_MODELS) | {MANUAL_STRATEGY_ID}
    if strategy_id not in known_strategies:
        valid = ", ".join(sorted(strategy_registry.PARAMS_MODELS))
        raise TradeWriteError(
            "UNKNOWN_STRATEGY",
            f"錯誤：未知策略 strategy_id='{strategy_id}'。"
            f"可用策略：{valid}；或省略改用 {MANUAL_STRATEGY_ID}（預設，不受 risk_exit 監控）。",
        )

    price_scaled = int(round(price * 10000))
    is_long_term_int = 1 if is_long_term else 0

    # 回填日期：--date 指定成交日（補錄昨日/前日的真實成交），否則用現在。
    # filled_at 是所有現金事件/庫存/損益的經濟日期；run_id 沿用 manual-YYYYMMDD 慣例。
    # ponytail: 同日多筆 fill 都蓋 12:00，FIFO tie-break 退回插入序；若日後需精確盤中序，改帶真實成交時間。
    if trade_date:
        try:
            date.fromisoformat(trade_date)
        except ValueError:
            raise TradeWriteError("BAD_DATE", f"錯誤：--date 須為 YYYY-MM-DD，收到 '{trade_date}'。")
        filled_at = f"{trade_date}T12:00:00+08:00"
        run_compact = trade_date.replace("-", "")
    else:
        filled_at = datetime.now().isoformat()
        run_compact = date.today().strftime("%Y%m%d")

    fill_payload = {
        "fill_id": f"fill-manual-{_uuid_like()}",
        "account_id": account_id,
        "run_id": f"manual-{run_compact}",
        "order_id": f"ord-manual-{_uuid_like()}",
        "execution_key": f"manual-fill-{symbol}-{_uuid_like()}",
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price_scaled,
        "filled_at": filled_at,
        "is_long_term": is_long_term_int,
        "source": "MANUAL_IMPORT",
        "strategy_id": strategy_id,
    }

    # apply_fill_transaction 內已以 `with conn` 原子提交；ValueError 往外傳。
    PortfolioProjection(conn).apply_fill_transaction(fill_payload)

    trade_value = int(round(quantity * price_scaled / 10000.0))
    broker_fee = max(20, int(round(trade_value * 0.001425)))
    tax = int(round(trade_value * 0.003)) if side == "SELL" else 0

    monitor_status = _monitor_status(is_long_term, strategy_id, exit_strategy_ids)

    return {
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price_scaled": price_scaled,
        "strategy_id": strategy_id,
        "is_long_term": bool(is_long_term),
        "trade_value": trade_value,
        "broker_fee": broker_fee,
        "tax": tax,
        "monitor_status": monitor_status,
    }


def _monitor_status(is_long_term: bool, strategy_id: str, exit_strategy_ids: Optional[set]) -> str:
    """risk_exit 監控資格枚舉（與 RiskExitEngine / dashboard 一致）。

    long_term_excluded / manual_excluded：結構性排除。monitored / not_monitored：依
    strategy_id 是否屬具 exit 區塊集合。indeterminate：呼叫端未能提供 exit 集合（None）。
    """
    if is_long_term:
        return "long_term_excluded"
    if strategy_id == MANUAL_STRATEGY_ID:
        return "manual_excluded"
    if exit_strategy_ids is None:
        return "indeterminate"
    return "monitored" if strategy_id in exit_strategy_ids else "not_monitored"


def reject_signal(conn: sqlite3.Connection, *, signal_id: str, reason: Optional[str] = None) -> dict:
    """標記訊號為 REJECTED（trade plan / execute-pending 將跳過）。

    找不到 → ``TradeWriteError("SIGNAL_NOT_FOUND", ...)``。已是 REJECTED → 回
    ``status="already_rejected"``（非錯誤）。否則 UPDATE 並 commit，回 ``status="rejected"``。
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT signal_id, symbol, action, user_override FROM signal_items WHERE signal_id = ?",
        (signal_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise TradeWriteError("SIGNAL_NOT_FOUND", f"找不到訊號 ID：{signal_id}")

    if row["user_override"] == "REJECTED":
        return {
            "signal_id": signal_id,
            "symbol": row["symbol"],
            "action": row["action"],
            "status": "already_rejected",
        }

    reason = reason or "手動拒絕"
    now_str = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """
        UPDATE signal_items
        SET user_override = 'REJECTED', override_reason = ?, overridden_at = ?
        WHERE signal_id = ?
        """,
        (reason, now_str, signal_id),
    )
    conn.commit()
    return {
        "signal_id": signal_id,
        "symbol": row["symbol"],
        "action": row["action"],
        "reason": reason,
        "status": "rejected",
    }


def un_reject_signal(conn: sqlite3.Connection, *, signal_id: str) -> dict:
    """取消訊號的 REJECTED 標記，恢復為待執行狀態。

    找不到 → ``TradeWriteError("SIGNAL_NOT_FOUND", ...)``。非 REJECTED → 回
    ``status="not_rejected"``（含 user_override，非錯誤）。否則清除標記並 commit，回
    ``status="restored"``。
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT signal_id, symbol, action, user_override FROM signal_items WHERE signal_id = ?",
        (signal_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise TradeWriteError("SIGNAL_NOT_FOUND", f"找不到訊號 ID：{signal_id}")

    if row["user_override"] != "REJECTED":
        return {
            "signal_id": signal_id,
            "symbol": row["symbol"],
            "action": row["action"],
            "user_override": row["user_override"],
            "status": "not_rejected",
        }

    cursor.execute(
        "UPDATE signal_items SET user_override = NULL, override_reason = NULL, overridden_at = NULL WHERE signal_id = ?",
        (signal_id,),
    )
    conn.commit()
    return {
        "signal_id": signal_id,
        "symbol": row["symbol"],
        "action": row["action"],
        "status": "restored",
    }


def set_long_term(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    value: bool = True,
    strategy_id: str = MANUAL_STRATEGY_ID,
) -> dict:
    """把帳戶內某 (strategy_id, symbol) bucket 的持倉重分類為長期持有（value=True）或取消。

    **限定單一策略 bucket**（預設 MANUAL）：同一 symbol 可能同時存在手動長期持倉與策略
    交易部位（FIFO 依 strategy_id 隔離），若全 symbol 翻旗會誤把策略部位排除於 risk_exit
    監控之外。本功能語意為「將手動持倉標長期」，故預設只動 MANUAL bucket；要動其他策略
    bucket 須顯式指定 strategy_id。

    `is_long_term` 是不可變事實 `fills` 的欄位（rebuild_from_ledger 自 fills 重建
    position_lots），故須更新 fills 後重建投影才持久。重分類不改變數量／FIFO 淨額，
    `reconcile` 比對淨額，翻旗+重建後仍應通過。

    回傳 affected（更新筆數）、該 bucket 重建後 position_qty 與 is_long_term、reconcile_status。
    查無該 bucket 成交 → affected=0（非錯誤）。
    """
    cursor = conn.cursor()
    target = 1 if value else 0
    cursor.execute(
        "UPDATE fills SET is_long_term = ? WHERE account_id = ? AND symbol = ? AND strategy_id = ?",
        (target, account_id, symbol, strategy_id),
    )
    affected = cursor.rowcount
    conn.commit()

    if affected == 0:
        return {
            "account_id": account_id,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "affected": 0,
            "position_qty": 0,
            "is_long_term": value,
            "reconcile_status": None,
        }

    projection = PortfolioProjection(conn)
    projection.rebuild_from_ledger(account_id)

    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    bucket = positions.get((strategy_id, symbol))
    recon = projection.reconcile(account_id)

    return {
        "account_id": account_id,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "affected": affected,
        "position_qty": bucket["quantity"] if bucket else 0,
        "is_long_term": bucket["is_long_term"] if bucket else value,
        "reconcile_status": recon.get("status") if isinstance(recon, dict) else None,
    }
