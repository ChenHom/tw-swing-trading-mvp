import uuid
from datetime import date
from src.market_data.repository import SqliteMarketBarRepository

class FakeBroker:
    def __init__(self, repository: SqliteMarketBarRepository):
        self.repository = repository

    def execute_orders(self, orders: list[dict], execution_date: date, slippage_bps: int = 10) -> tuple[list[dict], str]:
        if not orders:
            return [], "FILLED"
            
        for order in orders:
            symbol = order["symbol"]
            bar = self.repository.find(symbol, execution_date)
            if not bar:
                return [], "WAITING_MARKET_DATA"
                
        fills = []
        for order in orders:
            symbol = order["symbol"]
            action = order["action"]
            quantity = order["quantity"]
            
            bar = self.repository.find(symbol, execution_date)
            # Safe check because we verified in previous loop
            assert bar is not None
            open_price = bar.open
            
            if "long" in action:
                if "open" in action or "increase" in action:
                    side = "BUY"
                    fill_price = int(round(open_price * (1 + slippage_bps / 10000)))
                elif "close" in action:
                    side = "SELL"
                    fill_price = int(round(open_price * (1 - slippage_bps / 10000)))
                else:
                    raise ValueError(f"Unknown action: {action}")
            else:
                raise ValueError(f"Unknown action: {action}")
                
            fill_id = f"fill-{uuid.uuid4().hex[:8]}"
            fills.append({
                "fill_id": fill_id,
                "signal_id": order["signal_id"],
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": fill_price,
                "filled_at": f"{execution_date.isoformat()}T09:00:00+08:00",
                "status": "FILLED"
            })
            
        return fills, "FILLED"
