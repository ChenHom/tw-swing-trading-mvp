from src.market_data.repository import PointInTimeMarketData


def index_above_ma(market_data: PointInTimeMarketData, index_symbol: str, period: int) -> bool:
    """大盤濾網：加權指數收盤高於 period 日均線。歷史不足視為濾網不通過（不進場）。"""
    history = market_data.history(index_symbol, limit=period)
    if len(history) < period:
        return False
    closes = [bar.close for bar in history]
    sma = sum(closes) / period
    return closes[-1] > sma
