"""多策略 Allocator：固定順序合併、同日 netting、雙層限額、T+1 現金規則。

- SELL 一律先處理（exit 優先），數量取該 strategy_id + symbol 彙總部位全數。
- 同日同標的 BUY/SELL 並存：抑制 BUY，記 NETTING_SUPPRESSED 事件。
- BUY 限額：策略 manifest 限額（per-strategy）+ 全局帳戶限額（GlobalLimits）。
- T+1：當日 SELL 預估收益不計入當日 BUY 可用現金。
"""
from dataclasses import dataclass, field
from src.contracts.models import SignalItem, LimitsInfo


@dataclass
class GlobalLimits:
    max_open_positions: int = 8
    max_daily_buy_value: int = 200000
    max_new_positions_per_day: int = 2


@dataclass
class MultiPortfolioState:
    available_cash: int
    # (strategy_id, symbol) -> qty; strategy buckets only (no long-term / MANUAL)
    strategy_positions: dict
    global_daily_buy_spent: int = 0
    strategy_daily_buy_spent: dict = field(default_factory=dict)


def _split_lot_orders(signal: SignalItem, strategy_id: str, action: str, quantity: int) -> list[dict]:
    orders = []
    def order(qty: int, odd: bool) -> dict:
        return {
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "action": action,
            "quantity": qty,
            "is_odd_lot": odd,
            "estimated_price": signal.reference_price,
            "strategy_id": strategy_id,
            "signal_source": signal.signal_source,
        }
    if quantity >= 1000:
        board_qty = (quantity // 1000) * 1000
        odd_qty = quantity % 1000
        orders.append(order(board_qty, False))
        if odd_qty > 0:
            orders.append(order(odd_qty, True))
    else:
        orders.append(order(quantity, True))
    return orders


class MultiStrategyAllocator:
    @staticmethod
    def plan(
        sell_signals: list[SignalItem],
        buy_signals_in_order: list[SignalItem],
        portfolio: MultiPortfolioState,
        strategy_budgets: dict[str, int],
        strategy_limits: dict[str, LimitsInfo],
        global_limits: GlobalLimits,
    ) -> tuple[list[dict], dict, list[dict]]:
        """
        Returns (planned_orders, signal_results, events).
        signal_results: signal_id -> list[order] | block-reason string.
        Caller is responsible for ordering: sells in pipeline order; buys in
        pipeline order (approval-blocked buys must be filtered out beforehand).
        """
        local_positions = dict(portfolio.strategy_positions)
        # T+1 cash rule: today's sell proceeds are NOT added to buyable cash.
        local_cash = portfolio.available_cash
        global_spent = portfolio.global_daily_buy_spent
        strategy_spent = dict(portfolio.strategy_daily_buy_spent)

        planned_orders: list[dict] = []
        signal_results: dict = {}
        events: list[dict] = []
        netted_symbols: set[str] = set()

        # 1. SELLs first (exit priority). Quantity = full per-strategy position.
        for sig in sell_signals:
            strategy_id = sig.strategy_id
            key = (strategy_id, sig.symbol)
            current_qty = local_positions.get(key, 0)
            if current_qty <= 0:
                signal_results[sig.signal_id] = []
                continue
            sig_orders = _split_lot_orders(sig, strategy_id, "close_long", current_qty)
            local_positions[key] = 0
            netted_symbols.add(sig.symbol)
            planned_orders.extend(sig_orders)
            signal_results[sig.signal_id] = sig_orders

        # 2. BUYs in deterministic pipeline order; within a strategy the caller
        #    pre-sorts by ranking_score desc then symbol.
        new_positions_count = 0
        for sig in buy_signals_in_order:
            strategy_id = sig.strategy_id
            symbol = sig.symbol
            ref_price = sig.reference_price
            key = (strategy_id, symbol)

            # Same-day netting: exit wins, entry suppressed.
            if symbol in netted_symbols:
                signal_results[sig.signal_id] = "NETTING_SUPPRESSED: 當日同標的已有出場訊號"
                events.append({
                    "event_type": "NETTING_SUPPRESSED",
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "detail": f"signal {sig.signal_id} suppressed by same-day exit"
                })
                continue

            limits = strategy_limits.get(strategy_id)
            if limits is None:
                signal_results[sig.signal_id] = "APPROVAL_NOT_FOUND: 查無該策略的有效授權"
                continue

            # Per-strategy daily buy limit
            s_spent = strategy_spent.get(strategy_id, 0)
            strategy_remaining = limits.max_daily_buy_value - s_spent
            if strategy_remaining <= 0:
                signal_results[sig.signal_id] = (
                    f"DAILY_BUY_LIMIT_EXCEEDED: 策略 {strategy_id} 每日限額 {limits.max_daily_buy_value} 已用 {s_spent}"
                )
                continue

            # Global daily buy limit
            global_remaining = global_limits.max_daily_buy_value - global_spent
            if global_remaining <= 0:
                signal_results[sig.signal_id] = (
                    f"GLOBAL_DAILY_BUY_LIMIT_EXCEEDED: 全局每日限額 {global_limits.max_daily_buy_value} 已用 {global_spent}"
                )
                continue

            is_new_position = local_positions.get(key, 0) == 0
            # No-add invariant, enforced here at the per-account execution boundary.
            # Signal bundles are account-agnostic (no account_id) and shared across
            # accounts; whichever account's run generates a bundle first bakes ITS
            # position view into the strategy's generate()-time no-add filter, so the
            # other account would otherwise execute a BUY for a symbol it already
            # holds. The only authoritative "do I already hold this?" check is against
            # this account's live positions, which is exactly local_positions here.
            # ponytail: all current strategies are enter-once (no scaling-in). If a
            # scaling strategy is ever added, gate this on a per-strategy allow_add flag.
            if not is_new_position:
                signal_results[sig.signal_id] = (
                    f"ALREADY_HOLDING: 策略 {strategy_id} 已持有 {symbol}，不加碼"
                )
                continue
            if is_new_position:
                # Per-strategy open positions
                strategy_open = sum(
                    1 for (sid, _sym), qty in local_positions.items() if sid == strategy_id and qty > 0
                )
                if strategy_open >= limits.max_open_positions:
                    signal_results[sig.signal_id] = (
                        f"MAX_OPEN_POSITIONS_EXCEEDED: 策略 {strategy_id} 持倉 {strategy_open}，上限 {limits.max_open_positions}"
                    )
                    continue
                # Global distinct open symbols across strategy buckets
                global_open = len({sym for (_sid, sym), qty in local_positions.items() if qty > 0})
                if global_open >= global_limits.max_open_positions:
                    signal_results[sig.signal_id] = (
                        f"GLOBAL_MAX_OPEN_POSITIONS_EXCEEDED: 全帳戶持倉 {global_open}，上限 {global_limits.max_open_positions}"
                    )
                    continue
                if new_positions_count >= global_limits.max_new_positions_per_day:
                    signal_results[sig.signal_id] = (
                        f"DAILY_NEW_BUY_LIMIT_EXCEEDED: 每日新建倉上限 {global_limits.max_new_positions_per_day}"
                    )
                    continue

            order_budget = min(
                strategy_budgets.get(strategy_id, 20000),
                limits.max_order_value,
                strategy_remaining,
                global_remaining,
                local_cash,
            )
            if order_budget <= 0 or order_budget < ref_price:
                signal_results[sig.signal_id] = (
                    f"INSUFFICIENT_CASH: cash {local_cash}, budget {order_budget}, price {ref_price}"
                )
                continue

            quantity = int(order_budget // ref_price)
            # Shrink to fit cash + estimated fee and remaining limits
            while quantity > 0:
                trade_value = int(round(quantity * ref_price))
                fee = max(20, int(round(trade_value * 0.001425)))
                if (trade_value + fee <= local_cash
                        and trade_value <= strategy_remaining
                        and trade_value <= global_remaining):
                    break
                quantity -= 1
            if quantity <= 0:
                signal_results[sig.signal_id] = (
                    f"INSUFFICIENT_CASH: 現金 {local_cash} 含手續費後不足以買入 1 股"
                )
                continue

            trade_value = int(round(quantity * ref_price))
            fee = max(20, int(round(trade_value * 0.001425)))
            local_cash -= (trade_value + fee)
            global_spent += trade_value
            strategy_spent[strategy_id] = strategy_spent.get(strategy_id, 0) + trade_value
            local_positions[key] = local_positions.get(key, 0) + quantity
            if is_new_position:
                new_positions_count += 1

            action = "open_long" if is_new_position else "increase_long"
            sig_orders = _split_lot_orders(sig, strategy_id, action, quantity)
            planned_orders.extend(sig_orders)
            signal_results[sig.signal_id] = sig_orders

        return planned_orders, signal_results, events
