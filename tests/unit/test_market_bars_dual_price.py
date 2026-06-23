"""雙價（raw/adjusted）canonical bar 資料模型單元測試（P0-T1）。

驗收：
  ① (symbol, trade_date, price_basis) 唯一 —— 同日 raw 與 adjusted 兩筆可共存，
     重複 upsert 同一 basis 為就地更新而非新增列。
  ② adjusted = raw * adjustment_factor 自洽。
"""
import pytest
from datetime import date

from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.contracts.models import MarketBar


@pytest.fixture
def research_repo(tmp_path):
    db_path = str(tmp_path / "research.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    return SqliteMarketBarRepository(conn)


def _bar(price_basis: str, close: int, adjustment_factor: float = 1.0) -> MarketBar:
    # source 依 price_basis 區分（FinMind raw/adjusted 本來就是不同 dataset 端點），
    # 同日 raw+adjusted 兩筆才不會撞到 market_bars 既有 PK (symbol, exchange, trade_date, source)。
    return MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 1, 5),
        open=close, high=close, low=close, close=close,
        volume=1000, amount=close * 1000 // 10000,
        source=f"finmind:{price_basis}", source_fetched_at="2026-01-06T00:00:00+08:00",
        raw_payload_checksum="abc123",
        price_basis=price_basis, adjustment_factor=adjustment_factor,
    )


def test_raw_and_adjusted_coexist_same_date(research_repo):
    research_repo.upsert_canonical(_bar("raw", 5000000))
    research_repo.upsert_canonical(_bar("adjusted", 4950000, adjustment_factor=0.99))

    raw = research_repo.find_by_basis("2330", date(2026, 1, 5), "raw")
    adj = research_repo.find_by_basis("2330", date(2026, 1, 5), "adjusted")
    assert raw is not None and adj is not None
    assert raw.close == 5000000
    assert adj.close == 4950000


def test_duplicate_upsert_updates_in_place(research_repo):
    research_repo.upsert_canonical(_bar("raw", 5000000))
    research_repo.upsert_canonical(_bar("raw", 5100000))  # 同 basis 重複寫入 -> 更新, 非新增

    cursor = research_repo.conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM market_bars WHERE symbol = ? AND trade_date = ?",
        ("2330", "2026-01-05"),
    )
    assert cursor.fetchone()["cnt"] == 1

    raw = research_repo.find_by_basis("2330", date(2026, 1, 5), "raw")
    assert raw.close == 5100000


def test_adjusted_equals_raw_times_factor(research_repo):
    raw_close = 5000000
    factor = 0.97
    research_repo.upsert_canonical(_bar("raw", raw_close))
    research_repo.upsert_canonical(_bar("adjusted", round(raw_close * factor), adjustment_factor=factor))

    raw = research_repo.find_by_basis("2330", date(2026, 1, 5), "raw")
    adj = research_repo.find_by_basis("2330", date(2026, 1, 5), "adjusted")
    assert adj.close == round(raw.close * adj.adjustment_factor)
