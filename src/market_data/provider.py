from datetime import date, datetime
from typing import Protocol
import json
from pathlib import Path
from src.contracts.models import MinuteBar

class MarketDataProvider(Protocol):
    def fetch_kbars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[MinuteBar]:
        ...

class FixtureMarketDataProvider:
    def __init__(self, fixtures_dir: str = "fixtures/market"):
        self.fixtures_dir = Path(fixtures_dir)
        self.in_memory_data: dict[str, list[MinuteBar]] = {}

    def set_fixture_data(self, symbol: str, bars: list[MinuteBar]) -> None:
        self.in_memory_data[symbol] = bars

    def fetch_kbars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[MinuteBar]:
        # First check in-memory data
        bars = []
        if symbol in self.in_memory_data:
            bars = self.in_memory_data[symbol]
        else:
            # Try to load from json fixture file
            filepath = self.fixtures_dir / f"{symbol}.json"
            if filepath.exists():
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Assume JSON is list of dicts matching MinuteBar
                loaded_bars = []
                for item in data:
                    # Parse ISO format datetime
                    dt = datetime.fromisoformat(item["time"])
                    loaded_bars.append(
                        MinuteBar(
                            time=dt,
                            open=float(item["open"]),
                            high=float(item["high"]),
                            low=float(item["low"]),
                            close=float(item["close"]),
                            volume=int(item["volume"]),
                            amount=float(item["amount"])
                        )
                    )
                self.in_memory_data[symbol] = loaded_bars
                bars = loaded_bars
        
        # Filter by date range (start_date <= bar_time.date() <= end_date)
        filtered_bars = []
        for bar in bars:
            bar_date = bar.time.date()
            if start_date <= bar_date <= end_date:
                filtered_bars.append(bar)
        return filtered_bars


class ShioajiMarketDataProvider:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._api = None

    def _get_api(self):
        if self._api is None:
            import shioaji as sj
            import os
            is_sim = os.getenv("IS_SIMULATION", "False").lower() in ("true", "1", "t", "y", "yes")
            self._api = sj.Shioaji(simulation=is_sim)
            self._api.login(api_key=self.api_key, secret_key=self.secret_key)
        return self._api

    def fetch_kbars(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[MinuteBar]:
        api = self._get_api()
        
        # Try to find the contract for the symbol
        # First check in Stocks
        try:
            contract = api.Contracts.Stocks[symbol]
        except KeyError:
            raise ValueError(f"Contract not found for symbol: {symbol}")
            
        if not contract:
            raise ValueError(f"Contract not found for symbol: {symbol}")
            
        kbars = api.kbars(
            contract,
            start=start_date.isoformat(),
            end=end_date.isoformat()
        )
        
        if not kbars or len(kbars.ts) == 0:
            return []
            
        import pytz
        from datetime import timezone
        taipei_tz = pytz.timezone("Asia/Taipei")
        
        import os
        is_sim = os.getenv("IS_SIMULATION", "False").lower() in ("true", "1", "t", "y", "yes")
        
        bars = []
        for i in range(len(kbars.ts)):
            # Shioaji ts is nanoseconds since epoch
            ts_s = kbars.ts[i] / 1_000_000_000
            
            if is_sim:
                # In simulation mode, the timestamp naive values represent local Taipei time components
                dt_utc = datetime.fromtimestamp(ts_s, tz=timezone.utc)
                dt = taipei_tz.localize(datetime(
                    dt_utc.year, dt_utc.month, dt_utc.day,
                    dt_utc.hour, dt_utc.minute, dt_utc.second
                ))
            else:
                dt = datetime.fromtimestamp(ts_s, tz=timezone.utc).astimezone(taipei_tz)
            
            bars.append(
                MinuteBar(
                    time=dt,
                    open=float(kbars.Open[i]),
                    high=float(kbars.High[i]),
                    low=float(kbars.Low[i]),
                    close=float(kbars.Close[i]),
                    volume=int(kbars.Volume[i]),
                    amount=float(kbars.Amount[i])
                )
            )
        return bars

