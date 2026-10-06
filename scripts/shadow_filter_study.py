"""breakout_shadow_filter 主檢定（訊號層級配對分析，thesis §B）。純讀 research.db，不寫 DB。

執行（在有 research.db 與 PIT universe 的機器上）：
  python3 -m scripts.shadow_filter_study --db data/research.db \
      --universe-policy liquidity-top150-v1 --from 2018-01-01 --to 2026-06-22 \
      --output artifacts/reports/research/breakout_shadow_filter-signal-study.json

輸出 JSON：summary（含預先登錄的 P1~P4 判定與 verdict）＋ 逐筆 outcomes，並印出 summary。
"""
import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.application.research.shadow_filter_study import (
    InMemoryBars, enumerate_outcomes, outcomes_as_dicts, summarize,
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.cli import common
from src.contracts.models import TrendBreakoutParams
from src.portfolio.db import get_db_connection
from src.strategy import registry
from src.strategy.universe import PolicyUniverseProvider

STRATEGY_ID = "breakout_shadow_filter"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--universe-policy", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--price-basis", default="raw")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    settings = common.get_settings()
    defn = registry.load_strategy_definition(settings, STRATEGY_ID)
    entry_params = TrendBreakoutParams(**defn.params.model_dump(exclude={"shadow_window_days"}))
    index_symbol = settings.trading.pipeline.index_symbol
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    conn = get_db_connection(args.db)
    symbols = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM universe_policy WHERE policy_version = ? ORDER BY symbol",
        (args.universe_policy,),
    )]
    if not symbols:
        sys.exit(f"universe_policy '{args.universe_policy}' 無成分股")
    store = InMemoryBars(conn, symbols + [index_symbol], price_basis=args.price_basis)
    sessions = list(ExchangeCalendarsTradingCalendar().sessions_between(start, end))

    outcomes = enumerate_outcomes(
        store, PolicyUniverseProvider(conn, args.universe_policy), sessions, sessions, index_symbol,
        entry_params, defn.exit_params, defn.params.shadow_window_days,
        settings.backtest.slippage_bps, end,
    )
    summary = summarize(outcomes)
    summary["inputs"] = {
        "db_sha256": _sha256(args.db), "universe_policy": args.universe_policy,
        "start": args.start, "end": args.end, "price_basis": args.price_basis,
        "strategy_version": defn.strategy_version, "params_hash": defn.params_hash,
        "slippage_bps": settings.backtest.slippage_bps, "index_symbol": index_symbol,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "outcomes": outcomes_as_dicts(outcomes)}, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
