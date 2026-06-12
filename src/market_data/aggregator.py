import datetime
from datetime import date, time
from typing import Optional
import hashlib
from src.contracts.models import MinuteBar, MarketBar

class DailyBarAggregator:
    @staticmethod
    def aggregate(
        symbol: str,
        exchange: str,
        instrument_type: str,
        trade_date: date,
        minute_bars: list[MinuteBar],
        source: str,
        source_fetched_at: str,
        raw_payload_checksum: str
    ) -> Optional[MarketBar]:
        # Filter regular session: 09:00:00 to 13:30:00
        regular_bars = []
        start_time = time(9, 0, 0)
        end_time = time(13, 30, 0)
        
        for bar in minute_bars:
            bar_time = bar.time.time()
            if start_time <= bar_time <= end_time:
                regular_bars.append(bar)
                
        if not regular_bars:
            return None
            
        # Sort bars chronologically
        regular_bars.sort(key=lambda x: x.time)
        
        open_price = regular_bars[0].open
        close_price = regular_bars[-1].close
        high_price = max(bar.high for bar in regular_bars)
        low_price = min(bar.low for bar in regular_bars)
        total_volume = sum(bar.volume for bar in regular_bars)
        total_amount = sum(bar.amount for bar in regular_bars)
        
        # Scale to integer representation
        open_scaled = int(round(open_price * 10000))
        high_scaled = int(round(high_price * 10000))
        low_scaled = int(round(low_price * 10000))
        close_scaled = int(round(close_price * 10000))
        volume_scaled = int(total_volume)
        amount_scaled = int(round(total_amount))
        
        return MarketBar(
            symbol=symbol,
            exchange=exchange,
            instrument_type=instrument_type,
            trade_date=trade_date,
            open=open_scaled,
            high=high_scaled,
            low=low_scaled,
            close=close_scaled,
            volume=volume_scaled,
            amount=amount_scaled,
            source=source,
            source_timezone="Asia/Taipei",
            is_complete=1,
            source_fetched_at=source_fetched_at,
            raw_payload_checksum=raw_payload_checksum
        )

class MarketBarValidator:
    @staticmethod
    def validate(bar: MarketBar) -> None:
        errors = []
        if bar.open <= 0:
            errors.append(f"Open price must be positive, got {bar.open}")
        if bar.high <= 0:
            errors.append(f"High price must be positive, got {bar.high}")
        if bar.low <= 0:
            errors.append(f"Low price must be positive, got {bar.low}")
        if bar.close <= 0:
            errors.append(f"Close price must be positive, got {bar.close}")
        # Index bars have no tradable volume/amount; zero or missing is valid (§2.3).
        if bar.instrument_type != "INDEX":
            if bar.volume < 0:
                errors.append(f"Volume cannot be negative, got {bar.volume}")
            if bar.amount < 0:
                errors.append(f"Amount cannot be negative, got {bar.amount}")
            
        if bar.high < max(bar.open, bar.close, bar.low):
            errors.append(f"High ({bar.high}) must be >= max(open={bar.open}, close={bar.close}, low={bar.low})")
        if bar.low > min(bar.open, bar.close, bar.high):
            errors.append(f"Low ({bar.low}) must be <= min(open={bar.open}, close={bar.close}, high={bar.high})")
            
        if errors:
            raise ValueError(f"MARKET_BAR_INVALID: {'; '.join(errors)}")
