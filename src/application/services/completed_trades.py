"""Read-only completed-trade history built from authoritative FIFO facts."""
from __future__ import annotations

import sqlite3
from datetime import date

from src.contracts.stock_names import stock_name
from src.contracts.strategy_names import strategy_name


def _iso_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def list_close_dates(conn: sqlite3.Connection, account_id: str) -> list[str]:
    """List dates containing completed FIFO matches, newest first."""
    rows = conn.execute(
        """
        SELECT DISTINCT substr(matched_at, 1, 10) AS close_date
        FROM fifo_matches
        WHERE account_id = ?
        ORDER BY close_date DESC
        """,
        (account_id,),
    ).fetchall()
    return [row["close_date"] for row in rows]


def read_completed_trades(
    conn: sqlite3.Connection, account_id: str, close_date: date | str
) -> list[dict]:
    """Return one row per SELL, with its FIFO matches nested in ``lots``."""
    rows = conn.execute(
        """
        SELECT fm.match_id, fm.symbol, fm.strategy_id, fm.buy_fill_id,
               fm.sell_fill_id, fm.quantity, fm.buy_price, fm.sell_price,
               fm.realized_pnl, fm.net_realized_pnl,
               bf.filled_at AS buy_filled_at, sf.filled_at AS sell_filled_at
        FROM fifo_matches fm
        JOIN fills bf ON bf.fill_id = fm.buy_fill_id
                     AND bf.account_id = fm.account_id
                     AND bf.symbol = fm.symbol
                     AND bf.strategy_id = fm.strategy_id
                     AND bf.side = 'BUY'
        JOIN fills sf ON sf.fill_id = fm.sell_fill_id
                     AND sf.account_id = fm.account_id
                     AND sf.symbol = fm.symbol
                     AND sf.strategy_id = fm.strategy_id
                     AND sf.side = 'SELL'
        WHERE fm.account_id = ? AND substr(fm.matched_at, 1, 10) = ?
        ORDER BY sf.filled_at, fm.sell_fill_id, bf.filled_at, fm.match_id
        """,
        (account_id, _iso_date(close_date)),
    ).fetchall()

    grouped: dict[str, dict] = {}
    for row in rows:
        sell_date = date.fromisoformat(row["sell_filled_at"][:10])
        buy_date = date.fromisoformat(row["buy_filled_at"][:10])
        trade = grouped.setdefault(
            row["sell_fill_id"],
            {
                "sell_fill_id": row["sell_fill_id"],
                "sell_filled_at": row["sell_filled_at"],
                "symbol": row["symbol"],
                "name": stock_name(row["symbol"]),
                "strategy_id": row["strategy_id"],
                "strategy_name": strategy_name(row["strategy_id"]),
                "sell_price": row["sell_price"] / 10000.0,
                "quantity": 0,
                "buy_value_x10000": 0,
                "gross_pnl": 0,
                "net_values": [],
                "earliest_buy_date": buy_date,
                "sell_date": sell_date,
                "lots": [],
            },
        )
        trade["quantity"] += row["quantity"]
        trade["buy_value_x10000"] += row["quantity"] * row["buy_price"]
        trade["gross_pnl"] += row["realized_pnl"]
        trade["net_values"].append(row["net_realized_pnl"])
        trade["earliest_buy_date"] = min(trade["earliest_buy_date"], buy_date)
        trade["lots"].append(
            {
                "match_id": row["match_id"],
                "buy_fill_id": row["buy_fill_id"],
                "sell_fill_id": row["sell_fill_id"],
                "buy_date": buy_date.isoformat(),
                "quantity": row["quantity"],
                "buy_price": row["buy_price"] / 10000.0,
                "holding_days": (sell_date - buy_date).days,
                "gross_pnl": row["realized_pnl"],
                "net_pnl": row["net_realized_pnl"],
            }
        )

    trades = []
    for trade in grouped.values():
        net_values = trade.pop("net_values")
        net_known = all(value is not None for value in net_values)
        net_pnl = sum(net_values) if net_known else None
        buy_value_x10000 = trade.pop("buy_value_x10000")
        trade["weighted_buy_price"] = buy_value_x10000 / trade["quantity"] / 10000.0
        trade["net_pnl"] = net_pnl
        trade["cost_twd"] = trade["gross_pnl"] - net_pnl if net_known else None
        trade["return_pct"] = net_pnl * 1_000_000 / buy_value_x10000 if net_known else None
        trade["holding_days"] = (
            trade.pop("sell_date") - trade.pop("earliest_buy_date")
        ).days
        trades.append(trade)
    return trades


def build_trade_day_summary(trades: list[dict]) -> dict:
    """Summarize one close date without inventing unknown historical net P&L."""
    all_net_known = all(trade["net_pnl"] is not None for trade in trades)
    return {
        "count": len(trades),
        "gross_pnl": sum(trade["gross_pnl"] for trade in trades),
        "cost_twd": sum(trade["cost_twd"] for trade in trades) if all_net_known else None,
        "net_pnl": sum(trade["net_pnl"] for trade in trades) if all_net_known else None,
        "has_unknown_net": not all_net_known,
    }


def build_completed_trade_history(
    conn: sqlite3.Connection,
    account_id: str,
    requested_date: date | str | None = None,
) -> dict:
    """Build selected-day trades plus navigation between dates that have closes."""
    dates = list_close_dates(conn, account_id)
    selected_date = (
        _iso_date(requested_date)
        if requested_date is not None
        else (dates[0] if dates else None)
    )
    trades = (
        read_completed_trades(conn, account_id, selected_date)
        if selected_date is not None
        else []
    )

    newer_date = None
    older_date = None
    if selected_date in dates:
        index = dates.index(selected_date)
        newer_date = dates[index - 1] if index > 0 else None
        older_date = dates[index + 1] if index + 1 < len(dates) else None

    return {
        "dates": dates,
        "selected_date": selected_date,
        "newer_date": newer_date,
        "older_date": older_date,
        "trades": trades,
        "summary": build_trade_day_summary(trades),
    }
