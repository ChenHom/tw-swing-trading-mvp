"""Research Ledger（P2-T2）：append-only 研究嘗試紀錄，失敗/棄用版本不刪——
餵 DSR（[backtest.py](backtest.py) `_deflated_sharpe_ratio`）的 num_trials，
修正多重檢定下「試到一個碰巧好看」的機率。"""
import sqlite3
import uuid

# 研究家族：為程式隔離另開 strategy_id、但本質是某策略加濾網／變體者，試驗次數與過擬合責任
# 算回原家族（換 id 不得重置 DSR num_trials）。未列者自成一族（family = strategy_id）。
STRATEGY_FAMILIES: dict[str, str] = {
    "breakout_shadow_filter": "trend_breakout",
}


def strategy_family(strategy_id: str) -> str:
    return STRATEGY_FAMILIES.get(strategy_id, strategy_id)


def family_members(strategy_id: str) -> list[str]:
    family = strategy_family(strategy_id)
    return sorted({family, *(sid for sid, fam in STRATEGY_FAMILIES.items() if fam == family)})


def record_research_attempt(
    conn: sqlite3.Connection, *, strategy_id: str, strategy_version: str, params_hash: str,
    run_id: str, status: str = "TESTED", notes: str = None,
) -> None:
    conn.execute(
        """
        INSERT INTO research_ledger (entry_id, strategy_id, strategy_version, params_hash, run_id,
                                      status, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (str(uuid.uuid4()), strategy_id, strategy_version, params_hash, run_id, status, notes),
    )
    conn.commit()


def count_research_trials(conn: sqlite3.Connection, strategy_id: str) -> int:
    """DSR num_trials：同研究家族（見 STRATEGY_FAMILIES）下曾嘗試過的相異
    (strategy_id, strategy_version, params_hash) 組合數，含已棄用/失敗版本——不可因後來否決
    就排除，否則低估真實試驗次數、高估 DSR。"""
    members = family_members(strategy_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(DISTINCT strategy_id || ':' || strategy_version || ':' || params_hash) AS n "
        f"FROM research_ledger WHERE strategy_id IN ({','.join('?' for _ in members)})",
        members,
    )
    return cursor.fetchone()["n"]
