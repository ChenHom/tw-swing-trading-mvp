"""market backfill-history（P0-T9）：FinMind 主 + TWSE 補缺口、剔除清單、股利事件、可續跑。"""
import pytest
from datetime import date
from unittest.mock import MagicMock

from src.portfolio.db import get_db_connection
from src.contracts.models import MarketBar
from src.cli.market import cmd_market_backfill_history
from src.config import SymbolConfig


def _bar(symbol, trade_date, source, open_=1000000, checksum="chk"):
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK", trade_date=trade_date,
        open=open_, high=open_ + 10000, low=open_ - 10000, close=open_,
        volume=100, amount=10000, source=source, source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum=checksum,
    )


@pytest.fixture
def backfill_env(tmp_path):
    from src.calendar.calendar import ExchangeCalendarsTradingCalendar
    sessions = ExchangeCalendarsTradingCalendar().sessions_between(date(2026, 6, 8), date(2026, 6, 12))
    gap_date = sessions[1]  # FinMind 故意缺這天，交由 TWSE 補

    class FakeFinMind:
        def __init__(self, *a, **kw):
            pass

        def fetch_raw_price(self, symbol, start_date, end_date, exchange, instrument_type="STOCK"):
            if symbol == "3231":
                return []  # 整窗無資料
            return [_bar(symbol, d, "finmind:TaiwanStockPrice") for d in sessions if d != gap_date]

        def fetch_dividend_events(self, symbol, start_date, end_date):
            if symbol != "2330":
                return []
            return [{
                "action_id": f"finmind-{symbol}-CASH-{gap_date.isoformat()}",
                "symbol": symbol, "action_type": "CASH_DIVIDEND", "ex_date": gap_date.isoformat(),
                "cash_per_share": 50000, "stock_ratio": None, "source": "finmind:TaiwanStockDividend",
                "effective_date": gap_date.isoformat(), "known_at": "2026-06-01T00:00:00+08:00",
            }]

    class FakeTwse:
        def __init__(self, *a, **kw):
            pass

        def fetch_range(self, symbol, start_date, end_date, exchange, instrument_type="STOCK"):
            if symbol == "3231":
                return []  # 兩個來源都沒有 → 真正剔除
            return [_bar(symbol, gap_date, "twse:STOCK_DAY", open_=1010000, checksum="twse-chk")]

    db_path = str(tmp_path / "research.db")
    mock_settings = MagicMock()
    mock_settings.universe.symbols = [SymbolConfig(code="2330", exchange="TSE", instrument_type="STOCK")]
    mock_settings.universe.indices = []

    return sessions, gap_date, db_path, mock_settings, FakeFinMind, FakeTwse


class _Args:
    def __init__(self, start_date, end_date, symbols, db, source="auto"):
        self.start_date = start_date
        self.end_date = end_date
        self.symbols = symbols
        self.db = db
        self.source = source


def test_backfill_history_gap_fills_from_twse_and_excludes_zero_data_symbol(backfill_env, monkeypatch, capsys):
    sessions, gap_date, db_path, mock_settings, FakeFinMind, FakeTwse = backfill_env
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    monkeypatch.setattr("src.market_data.finmind_provider.FinMindProvider", FakeFinMind)
    monkeypatch.setattr("src.market_data.twse_provider.TwseProvider", FakeTwse)

    args = _Args(sessions[0].isoformat(), sessions[-1].isoformat(), "2330,3231", db_path)
    cmd_market_backfill_history(args)

    captured = capsys.readouterr()
    assert "剔除清單（整窗無任何資料）：3231" in captured.out

    conn = get_db_connection(db_path)
    rows = conn.execute(
        "SELECT trade_date, source, open FROM market_bars WHERE symbol = '2330' ORDER BY trade_date"
    ).fetchall()
    assert [r["trade_date"] for r in rows] == [d.isoformat() for d in sessions]
    gap_row = next(r for r in rows if r["trade_date"] == gap_date.isoformat())
    assert gap_row["source"] == "twse:STOCK_DAY"  # 缺口確實由 TWSE 補上
    assert gap_row["open"] == 1010000

    assert conn.execute("SELECT COUNT(*) AS c FROM market_bars WHERE symbol = '3231'").fetchone()["c"] == 0

    div_row = conn.execute(
        "SELECT action_type, cash_per_share FROM corporate_actions WHERE symbol = '2330'"
    ).fetchone()
    assert div_row["action_type"] == "CASH_DIVIDEND"
    assert div_row["cash_per_share"] == 50000
    conn.close()


def test_backfill_history_is_idempotent_on_rerun(backfill_env, monkeypatch, capsys):
    sessions, gap_date, db_path, mock_settings, FakeFinMind, FakeTwse = backfill_env
    monkeypatch.setattr("src.cli.common.get_settings", lambda: mock_settings)
    monkeypatch.setattr("src.market_data.finmind_provider.FinMindProvider", FakeFinMind)
    monkeypatch.setattr("src.market_data.twse_provider.TwseProvider", FakeTwse)

    args = _Args(sessions[0].isoformat(), sessions[-1].isoformat(), "2330", db_path)
    cmd_market_backfill_history(args)
    cmd_market_backfill_history(args)  # 可續跑：重跑不應拋錯或重複落帳

    conn = get_db_connection(db_path)
    assert conn.execute("SELECT COUNT(*) AS c FROM market_bars WHERE symbol = '2330'").fetchone()["c"] == len(sessions)
    assert conn.execute("SELECT COUNT(*) AS c FROM corporate_actions WHERE symbol = '2330'").fetchone()["c"] == 1
    conn.close()
