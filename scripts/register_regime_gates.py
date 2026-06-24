"""註冊三支策略的 regime_gate_thresholds（R-T4b B4b）。

門檻數值＝各策略 docs/strategies/<id>.md「regime gate 提議數值」表，**看任何回測結果前**
即已寫死於 git 版控文件，本 script 只忠實轉錄進 DB（write-once，set 端 ON CONFLICT DO NOTHING）。
研究可重現用：fresh research.db 經 backfill + build-universe 後，跑本 script 補回 gate 再回測。

執行：python3 -m scripts.register_regime_gates [--db data/research.db]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.portfolio.db import init_db, get_db_connection
from src.application.runners.verdict import set_regime_gate_thresholds

REGIME_DEFINITION_VERSION = "regime-v1-phase1"

# (strategy_id, version, max_regime_drawdown, min_expectancy_ci_lower,
#  max_bear_underperformance, min_effective_sample_size, max_profit_concentration)
GATES = [
    ("trend_breakout", "1.0.0", 0.30, 0.0, 0.15, 30, 0.40),
    ("pullback_rebound", "1.0.0", 0.25, 0.0, 0.15, 30, 0.40),
    ("trend_rider", "1.0.0", 0.35, 0.0, 0.20, 20, 0.50),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/research.db")
    args = ap.parse_args()

    init_db(args.db)
    conn = get_db_connection(args.db)
    for sid, ver, maxdd, ci, bear, ess, hhi in GATES:
        set_regime_gate_thresholds(conn, sid, ver, REGIME_DEFINITION_VERSION, maxdd, ci, bear, ess, hhi)

    conn.row_factory = sqlite3.Row
    print("regime_gate_thresholds 現況：")
    for r in conn.execute(
        "SELECT strategy_id, strategy_version, max_regime_drawdown, min_expectancy_ci_lower, "
        "min_effective_sample_size, max_profit_concentration FROM regime_gate_thresholds ORDER BY strategy_id"
    ):
        print(" ", dict(r))


if __name__ == "__main__":
    main()
