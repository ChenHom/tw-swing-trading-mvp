"""FinMind provider 單元測試（P0-T2）。全部 mock HTTP，無真實網路呼叫。"""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock

from src.market_data.finmind_provider import FinMindProvider, load_finmind_token


def _resp(status_code=200, data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = {"data": data or []}
    r.raise_for_status = MagicMock()
    return r


@patch("src.market_data.finmind_provider.requests.get")
def test_fetch_stock_names_filters_and_maps(mock_get):
    mock_get.return_value = _resp(data=[
        {"stock_id": "2330", "stock_name": "台積電", "type": "twse"},
        {"stock_id": "00981A", "stock_name": "主動統一台股增長", "type": "twse"},  # ETF
        {"stock_id": "TAIEX", "stock_name": "發行量加權股價指數", "type": "twse"},  # 非數字開頭→濾掉
        {"stock_id": "3481", "stock_name": "", "type": "twse"},  # 空名→跳過
    ])
    names = FinMindProvider().fetch_stock_names()
    assert names == {"2330": "台積電", "00981A": "主動統一台股增長"}
    assert "TAIEX" not in names and "3481" not in names


def test_load_token_prefers_env(monkeypatch):
    monkeypatch.setenv("FINMIND_API_TOKEN", "env-token")
    assert load_finmind_token() == "env-token"


def test_load_token_missing_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("FINMIND_API_TOKEN", raising=False)
    assert load_finmind_token(openclaw_env_path=str(tmp_path / "nope.env")) is None


@patch("src.market_data.finmind_provider.requests.get")
def test_fetch_raw_price_maps_fields(mock_get):
    mock_get.return_value = _resp(data=[{
        "date": "2022-01-03", "stock_id": "2330",
        "Trading_Volume": 73703302, "Trading_money": 46249716919,
        "open": 619.0, "max": 632.0, "min": 618.0, "close": 631.0,
        "spread": 16.0, "Trading_turnover": 88508,
    }])
    provider = FinMindProvider(token="t")
    bars = provider.fetch_raw_price("2330", date(2022, 1, 1), date(2022, 1, 31), exchange="TSE")

    assert len(bars) == 1
    bar = bars[0]
    assert bar.trade_date == date(2022, 1, 3)
    assert bar.open == 6190000
    assert bar.high == 6320000
    assert bar.low == 6180000
    assert bar.close == 6310000
    assert bar.volume == 73703302
    assert bar.amount == 46249716919
    assert bar.price_basis == "raw"
    assert bar.adjustment_factor == 1.0
    assert bar.source == "finmind:TaiwanStockPrice"


@patch("src.market_data.finmind_provider.requests.get")
def test_tse_index_aliased_to_taiex_but_stored_under_tse(mock_get):
    # 加權指數內部碼 "TSE" → FinMind data_id "TAIEX"；抓取換 id、落帳 symbol 仍為 "TSE"。
    mock_get.return_value = _resp(data=[{
        "date": "2024-01-02", "stock_id": "TAIEX",
        "Trading_Volume": 6411778806, "Trading_money": 301290668897,
        "open": 17939.79, "max": 17956.74, "min": 17784.97, "close": 17853.76,
        "spread": -77.05, "Trading_turnover": 2267660,
    }])
    provider = FinMindProvider(token="t")
    bars = provider.fetch_raw_price("TSE", date(2024, 1, 1), date(2024, 1, 31), exchange="TSE", instrument_type="INDEX")

    assert mock_get.call_args.kwargs["params"]["data_id"] == "TAIEX"  # API 用 TAIEX
    assert len(bars) == 1
    assert bars[0].symbol == "TSE"  # 落帳仍 TSE（策略 index 濾網/ live 一致）
    assert bars[0].close == 178537600


@patch("src.market_data.finmind_provider.requests.get")
def test_402_returns_empty_without_retry(mock_get):
    mock_get.return_value = _resp(status_code=402)
    provider = FinMindProvider(token="t")
    bars = provider.fetch_raw_price("2330", date(2022, 1, 1), date(2022, 1, 31), exchange="TSE")
    assert bars == []
    assert mock_get.call_count == 1


@patch("src.market_data.finmind_provider.time.sleep", return_value=None)
@patch("src.market_data.finmind_provider.requests.get")
def test_429_retries_with_backoff(mock_get, mock_sleep):
    mock_get.side_effect = [_resp(status_code=429), _resp(status_code=429), _resp(data=[])]
    provider = FinMindProvider(token="t")
    bars = provider.fetch_raw_price("2330", date(2022, 1, 1), date(2022, 1, 31), exchange="TSE")
    assert bars == []
    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 2


@patch("src.market_data.finmind_provider.requests.get")
def test_fetch_dividend_events_cash_only(mock_get):
    mock_get.return_value = _resp(data=[{
        "date": "2018-07-01", "stock_id": "2330",
        "StockEarningsDistribution": 0.0, "StockExDividendTradingDate": "",
        "CashEarningsDistribution": 8.0, "CashExDividendTradingDate": "2018-06-25",
        "AnnouncementDate": "2018-06-07", "AnnouncementTime": "17:31:43",
    }])
    provider = FinMindProvider(token="t")
    actions = provider.fetch_dividend_events("2330", date(2018, 1, 1), date(2018, 12, 31))

    assert len(actions) == 1
    a = actions[0]
    assert a["action_type"] == "CASH_DIVIDEND"
    assert a["ex_date"] == "2018-06-25"
    assert a["cash_per_share"] == 80000
    assert a["known_at"] == "2018-06-07T17:31:43+08:00"
    assert a["effective_date"] == "2018-06-25"


@patch("src.market_data.finmind_provider.requests.get")
def test_fetch_dividend_events_skips_all_zero_rows(mock_get):
    mock_get.return_value = _resp(data=[{
        "date": "2026-01-01", "stock_id": "2330",
        "StockEarningsDistribution": 0.0, "StockExDividendTradingDate": "",
        "CashEarningsDistribution": 0.0, "CashExDividendTradingDate": "",
        "AnnouncementDate": "2026-01-01", "AnnouncementTime": "00:00:00",
    }])
    provider = FinMindProvider(token="t")
    actions = provider.fetch_dividend_events("2330", date(2026, 1, 1), date(2026, 1, 31))
    assert actions == []
