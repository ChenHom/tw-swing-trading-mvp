"""四種資料分離 + 家族級 Final lockbox（P2-T3）。

`strategy_family` 沿用既有 `strategy_id`（同一策略不同 version 本就共用同一 strategy_id，
天然就是「同家族」分組——換版本不換 family，故無需新發明一套家族命名）。

四種資料（依日期切兩刀分三段 + 開放式第四段）：
- Training：<= training_end_date（設計規則/參數）
- Walk-forward validation：(training_end_date, walkforward_end_date]（選模型/驗參數高原，已屬研究資料）
- Final lockbox：(walkforward_end_date, lockbox_end_date]（**整個家族只能開封一次**，
  不可每次新版本/新 Challenger 都重開——否則等於反覆偷看同一段「未來」）
- Live forward data：> lockbox_end_date（lockbox 開封後唯一可信的新證據）

切界日期須在開封前寫死（看結果前），`set_data_partition_policy` 與 `record_lockbox_opening`
皆以 DB 約束（PRIMARY KEY）擋掉事後覆寫/重開，非僅靠呼叫端自律。
"""
import sqlite3
from datetime import date
from typing import Optional


def set_data_partition_policy(
    conn: sqlite3.Connection, strategy_family: str,
    training_end_date: date, walkforward_end_date: date, lockbox_end_date: date,
) -> None:
    """切界日期只能寫一次；同 family 重複呼叫不覆寫既有值（ON CONFLICT DO NOTHING）。"""
    conn.execute(
        """
        INSERT INTO data_partition_policy (
            strategy_family, training_end_date, walkforward_end_date, lockbox_end_date, created_at
        ) VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(strategy_family) DO NOTHING
        """,
        (strategy_family, training_end_date.isoformat(), walkforward_end_date.isoformat(),
         lockbox_end_date.isoformat()),
    )
    conn.commit()


def get_data_partition_policy(conn: sqlite3.Connection, strategy_family: str) -> Optional[dict]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT training_end_date, walkforward_end_date, lockbox_end_date "
        "FROM data_partition_policy WHERE strategy_family = ?",
        (strategy_family,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "training_end_date": date.fromisoformat(row["training_end_date"]),
        "walkforward_end_date": date.fromisoformat(row["walkforward_end_date"]),
        "lockbox_end_date": date.fromisoformat(row["lockbox_end_date"]),
    }


def classify_window(policy: dict, start_date: date, end_date: date) -> dict:
    """回視窗 [start_date, end_date] 觸及哪些資料分段；用於判斷一次 backtest/評估
    是否動到 lockbox 或 live forward 資料，而非僅資訊性質的單一標籤。"""
    bounds = [
        ("training", date.min, policy["training_end_date"]),
        ("walkforward", policy["training_end_date"], policy["walkforward_end_date"]),
        ("lockbox", policy["walkforward_end_date"], policy["lockbox_end_date"]),
        ("live_forward", policy["lockbox_end_date"], date.max),
    ]
    touched = [name for name, lo, hi in bounds if start_date <= hi and end_date > lo]
    return {
        "partitions_touched": touched,
        "touches_lockbox": "lockbox" in touched,
        "touches_live_forward": "live_forward" in touched,
    }


def record_lockbox_opening(
    conn: sqlite3.Connection, strategy_family: str, opened_by_strategy_version: str,
    opened_by_run_id: str, reason: str = None,
) -> None:
    """開封記錄；同 family 第二次呼叫違反 PRIMARY KEY 直接拋 sqlite3.IntegrityError——
    「只開一次」由 DB 約束擋，不靠呼叫端先檢查。"""
    conn.execute(
        """
        INSERT INTO lockbox_openings (
            strategy_family, opened_at, opened_by_strategy_version, opened_by_run_id, reason, created_at
        ) VALUES (?, datetime('now'), ?, ?, ?, datetime('now'))
        """,
        (strategy_family, opened_by_strategy_version, opened_by_run_id, reason),
    )
    conn.commit()


def has_lockbox_been_opened(conn: sqlite3.Connection, strategy_family: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM lockbox_openings WHERE strategy_family = ?", (strategy_family,))
    return cursor.fetchone() is not None
