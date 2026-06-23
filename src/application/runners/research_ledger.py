"""Research Ledger（P2-T2）：append-only 研究嘗試紀錄，失敗/棄用版本不刪——
餵 DSR（[backtest.py](backtest.py) `_deflated_sharpe_ratio`）的 num_trials，
修正多重檢定下「試到一個碰巧好看」的機率。"""
import sqlite3
import uuid


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
    """DSR num_trials：同 strategy_id 下曾嘗試過的相異 (strategy_version, params_hash) 組合數，
    含已棄用/失敗版本——不可因後來否決就排除，否則低估真實試驗次數、高估 DSR。"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(DISTINCT strategy_version || ':' || params_hash) AS n "
        "FROM research_ledger WHERE strategy_id = ?",
        (strategy_id,),
    )
    return cursor.fetchone()["n"]
