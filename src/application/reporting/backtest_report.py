"""回測結果落檔（equity curve + statistics + meta）。

`BacktestRunner.run()`（src/application/runners/backtest.py）算好的 equity_curve /
statistics 原本只在 CLI 印完 stdout 就丟棄。本模組把結果落成 JSON，供 Web 儀表板
讀取畫權益曲線；落檔模式比照 daily_report.write_daily_report 的
「結果檔 + LATEST.txt + INDEX.tsv」三件套。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

DEFAULT_BACKTEST_DIR = "artifacts/reports/backtest"


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def _sanitize_stats(stats: dict) -> dict:
    """把 inf/-inf/nan（如無虧損交易時的 profit_factor）轉成 None，確保是合法 JSON。"""
    out = {}
    for k, v in stats.items():
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
        else:
            out[k] = v
    return out


def write_backtest_result(
    result: dict,
    *,
    strategy_id: str,
    start_date,
    end_date,
    initial_cash: int,
    base_dir: str = DEFAULT_BACKTEST_DIR,
) -> Path:
    """落檔回測結果。

    - 結果本體：<base_dir>/<strategy_id>_<run_id>.json
    - LATEST.txt：最新一筆結果的絕對路徑
    - INDEX.tsv：created_at<TAB>strategy_id<TAB>run_id<TAB>start_date<TAB>end_date<TAB>
      final_equity<TAB>total_pnl_bps<TAB>max_drawdown<TAB>path 逐行追加
    回傳結果檔 Path。
    """
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_id = result["run_id"]
    stats = _sanitize_stats(result["statistics"])
    created_at = datetime.now().astimezone().isoformat()
    start_iso = _iso(start_date)
    end_iso = _iso(end_date)

    payload = {
        "run_id": run_id,
        "account_id": result["account_id"],
        "strategy_id": strategy_id,
        "start_date": start_iso,
        "end_date": end_iso,
        "initial_cash": initial_cash,
        "created_at": created_at,
        "equity_curve": [
            {**point, "date": _iso(point["date"])}
            for point in result["equity_curve"]
        ],
        "statistics": stats,
    }

    result_path = (out_dir / f"{strategy_id}_{run_id}.json").resolve()
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    (out_dir / "LATEST.txt").write_text(str(result_path) + "\n", encoding="utf-8")

    index_path = out_dir / "INDEX.tsv"
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(
            f"{created_at}\t{strategy_id}\t{run_id}\t{start_iso}\t{end_iso}\t"
            f"{stats['final_equity']}\t{stats['total_pnl_bps']}\t{stats['max_drawdown']}\t{result_path}\n"
        )

    return result_path
