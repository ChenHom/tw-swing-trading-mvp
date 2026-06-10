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

    @staticmethod
    def plan_all(
        signals: list[SignalItem],
        portfolio: PortfolioState,
        strategy_budget: int,
        manifest_limits: LimitsInfo
    ) -> tuple[list[dict], dict[str, list[dict] | str]]:
        # Clone local state
        local_cash = portfolio.available_cash
        local_daily_buy_value_spent = portfolio.daily_buy_value_spent
        local_positions = dict(portfolio.positions)
        
        sell_signals = [s for s in signals if s.action.upper() == "SELL"]
        buy_signals = [s for s in signals if s.action.upper() == "BUY"]
        
        # Sort BUY signals: by ranking_score descending (default 0.0), then symbol ascending (tie-breaker)
        buy_signals.sort(key=lambda s: (-(s.ranking_score if s.ranking_score is not None else 0.0), s.symbol))
        
        planned_orders = []
        signal_results = {}
        
        # 1. Process all SELL signals first (free up cash)
        for sig in sell_signals:
            symbol = sig.symbol
            ref_price = sig.reference_price
            current_qty = local_positions.get(symbol, 0)
            
            if current_qty <= 0:
                signal_results[sig.signal_id] = []
                continue
                
            # Calculate estimated proceeds
            trade_value = int(round(current_qty * ref_price))
            broker_fee = max(20, int(round(trade_value * 0.001425)))
            tax = int(round(trade_value * 0.003))
            net_proceeds = trade_value - broker_fee - tax
            
            local_cash += net_proceeds
            local_positions[symbol] = 0
            
            sig_orders = []
            order_action = "close_long"
            
            if current_qty >= 1000:
                board_qty = (current_qty // 1000) * 1000
                odd_qty = current_qty % 1000
                
                sig_orders.append({
                    "signal_id": sig.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": board_qty,
                    "is_odd_lot": False,
                    "estimated_price": ref_price
                })
                if odd_qty > 0:
                    sig_orders.append({
                        "signal_id": sig.signal_id,
                        "symbol": symbol,
                        "action": order_action,
                        "quantity": odd_qty,
                        "is_odd_lot": True,
                        "estimated_price": ref_price
                    })
            else:
                sig_orders.append({
                    "signal_id": sig.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": current_qty,
                    "is_odd_lot": True,
                    "estimated_price": ref_price
                })
                
            planned_orders.extend(sig_orders)
            signal_results[sig.signal_id] = sig_orders
            
        # 2. Process BUY signals
        new_buys_count = 0
        max_new_buys = 2 # hard limit: max 2 new buy positions per day
        
        for sig in buy_signals:
            symbol = sig.symbol
            ref_price = sig.reference_price
            current_qty = local_positions.get(symbol, 0)
            
            remaining_daily_limit = manifest_limits.max_daily_buy_value - local_daily_buy_value_spent
            if remaining_daily_limit <= 0:
                signal_results[sig.signal_id] = f"DAILY_BUY_LIMIT_EXCEEDED: Limit {manifest_limits.max_daily_buy_value}, spent {local_daily_buy_value_spent}"
                continue
                
            order_budget = min(
                strategy_budget,
                manifest_limits.max_order_value,
                remaining_daily_limit,
                local_cash
            )
            
            if order_budget <= 0 or order_budget < ref_price:
                signal_results[sig.signal_id] = f"INSUFFICIENT_CASH: cash {local_cash}, budget {order_budget}, price {ref_price}"
                continue
                
            is_new_position = (current_qty == 0)
            if is_new_position:
                active_positions_count = sum(1 for qty in local_positions.values() if qty > 0)
                if active_positions_count >= manifest_limits.max_open_positions:
                    signal_results[sig.signal_id] = f"MAX_OPEN_POSITIONS_EXCEEDED: Open positions count {active_positions_count}, max is {manifest_limits.max_open_positions}"
                    continue
                if new_buys_count >= max_new_buys:
                    signal_results[sig.signal_id] = f"DAILY_NEW_BUY_LIMIT_EXCEEDED: Max 2 new buys per day, already planned {new_buys_count}"
                    continue
                    
            quantity = int(math.floor(order_budget / ref_price))
            
            # Adjust quantity down to fit cash and remaining daily limit with estimated broker fee
            while quantity > 0:
                trade_value = int(round(quantity * ref_price))
                fee = max(20, int(round(trade_value * 0.001425)))
                if trade_value + fee <= local_cash and trade_value <= remaining_daily_limit:
                    break
                quantity -= 1
                
            if quantity <= 0:
                signal_results[sig.signal_id] = f"INSUFFICIENT_CASH: Cash {local_cash} cannot afford even 1 share including fee"
                continue
                
            trade_value = int(round(quantity * ref_price))
            fee = max(20, int(round(trade_value * 0.001425)) )
            local_cash -= (trade_value + fee)
            local_daily_buy_value_spent += trade_value
            local_positions[symbol] = local_positions.get(symbol, 0) + quantity
            if is_new_position:
                new_buys_count += 1
                
            sig_orders = []
            order_action = "open_long" if is_new_position else "increase_long"
            
            if quantity >= 1000:
                board_qty = (quantity // 1000) * 1000
                odd_qty = quantity % 1000
                
                sig_orders.append({
                    "signal_id": sig.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": board_qty,
                    "is_odd_lot": False,
                    "estimated_price": ref_price
                })
                if odd_qty > 0:
                    sig_orders.append({
                        "signal_id": sig.signal_id,
                        "symbol": symbol,
                        "action": order_action,
                        "quantity": odd_qty,
                        "is_odd_lot": True,
                        "estimated_price": ref_price
                    })
            else:
                sig_orders.append({
                    "signal_id": sig.signal_id,
                    "symbol": symbol,
                    "action": order_action,
                    "quantity": quantity,
                    "is_odd_lot": True,
                    "estimated_price": ref_price
                })
                
            planned_orders.extend(sig_orders)
            signal_results[sig.signal_id] = sig_orders
            
        return planned_orders, signal_results
