"""LLM 進場顧問（P1：提示詞產生 + 回填記帳）。

系統**不呼叫 LLM**。它做兩件本份事：
1. 把某個訊號的 PIT-safe 行情（只用 ≤ 訊號日 D 的資料）組成一段可貼的提示詞；
2. 把人手動問 LLM 後的回應與決定回填、存進 signal_llm_reviews（forward 驗證的記帳）。

防幻覺：所有數字都由我們自家 market_bars 算好餵進提示詞，不靠 LLM 自行查。
PIT：market_repo.as_of(D) 只回 trade_date ≤ D 的 bar，故提示詞天然不含未來。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone, timedelta

from src.market_data.repository import SqliteMarketBarRepository
from src.market_data.chip_sync import get_chips
from src.contracts.stock_names import stock_name
from src.contracts.reason_codes import signal_reason_text
from src.contracts.strategy_names import strategy_name

DECISIONS = ["進場", "小部位試單", "不進場"]


def _lots(shares: int) -> int:
    """股 → 張（四捨五入）。"""
    return round(shares / 1000.0)


def _chip_lines(conn, symbol: str, d: date) -> list[str]:
    """籌碼段（單位張；正=買超/增、負=賣超/減）。無資料回 []（提示詞優雅省略）。
    讀 chip_* 表（盤後 21:00 排程 sync_chips 同步的 cache），**不即時打 FinMind API**。"""
    chips = get_chips(conn, symbol, d)
    if not chips:
        return []
    lines = ["", "【籌碼】（單位：張；正=買超/增，負=賣超/減）"]
    inst = chips["institutional"]
    if inst:
        tot5 = sum(r["total_net"] for r in inst)
        lines.append(f"近 {len(inst)} 日三大法人合計：{_lots(tot5):+,} 張")
        for r in inst:
            lines.append(
                f"  {r['trade_date']}  外資 {_lots(r['foreign_net']):+,} 投信 {_lots(r['trust_net']):+,} "
                f"自營 {_lots(r['dealer_net']):+,}  合計 {_lots(r['total_net']):+,}"
            )
    m = chips["margin"]
    if m:
        lines.append(f"融資餘額 {m['margin_balance']:,} 張；融券餘額 {m['short_balance']:,} 張（截至 {m['trade_date']}）")
    return lines


def _fetch_signal(conn: sqlite3.Connection, signal_id: str):
    return conn.execute(
        """
        SELECT si.signal_id, si.symbol, si.action, si.reference_price, si.reason_code,
               sb.strategy_id, sb.signal_date, sb.target_execution_date
        FROM signal_items si JOIN signal_bundles sb ON si.bundle_id = sb.bundle_id
        WHERE si.signal_id = ?
        """, (signal_id,)).fetchone()


def _ma(closes: list[float], n: int):
    return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None


def build_prompt(conn: sqlite3.Connection, market_repo: SqliteMarketBarRepository, signal_id: str) -> dict | None:
    """回傳 {signal: {...}, prompt: str}；查無此訊號回 None。"""
    row = _fetch_signal(conn, signal_id)
    if row is None:
        return None

    symbol = row["symbol"]
    name = stock_name(symbol) or symbol
    d = date.fromisoformat(row["signal_date"])
    # 需 60 日均線 + 近期 K 線，多抓緩衝
    history = market_repo.as_of(d).history(symbol, limit=70)
    closes = [b.close / 10000.0 for b in history]
    volumes = [b.volume for b in history]  # 股

    sig = {
        "signal_id": signal_id,
        "symbol": symbol,
        "name": name,
        "action": row["action"],
        "signal_date": row["signal_date"],
        "target_date": row["target_execution_date"],
        "strategy": strategy_name(row["strategy_id"]) or row["strategy_id"],
        "reason": signal_reason_text(row["reason_code"]),
    }

    if len(closes) < 20:
        sig["insufficient"] = True
        prompt = (
            f"{name}（{symbol}）資料不足（僅 {len(closes)} 根日K，<20），"
            f"無法組成可靠的進場研判提示詞。"
        )
        return {"signal": sig, "prompt": prompt}

    latest_close = round(closes[-1], 2)
    ma5, ma10, ma20, ma60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    vol_lots = round(volumes[-1] / 1000.0, 1)
    avg20_lots = round(sum(volumes[-21:-1]) / 20 / 1000.0, 1) if len(volumes) >= 21 else None
    vol_ratio = round(volumes[-1] / (sum(volumes[-21:-1]) / 20), 2) if len(volumes) >= 21 and sum(volumes[-21:-1]) > 0 else None

    def _ma_line(label, v):
        if v is None:
            return f"{label}：資料不足"
        rel = "站上" if latest_close >= v else "跌破"
        return f"{label}：約 {v}（收盤{rel}）"

    # 近 12 日 K 線（PIT，僅 ≤ D）
    kbars = []
    for b in history[-12:]:
        kbars.append(
            f"  {b.trade_date}  開{b.open/10000:.2f} 高{b.high/10000:.2f} "
            f"低{b.low/10000:.2f} 收{b.close/10000:.2f}  量{b.volume/1000:.1f}張"
        )

    lines = [
        f"請以台股波段交易角度，判斷 {name}（{symbol}）目前是否適合進場。",
        f"策略訊號：{sig['strategy']} / {sig['reason']}（{sig['action']}）。",
        f"資料截止＝{row['signal_date']} 收盤（D），次一交易日 {row['target_execution_date']} 開盤執行。",
        "",
        "【價格與均線】（單位：元）",
        f"最新收盤：{latest_close}",
        _ma_line("5 日均線", ma5),
        _ma_line("10 日均線", ma10),
        _ma_line("20 日均線", ma20),
        _ma_line("60 日均線", ma60),
        "",
        "【量能】（單位：張）",
        f"當日成交量：{vol_lots} 張"
        + (f"；近20日均量 {avg20_lots} 張（量比 {vol_ratio}）" if avg20_lots else ""),
        *_chip_lines(conn, symbol, d),
        "",
        "【近 12 日日K】",
        *kbars,
        "",
        "請依上列資料評估：均線排列與回踩/突破位置、量能是否健康、是否有追高風險；",
        "並給出結論〔進場 / 小部位試單 / 不進場〕＋低接區間、停損位、轉強確認位。",
        "（僅依上述資料判斷，勿臆測未提供的消息面或未來走勢。）",
    ]
    return {"signal": sig, "prompt": "\n".join(lines)}


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def save_review(conn: sqlite3.Connection, signal_id: str, account_id: str,
                prompt: str, llm_response: str, decision: str, model_note: str = "") -> None:
    """回填覆寫（一訊號每帳號一筆）。created_at 保留首次。"""
    existing = conn.execute(
        "SELECT created_at FROM signal_llm_reviews WHERE signal_id = ? AND account_id = ?",
        (signal_id, account_id)).fetchone()
    created = existing["created_at"] if existing else _now()
    conn.execute(
        """
        INSERT INTO signal_llm_reviews (signal_id, account_id, prompt, llm_response, decision, model_note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, account_id) DO UPDATE SET
            prompt=excluded.prompt, llm_response=excluded.llm_response,
            decision=excluded.decision, model_note=excluded.model_note, updated_at=excluded.updated_at
        """,
        (signal_id, account_id, prompt, llm_response, decision, model_note, created, _now()))
    conn.commit()


def get_review(conn: sqlite3.Connection, signal_id: str, account_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM signal_llm_reviews WHERE signal_id = ? AND account_id = ?",
        (signal_id, account_id)).fetchone()
    return dict(row) if row else None
