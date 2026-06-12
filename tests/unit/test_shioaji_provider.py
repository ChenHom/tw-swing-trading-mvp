import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
import pytz
from src.market_data.provider import ShioajiMarketDataProvider

def test_shioaji_market_data_provider_success(monkeypatch):
    monkeypatch.setenv("IS_SIMULATION", "False")
    # 1. Create a mock KBar object
    mock_kbars = MagicMock()
    # Shioaji timestamps are in Unix nanoseconds
    # Let's say: 2026-06-10 09:00:00 Asia/Taipei
    # 2026-06-10 09:00:00 Asia/Taipei is 2026-06-10 01:00:00 UTC
    utc_dt = datetime(2026, 6, 10, 1, 0, 0, tzinfo=timezone.utc)
    ts_ns = int(utc_dt.timestamp() * 1_000_000_000)
    
    mock_kbars.ts = [ts_ns]
    mock_kbars.Open = [100.0]
    mock_kbars.High = [101.5]
    mock_kbars.Low = [99.5]
    mock_kbars.Close = [101.0]
    mock_kbars.Volume = [10]
    mock_kbars.Amount = [1000.0]
    
    # 2. Patch Shioaji
    with patch("shioaji.Shioaji") as mock_sj_class:
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        
        # Mock contract lookup
        mock_contract = MagicMock()
        mock_api.Contracts.Stocks = {"2330": mock_contract}
        
        # Mock kbars method
        mock_api.kbars.return_value = mock_kbars
        
        provider = ShioajiMarketDataProvider(api_key="test-api-key", secret_key="test-secret")
        bars = provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))
        
        mock_api.login.assert_called_once()
        _, kwargs = mock_api.login.call_args
        assert kwargs.get("api_key") == "test-api-key"
        assert kwargs.get("secret_key") == "test-secret"
        
        # Verify kbars query
        mock_api.kbars.assert_called_once_with(
            mock_contract,
            start="2026-06-10",
            end="2026-06-10",
            timeout=60000
        )
        
        # Verify results
        assert len(bars) == 1
        assert bars[0].open == 100.0
        assert bars[0].high == 101.5
        assert bars[0].low == 99.5
        assert bars[0].close == 101.0
        assert bars[0].volume == 10
        assert bars[0].amount == 1000.0
        
        # Timezone check (must be localized to Asia/Taipei)
        assert bars[0].time.tzinfo is not None
        assert "Taipei" in str(bars[0].time.tzinfo)
        assert bars[0].time.hour == 9
        assert bars[0].time.minute == 0


def test_shioaji_market_data_provider_simulation_success(monkeypatch):
    monkeypatch.setenv("IS_SIMULATION", "True")
    mock_kbars = MagicMock()
    # In simulation, 09:00:00 local is returned as 09:00:00 UTC timestamp components
    utc_dt = datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)
    ts_ns = int(utc_dt.timestamp() * 1_000_000_000)
    
    mock_kbars.ts = [ts_ns]
    mock_kbars.Open = [100.0]
    mock_kbars.High = [101.5]
    mock_kbars.Low = [99.5]
    mock_kbars.Close = [101.0]
    mock_kbars.Volume = [10]
    mock_kbars.Amount = [1000.0]
    
    with patch("shioaji.Shioaji") as mock_sj_class:
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        mock_contract = MagicMock()
        mock_api.Contracts.Stocks = {"2330": mock_contract}
        mock_api.kbars.return_value = mock_kbars
        
        provider = ShioajiMarketDataProvider(api_key="test-api-key", secret_key="test-secret")
        bars = provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))
        
        assert len(bars) == 1
        assert bars[0].time.tzinfo is not None
        assert "Taipei" in str(bars[0].time.tzinfo)
        assert bars[0].time.hour == 9
        assert bars[0].time.minute == 0

def test_shioaji_market_data_provider_missing_contract():
    with patch("shioaji.Shioaji") as mock_sj_class:
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        mock_api.Contracts.Stocks = {} # No stocks
        
        provider = ShioajiMarketDataProvider(api_key="test-api-key", secret_key="test-secret")
        with pytest.raises(ValueError, match="Contract not found"):
            provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))

def test_shioaji_market_data_provider_empty_kbars():
    with patch("shioaji.Shioaji") as mock_sj_class:
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        
        mock_contract = MagicMock()
        mock_api.Contracts.Stocks = {"2330": mock_contract}
        mock_api.kbars.return_value = None # Empty result
        
        provider = ShioajiMarketDataProvider(api_key="test-api-key", secret_key="test-secret")
        bars = provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))
        assert len(bars) == 0


def test_shioaji_kbars_retries_on_transient_timeout(monkeypatch):
    """登入後首筆 kbars 偶發逾時：逾時最多重試 3 次，其他例外直接拋出。"""
    monkeypatch.setenv("IS_SIMULATION", "False")
    ts_ns = 1781305200 * 1_000_000_000
    mock_kbars = MagicMock()
    mock_kbars.ts = [ts_ns]
    mock_kbars.Open = [100.0]
    mock_kbars.High = [101.5]
    mock_kbars.Low = [99.5]
    mock_kbars.Close = [101.0]
    mock_kbars.Volume = [10]
    mock_kbars.Amount = [1000.0]

    with patch("shioaji.Shioaji") as mock_sj_class, patch("time.sleep"):
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        mock_contract = MagicMock()
        mock_api.Contracts.Stocks = {"2330": mock_contract}

        # 前兩次逾時，第三次成功
        mock_api.kbars.side_effect = [TimeoutError("t1"), TimeoutError("t2"), mock_kbars]
        provider = ShioajiMarketDataProvider(api_key="k", secret_key="s")
        bars = provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))
        assert len(bars) == 1
        assert mock_api.kbars.call_count == 3

    with patch("shioaji.Shioaji") as mock_sj_class, patch("time.sleep"):
        mock_api = MagicMock()
        mock_sj_class.return_value = mock_api
        mock_api.Contracts.Stocks = {"2330": MagicMock()}

        # 連續逾時 3 次後拋出
        mock_api.kbars.side_effect = TimeoutError("always")
        provider = ShioajiMarketDataProvider(api_key="k", secret_key="s")
        import pytest as _pytest
        with _pytest.raises(TimeoutError):
            provider.fetch_kbars("2330", date(2026, 6, 10), date(2026, 6, 10))
        assert mock_api.kbars.call_count == 3
