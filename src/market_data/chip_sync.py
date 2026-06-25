"""籌碼同步（FinMind → app.db）：三大法人合計買賣超 + 融資券餘額。

供 LLM 進場顧問提示詞補多因子（規則看不到的籌碼面）。寫入冪等（INSERT OR REPLACE）。
PIT 讀取（get_chips 只回 ≤ as_of 的列），與行情同口徑、提示詞不含未來。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from src.market_data.finmind_provider import FinMindProvider

_INST_DS = "TaiwanStockInstitutionalInvestorsBuySell"
_MARG_DS = "TaiwanStockMarginPurchaseShortSale"

# 三大法人類別 → 聚合桶（net = buy − sell，單位股）
_FOREIGN = {"Foreign_Investor", "Foreign_Dealer_Self"}
_TRUST = {"Investment_Trust"}
_DEALER = {"Dealer_self", "Dealer_Hedging"}


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def aggregate_institutional(rows: list[dict]) -> dict[str, dict]:
    """原始法人列 → {date: {foreign, trust, dealer, total}}（單位股）。未知類別忽略。"""
    out: dict[str, dict] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        net = (r.get("buy") or 0) - (r.get("sell") or 0)
        agg = out.setdefault(d, {"foreign": 0, "trust": 0, "dealer": 0})
        name = r.get("name")
        if name in _FOREIGN:
            agg["foreign"] += net
        elif name in _TRUST:
            agg["trust"] += net
        elif name in _DEALER:
            agg["dealer"] += net
    for a in out.values():
        a["total"] = a["foreign"] + a["trust"] + a["dealer"]
    return out


def _cache_record(conn: sqlite3.Connection, dataset: str, data_id: str, rows: list[dict], now: str) -> None:
    """記錄 FinMind 原始回應（記憶體最新一份；audit + 之後一律讀 DB 不再打 API）。"""
    conn.execute(
        """INSERT OR REPLACE INTO finmind_cache (dataset, data_id, response_json, row_count, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (dataset, data_id, json.dumps(rows, ensure_ascii=False), len(rows), now))


def sync_chips(conn: sqlite3.Connection, symbols: list[str], start: date, end: date,
               provider: Optional[FinMindProvider] = None) -> dict:
    """抓 symbols 在 [start,end] 的籌碼 → 記錄原始回應到 finmind_cache → 聚合 upsert chip_*。
    盤後排程呼叫一次；之後 LLM 顧問只讀 chip_* 表，不再即時打 API。"""
    provider = provider or FinMindProvider()
    n_inst = n_marg = 0
    now = _now()
    for sym in symbols:
        inst_rows = provider.fetch_institutional(sym, start, end)
        _cache_record(conn, _INST_DS, sym, inst_rows, now)
        for d, a in aggregate_institutional(inst_rows).items():
            conn.execute(
                """INSERT OR REPLACE INTO chip_institutional
                   (symbol, trade_date, foreign_net, trust_net, dealer_net, total_net, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'finmind', ?)""",
                (sym, d, a["foreign"], a["trust"], a["dealer"], a["total"], now))
            n_inst += 1
        marg_rows = provider.fetch_margin(sym, start, end)
        _cache_record(conn, _MARG_DS, sym, marg_rows, now)
        for r in marg_rows:
            if not r.get("date"):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO chip_margin
                   (symbol, trade_date, margin_balance, short_balance, source, created_at)
                   VALUES (?, ?, ?, ?, 'finmind', ?)""",
                (sym, r["date"], r.get("MarginPurchaseTodayBalance") or 0,
                 r.get("ShortSaleTodayBalance") or 0, now))
            n_marg += 1
    conn.commit()
    return {"institutional_rows": n_inst, "margin_rows": n_marg, "symbols": len(symbols)}


def get_chips(conn: sqlite3.Connection, symbol: str, as_of: date, days: int = 5) -> Optional[dict]:
    """PIT 讀：近 days 日三大法人合計（≤ as_of）+ 最新一日融資券餘額。皆無資料回 None。"""
    inst = conn.execute(
        """SELECT trade_date, foreign_net, trust_net, dealer_net, total_net
           FROM chip_institutional WHERE symbol = ? AND trade_date <= ?
           ORDER BY trade_date DESC LIMIT ?""",
        (symbol, as_of.isoformat(), days)).fetchall()
    marg = conn.execute(
        """SELECT trade_date, margin_balance, short_balance FROM chip_margin
           WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1""",
        (symbol, as_of.isoformat())).fetchone()
    if not inst and not marg:
        return None
    return {
        "institutional": [dict(r) for r in inst][::-1],  # 由舊到新
        "margin": dict(marg) if marg else None,
    }
