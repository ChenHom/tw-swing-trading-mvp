"""PIT 標的池治理（P0-T5）。

今日固定 21 檔清單（config/universe.yaml）只能以 policy_version 含 'diagnostic' 入帳——
此前綴是 S1（裁決五級狀態）的程式層防線：`RESEARCH_PASS` 必須改用非 diagnostic 的
PIT policy（如依指數成分史重建），尚未建立，屬未來工作。本模組只提供「policy 驅動池」
的機制本身：給定 policy_version + as_of 日期，回傳當天有效、且只用 known_at<=as_of
資訊判定的成分股（不可用後見之明排除/納入）。
"""
from datetime import date


class UniversePolicy:
    def __init__(self, conn):
        self.conn = conn

    def constituents_as_of(self, policy_version: str, as_of: date) -> list:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT symbol FROM universe_policy
            WHERE policy_version = ?
              AND known_at <= ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to >= ?)
            ORDER BY symbol
            """,
            (policy_version, as_of.isoformat(), as_of.isoformat(), as_of.isoformat())
        )
        return [row["symbol"] for row in cursor.fetchall()]

    def is_diagnostic_only(self, policy_version: str) -> bool:
        return "diagnostic" in policy_version.lower()

    def seed_diagnostic_policy(self, policy_version: str, symbols: list, as_of: date) -> None:
        """以今日固定清單建立 diagnostic-only policy（S1：永遠 INVALID，僅供淘汰用，不可晉級）。"""
        if "diagnostic" not in policy_version.lower():
            raise ValueError("seed_diagnostic_policy 的 policy_version 必須含 'diagnostic'，標記其不可晉級")
        cursor = self.conn.cursor()
        for symbol in symbols:
            cursor.execute(
                """
                INSERT INTO universe_policy
                (policy_version, symbol, effective_from, effective_to, known_at,
                 inclusion_reason, exclusion_reason, created_at)
                VALUES (?, ?, ?, NULL, ?, 'CURRENT_FIXED_LIST_DIAGNOSTIC_ONLY', NULL, datetime('now'))
                ON CONFLICT(policy_version, symbol, effective_from) DO NOTHING
                """,
                (policy_version, symbol, as_of.isoformat(), as_of.isoformat())
            )
        self.conn.commit()
