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
