"""版本指紋（P0-T8）：同輸入→同指紋（重現性）；資料變動→指紋變動（敏感度）。"""
import pytest
from datetime import date

from src.portfolio.db import init_db, get_db_connection
from src.contracts.models import MarketBar
from src.market_data.repository import SqliteMarketBarRepository
from src.application.runners.fingerprint import compute_fingerprint, persist_fingerprint


@pytest.fixture
def conn(tmp_path):
    db_file = tmp_path / "test_fingerprint.db"
    init_db(str(db_file))
    c = get_db_connection(str(db_file))
    yield c
    c.close()


def _seed_bar(repo, symbol, trade_date, close, checksum="chk"):
    repo.upsert(MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK",
        trade_date=trade_date,
        open=close, high=close, low=close, close=close,
        volume=100, amount=close * 100 // 10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum=checksum
    ))


def _args(**overrides):
    base = dict(
        run_id="bt-fixed",
        strategy_version="1.0.0",
        params_hash="sha256:abc",
        universe_symbols=["2330"],
        index_symbols=["0050"],
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        slippage_bps=10,
        initial_cash=1000000,
        manifest_digest="sha256:digest",
    )
    base.update(overrides)
    return base


def test_fingerprint_reproducible_for_same_inputs(conn):
    repo = SqliteMarketBarRepository(conn)
    _seed_bar(repo, "2330", date(2026, 6, 2), 1000000)
    _seed_bar(repo, "0050", date(2026, 6, 2), 500000)

    fp1 = compute_fingerprint(conn, **_args())
    fp2 = compute_fingerprint(conn, **_args())
    assert fp1 == fp2
    assert len(fp1) == 14


def test_fingerprint_changes_when_dataset_changes(conn):
    repo = SqliteMarketBarRepository(conn)
    _seed_bar(repo, "2330", date(2026, 6, 2), 1000000)

    fp_before = compute_fingerprint(conn, **_args())
    _seed_bar(repo, "2330", date(2026, 6, 2), 1010000)  # 改收盤價
    fp_after = compute_fingerprint(conn, **_args())

    assert fp_before["dataset_hash"] != fp_after["dataset_hash"]
    # 其餘與資料無關的欄位不受影響
    assert fp_before["engine_version"] == fp_after["engine_version"]
    assert fp_before["cost_model_version"] == fp_after["cost_model_version"]


def test_fingerprint_changes_when_corporate_action_added(conn):
    repo = SqliteMarketBarRepository(conn)
    _seed_bar(repo, "2330", date(2026, 6, 2), 1000000)
    fp_before = compute_fingerprint(conn, **_args())

    conn.execute(
        """
        INSERT INTO corporate_actions (action_id, symbol, action_type, ex_date, cash_per_share, source, created_at)
        VALUES ('ca-1', '2330', 'CASH_DIV', '2026-06-03', 50000, 'manual', datetime('now'))
        """
    )
    conn.commit()
    fp_after = compute_fingerprint(conn, **_args())

    assert fp_before["corporate_action_version"] != fp_after["corporate_action_version"]


def test_persist_fingerprint_writes_row(conn):
    repo = SqliteMarketBarRepository(conn)
    _seed_bar(repo, "2330", date(2026, 6, 2), 1000000)
    fp = compute_fingerprint(conn, **_args())
    persist_fingerprint(conn, fp)

    row = conn.execute("SELECT * FROM run_fingerprints WHERE run_id = ?", (fp["run_id"],)).fetchone()
    assert row is not None
    assert row["dataset_hash"] == fp["dataset_hash"]
