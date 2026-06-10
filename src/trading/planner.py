from dataclasses import dataclass
import math
from src.contracts.models import SignalItem, LimitsInfo

@dataclass
class PortfolioState:
    available_cash: int
    positions: dict[str, int]
    daily_buy_value_spent: int

class OrderPlanner:
    @staticmethod
    def plan_order(
        signal: SignalItem,
        portfolio: PortfolioState,
        strategy_budget: int,
        manifest_limits: LimitsInfo
    ) -> list[dict]:
        symbol = signal.symbol
        action = signal.action.upper()
        ref_price = signal.reference_price
        
        current_qty = portfolio.positions.get(symbol, 0)
        
        orders = []
        
        if action == "BUY":
            remaining_daily_limit = manifest_limits.max_daily_buy_value - portfolio.daily_buy_value_spent
            if remaining_daily_limit <= 0:
                raise ValueError(f"DAILY_BUY_LIMIT_EXCEEDED: Daily limit spent {portfolio.daily_buy_value_spent}, max is {manifest_limits.max_daily_buy_value}")
                
            order_budget = min(
                strategy_budget,
                manifest_limits.max_order_value,
                remaining_daily_limit,
                portfolio.available_cash
            )
            
            if order_budget <= 0 or order_budget < ref_price:
                raise ValueError(f"INSUFFICIENT_CASH: cash {portfolio.available_cash}, budget {order_budget}, price {ref_price}")
                
            quantity = int(math.floor(order_budget / ref_price))
            if quantity <= 0:
                raise ValueError(f"INSUFFICIENT_CASH: quantity to buy is 0 under budget {order_budget} and price {ref_price}")
                
            is_new_position = (current_qty == 0)
            if is_new_position:
                active_positions_count = sum(1 for qty in portfolio.positions.values() if qty > 0)
                if active_positions_count >= manifest_limits.max_open_positions:
                    raise ValueError(f"MAX_OPEN_POSITIONS_EXCEEDED: Open positions count {active_positions_count}, max is {manifest_limits.max_open_positions}")
                    
            order_action = "open_long" if is_new_position else "increase_long"
            
            if quantity >= 1000:
                board_qty = (quantity // 1000) * 1000
                odd_qty = quantity % 1000
                
                orders.append({
                    "signal_id": signal.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": board_qty,
                    "is_odd_lot": False,
                    "estimated_price": ref_price
                })
                if odd_qty > 0:
                    orders.append({
                        "signal_id": signal.signal_id,
                        "symbol": symbol,
                        "action": order_action,
                        "quantity": odd_qty,
                        "is_odd_lot": True,
                        "estimated_price": ref_price
                    })
            else:
                orders.append({
                    "signal_id": signal.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": quantity,
                    "is_odd_lot": True,
                    "estimated_price": ref_price
                })
                
        elif action == "SELL":
            if current_qty <= 0:
                return []
                
            order_action = "close_long"
            quantity = current_qty
            
            if quantity >= 1000:
                board_qty = (quantity // 1000) * 1000
                odd_qty = quantity % 1000
                
                orders.append({
                    "signal_id": signal.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": board_qty,
                    "is_odd_lot": False,
                    "estimated_price": ref_price
                })
                if odd_qty > 0:
                    orders.append({
                        "signal_id": signal.signal_id,
                        "symbol": symbol,
                        "action": order_action,
                        "quantity": odd_qty,
                        "is_odd_lot": True,
                        "estimated_price": ref_price
                    })
            else:
                orders.append({
                    "signal_id": signal.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": quantity,
                    "is_odd_lot": True,
                    "estimated_price": ref_price
                })
                
        return orders
