"""版本指紋（P0-T8）：每次 backtest run 落 14 欄，供重現性/差異歸因追溯。

同一指紋（尤其 dataset_hash/corporate_action_version/config_hash 三者）理應重現同結果；
結果不同時，可由哪個欄位變了反推差異來自資料修正、程式變動還是參數變動。
"""
import hashlib
import sqlite3
import subprocess
from datetime import date
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Optional

ENGINE_VERSION = "backtest-engine-1.0"
# FakeBroker 漲跌停/零量 UNFILLED + 零股 3x 滑價模型版本（P0-T7）
COST_MODEL_VERSION = "fake-broker-raw-2.0"


def _sha256(parts: list[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _trading_calendar_version() -> str:
    try:
        return f"exchange_calendars=={pkg_version('exchange_calendars')}:XTAI"
    except PackageNotFoundError:
        return "unknown"


def compute_fingerprint(
    conn: sqlite3.Connection,
    run_id: str,
    strategy_version: str,
    params_hash: str,
    universe_symbols: list[str],
    index_symbols: list[str],
    start_date: date,
    end_date: date,
    slippage_bps: int,
    initial_cash: int,
    manifest_digest: str,
    random_seed: Optional[int] = None,
    universe_policy_version: Optional[str] = None,
) -> dict:
    cursor = conn.cursor()
    symbols = sorted(set(universe_symbols) | set(index_symbols))
    placeholders = ",".join("?" for _ in symbols)

    cursor.execute(
        f"""
        SELECT symbol, trade_date, open, high, low, close, volume, raw_payload_checksum
        FROM market_bars
        WHERE symbol IN ({placeholders}) AND trade_date BETWEEN ? AND ? AND price_basis = 'raw'
        ORDER BY symbol, trade_date
        """,
        (*symbols, start_date.isoformat(), end_date.isoformat()),
    )
    bars = cursor.fetchall()
    dataset_hash = _sha256([
        f"{r['symbol']}|{r['trade_date']}|{r['open']}|{r['high']}|{r['low']}|{r['close']}|{r['volume']}"
        for r in bars
    ])
    source_payload_manifest_hash = _sha256(sorted(r["raw_payload_checksum"] for r in bars))

    cursor.execute(
        f"""
        SELECT action_id, symbol, action_type, ex_date, cash_per_share, stock_ratio, known_at
        FROM corporate_actions
        WHERE symbol IN ({placeholders})
        ORDER BY action_id
        """,
        symbols,
    )
    ca_rows = cursor.fetchall()
    corporate_action_version = _sha256([
        f"{r['action_id']}|{r['symbol']}|{r['action_type']}|{r['ex_date']}|"
        f"{r['cash_per_share']}|{r['stock_ratio']}|{r['known_at']}"
        for r in ca_rows
    ])

    # 固定清單（universe.yaml）無 policy 或 diagnostic policy → 保留 "diagnostic:" 前綴，verdict 一律
    # INVALID（後見之明手挑池不得晉級）。真 PIT policy（如 liquidity-top150-v1）→ 落 policy 前綴的
    # 真 snapshot id，verdict 的 is_diagnostic 閘門（startswith("diagnostic:")）即放行進入正式裁決。
    if universe_policy_version and "diagnostic" not in universe_policy_version.lower():
        universe_snapshot_id = f"{universe_policy_version}:" + _sha256(symbols)[:12]
    else:
        universe_snapshot_id = "diagnostic:" + _sha256(symbols)[:12]
    config_hash = _sha256([str(slippage_bps), str(initial_cash), manifest_digest, params_hash, strategy_version])

    return {
        "run_id": run_id,
        "dataset_hash": dataset_hash,
        "data_cutoff": end_date.isoformat(),
        "universe_snapshot_id": universe_snapshot_id,
        "corporate_action_version": corporate_action_version,
        "cost_model_version": COST_MODEL_VERSION,
        "strategy_version": strategy_version,
        "params_hash": params_hash,
        "code_commit": _git_commit(),
        "random_seed": random_seed,
        "engine_version": ENGINE_VERSION,
        "config_hash": config_hash,
        "trading_calendar_version": _trading_calendar_version(),
        "source_payload_manifest_hash": source_payload_manifest_hash,
    }


def persist_fingerprint(conn: sqlite3.Connection, fp: dict) -> None:
    conn.execute(
        """
        INSERT INTO run_fingerprints (
            run_id, dataset_hash, data_cutoff, universe_snapshot_id, corporate_action_version,
            cost_model_version, strategy_version, params_hash, code_commit, random_seed,
            engine_version, config_hash, trading_calendar_version, source_payload_manifest_hash,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            fp["run_id"], fp["dataset_hash"], fp["data_cutoff"], fp["universe_snapshot_id"],
            fp["corporate_action_version"], fp["cost_model_version"], fp["strategy_version"],
            fp["params_hash"], fp["code_commit"], fp["random_seed"], fp["engine_version"],
            fp["config_hash"], fp["trading_calendar_version"], fp["source_payload_manifest_hash"],
        ),
    )
    conn.commit()
