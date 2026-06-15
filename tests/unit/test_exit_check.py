"""dry_run_exit（單筆出場試算 service）測試：OK 明細 / 無 exit 區塊 / 查無部位。"""
from datetime import date

import pytest

from src.contracts.models import MarketBar, ExitParams, TrendBreakoutParams
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.strategy.registry import StrategyDefinition
from src.application.services import exit_check


def _defn(strategy_id="trend_breakout", with_exit=True):
    exit_params = ExitParams(
        fixed_stop_loss_bps=700, trailing_stop_bps=800, ma_break_period=5,
        ma_break_buffer_bps=0, ma_break_confirm_days=2, time_stop_days=20,
        time_stop_min_return_bps=500,
    ) if with_exit else None
    return StrategyDefinition(
        strategy_id=strategy_id, strategy_version="1.0.0", params=TrendBreakoutParams(),
        exit_params=exit_params, params_hash="sha256:test", order_budget_twd=20000,
    )


def _bar(symbol, d, close):
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK", trade_date=d,
        open=close, high=close, low=close, close=close, volume=1000, amount=1000,
        source="test", source_fetched_at="now", raw_payload_checksum="chk",
    )


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "exit_check.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    ledger.deposit("acc-1", "run-1", 1000000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger("acc-1")
    yield conn, projection
    conn.close()


def _buy(projection, symbol, qty, price, strategy_id, filled_at, is_long_term=0):
    projection.apply_fill_transaction({
        "fill_id": f"f-{symbol}-{strategy_id}", "account_id": "acc-1", "run_id": "run-1",
        "order_id": "ord-1", "execution_key": f"k-{symbol}-{strategy_id}", "symbol": symbol,
        "side": "BUY", "quantity": qty, "price": price, "filled_at": filled_at,
        "strategy_id": strategy_id, "is_long_term": is_long_term,
    })


def test_dry_run_ok_fixed_stop(setup):
    conn, projection = setup
    _buy(projection, "2330", 100, 1000000, "MANUAL", "2026-06-08T09:00:00+08:00")
    SqliteMarketBarRepository(conn).upsert(_bar("2330", date(2026, 6, 10), 929000))

    res = exit_check.dry_run_exit(
        conn, account_id="acc-1", symbol="2330", definition=_defn(),
        as_of_date=date(2026, 6, 10), calendar=ExchangeCalendarsTradingCalendar(),
    )
    assert res["status"] == "OK"
    assert res["detail"]["reason"] == "FIXED_STOP_EXIT"
    assert res["detail"]["fixed_stop"]["hit"] is True
    # MANUAL 部位無 watermark → 保守初始化
    assert res["detail"]["trailing"]["high_from_watermark"] is False


def test_dry_run_no_exit_block(setup):
    conn, projection = setup
    _buy(projection, "2330", 100, 1000000, "MANUAL", "2026-06-08T09:00:00+08:00")
    res = exit_check.dry_run_exit(
        conn, account_id="acc-1", symbol="2330", definition=_defn(with_exit=False),
        as_of_date=date(2026, 6, 10), calendar=ExchangeCalendarsTradingCalendar(),
    )
    assert res["status"] == "NO_EXIT_BLOCK"


def test_dry_run_no_position(setup):
    conn, projection = setup
    res = exit_check.dry_run_exit(
        conn, account_id="acc-1", symbol="9999", definition=_defn(),
        as_of_date=date(2026, 6, 10), calendar=ExchangeCalendarsTradingCalendar(),
    )
    assert res["status"] == "NO_POSITION"


def test_dry_run_not_evaluable_without_bar(setup):
    conn, projection = setup
    _buy(projection, "2330", 100, 1000000, "MANUAL", "2026-06-08T09:00:00+08:00")
    # 未 upsert 任何行情 → 無收盤可評估
    res = exit_check.dry_run_exit(
        conn, account_id="acc-1", symbol="2330", definition=_defn(),
        as_of_date=date(2026, 6, 10), calendar=ExchangeCalendarsTradingCalendar(),
    )
    assert res["status"] == "NOT_EVALUABLE"
