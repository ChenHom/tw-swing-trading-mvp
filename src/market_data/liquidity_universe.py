"""PIT 流動性排名 universe builder（R-T4b Track 2）。

把固定 21 檔（後見之明手挑）換成「每個再平衡日、依當下已知的流動性排名取 top-N」的
per-date 成分股，寫進 `universe_policy`。policy_version 非 'diagnostic' → 通過 verdict 的
diagnostic 閘門（見 verdict.py / fingerprint.py）。

PIT 不變式：再平衡日 R 的成分只用 trade_date <= R 的資料算（前 lookback_sessions 個
**實際有資料的**交易日平均成交額 amount），known_at=R，不可後見之明。

殘留偏誤（誠實揭露，非完美 PIT）：
  - roster 來自 TaiwanScokInfo 單一快照（FinMindProvider.fetch_twse_roster），漏掉部分早期
    下市股（如 3662），故候選集本身仍有殘留 survivorship——但遠小於固定 21 檔。
  - 用「實際資料交易日」當再平衡視窗，未對齊官方交易日曆；停牌/缺檔以「該檔在視窗內出現
    次數」門檻過濾（min_sessions），避免新上市單日爆量混入。
"""
from datetime import date

_INSERT = """
INSERT INTO universe_policy
    (policy_version, symbol, effective_from, effective_to, known_at,
     inclusion_reason, exclusion_reason, created_at)
VALUES (?, ?, ?, ?, ?, ?, NULL, datetime('now'))
ON CONFLICT(policy_version, symbol, effective_from) DO NOTHING
"""


def _rebalance_dates(conn, start_date: date, end_date: date) -> list[str]:
    """每月第一個**實際有資料的**交易日（落在 [start, end]）作再平衡日。"""
    rows = conn.execute(
        """
        SELECT MIN(trade_date) AS d
        FROM market_bars
        WHERE price_basis = 'raw' AND trade_date BETWEEN ? AND ?
        GROUP BY substr(trade_date, 1, 7)
        ORDER BY d
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return [r["d"] for r in rows if r["d"]]


def _trailing_sessions(conn, as_of: str, lookback: int) -> list[str]:
    """as_of（含）往回 lookback 個實際交易日。"""
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM market_bars
        WHERE price_basis = 'raw' AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (as_of, lookback),
    ).fetchall()
    return [r["trade_date"] for r in rows]


def build_liquidity_policy(
    conn,
    policy_version: str,
    start_date: date,
    end_date: date,
    top_n: int = 150,
    lookback_sessions: int = 20,
    min_sessions: int | None = None,
) -> dict:
    """月再平衡、PIT 流動性 top-N 寫入 universe_policy。回傳統計 dict。

    min_sessions：視窗內至少要有幾個交易日才納入排名（過濾停牌/剛上市），預設 lookback*0.8。
    """
    if "diagnostic" in policy_version.lower():
        raise ValueError("流動性 policy 不可含 'diagnostic'（那是固定清單的保留前綴，永遠 INVALID）")
    if min_sessions is None:
        min_sessions = max(1, int(lookback_sessions * 0.8))

    rebals = _rebalance_dates(conn, start_date, end_date)
    if not rebals:
        return {"policy_version": policy_version, "rebalances": 0, "rows": 0, "distinct_symbols": 0}

    written = 0
    distinct: set[str] = set()
    for i, R in enumerate(rebals):
        window = _trailing_sessions(conn, R, lookback_sessions)
        if not window:
            continue
        placeholders = ",".join("?" for _ in window)
        ranked = conn.execute(
            f"""
            SELECT symbol, AVG(amount) AS avg_amount, COUNT(*) AS n
            FROM market_bars
            WHERE price_basis = 'raw' AND amount IS NOT NULL AND amount > 0
              AND trade_date IN ({placeholders})
            GROUP BY symbol
            HAVING n >= ?
            ORDER BY avg_amount DESC
            LIMIT ?
            """,
            (*window, min_sessions, top_n),
        ).fetchall()

        # membership 對 [R, 次月再平衡前一日] 有效；最後一段 effective_to=NULL（開放）
        eff_to = None if i == len(rebals) - 1 else _prev_day(rebals[i + 1])
        for rank, row in enumerate(ranked, start=1):
            conn.execute(
                _INSERT,
                (policy_version, row["symbol"], R, eff_to, R,
                 f"LIQUIDITY_TOP{top_n}_RANK{rank}"),
            )
            written += 1
            distinct.add(row["symbol"])
    conn.commit()
    return {
        "policy_version": policy_version,
        "rebalances": len(rebals),
        "rows": written,
        "distinct_symbols": len(distinct),
        "first_rebalance": rebals[0],
        "last_rebalance": rebals[-1],
    }


def _prev_day(iso_date: str) -> str:
    from datetime import date as _d, timedelta
    return (_d.fromisoformat(iso_date) - timedelta(days=1)).isoformat()
