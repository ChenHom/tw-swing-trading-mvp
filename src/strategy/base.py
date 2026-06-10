from datetime import date
from typing import Protocol, Optional
from pydantic import BaseModel
from src.contracts.models import DailySignalBundle, MarketBar
from src.market_data.repository import PointInTimeMarketData

class PositionSnapshot(BaseModel):
    symbol: str
    quantity: int
    entry_price: int  # price x 10000

class PortfolioSnapshot(BaseModel):
    available_cash: int
    positions: dict[str, PositionSnapshot]

class SignalGenerationContext(BaseModel):
    as_of_date: date
    strategy_id: str
    strategy_version: str
    run_id: str
    approval_id: str
    params_hash: str

class SignalGenerator(Protocol):
    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot,
    ) -> DailySignalBundle:
        ...
