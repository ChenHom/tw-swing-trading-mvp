"""A1：raw + 股利事件 → back-adjusted 'adj' 序列。"""
from datetime import date

from src.contracts.models import MarketBar
from src.market_data.adjusted_series import build_adjusted_bars


def _bar(d: str, close: float) -> MarketBar:
    scaled = int(round(close * 10000))
    return MarketBar(
        symbol="2412", exchange="TSE", instrument_type="STOCK",
        trade_date=date.fromisoformat(d),
        open=scaled, high=scaled, low=scaled, close=scaled,
        volume=1000, amount=int(close * 1000),
        source="finmind:TaiwanStockPrice", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk",
        price_basis="raw", adjustment_factor=1.0,
    )


def test_cash_dividend_back_adjustment_removes_ex_date_gap():
    # 120 元配 6 元：ex 日 raw 跳空 -5%；adj 序列中 ex 日前的價格乘 (120-6)/120=0.95
    bars = [_bar("2026-06-01", 120.0), _bar("2026-06-02", 120.0),
            _bar("2026-06-03", 114.0), _bar("2026-06-04", 115.0)]
    actions = [{
        "action_id": "a1", "symbol": "2412", "action_type": "CASH_DIVIDEND",
        "ex_date": "2026-06-03", "cash_per_share": 60000, "stock_ratio": None,
    }]

    adj = build_adjusted_bars(bars, actions)

    assert [b.price_basis for b in adj] == ["adj"] * 4
    # ex 日之前打 0.95；ex 日（含）以後 = raw
    assert adj[0].close == int(round(1200000 * 0.95))
    assert adj[1].close == int(round(1200000 * 0.95))
    assert adj[2].close == 1140000
    assert adj[3].close == 1150000
    # 除息缺口消失：adj 序列 06-02 -> 06-03 報酬 = 0（1140000/1140000）
    assert adj[1].close == adj[2].close
    assert adj[0].adjustment_factor == 0.95
    assert adj[3].adjustment_factor == 1.0


def test_stock_dividend_and_chained_events_compound():
    # 先配股 10%（factor 1/1.1），後配息 5 元於 100 元（factor 0.95）——最早的 bar 兩者皆吃
    bars = [_bar("2026-01-05", 110.0), _bar("2026-03-02", 100.0),
            _bar("2026-03-03", 95.0), _bar("2026-06-01", 100.0)]
    actions = [
        {"action_id": "s1", "symbol": "2412", "action_type": "STOCK_DIVIDEND",
         "ex_date": "2026-03-03", "cash_per_share": None, "stock_ratio": 0.1},
        {"action_id": "c1", "symbol": "2412", "action_type": "CASH_DIVIDEND",
         "ex_date": "2026-06-01", "cash_per_share": 50000, "stock_ratio": None},
    ]

    adj = build_adjusted_bars(bars, actions)

    f_stock = 1 / 1.1
    f_cash = (950000 - 50000) / 950000  # 基準＝ex 日前一 bar（03-03 收 95 元）
    assert adj[0].close == int(round(1100000 * f_stock * f_cash))
    assert adj[1].close == int(round(1000000 * f_stock * f_cash))
    assert adj[2].close == int(round(950000 * f_cash))
    assert adj[3].close == 1000000


def test_event_before_data_window_is_skipped():
    bars = [_bar("2026-06-01", 100.0)]
    actions = [{"action_id": "old", "symbol": "2412", "action_type": "CASH_DIVIDEND",
                "ex_date": "2020-07-01", "cash_per_share": 50000, "stock_ratio": None}]
    adj = build_adjusted_bars(bars, actions)
    assert adj[0].close == 1000000
    assert adj[0].adjustment_factor == 1.0
