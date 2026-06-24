from datetime import date
import uuid
from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo
from src.contracts.decision_codes import DecisionCode
from src.contracts.models import TrendPullbackParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.universe import coerce_universe

class TrendPullbackStrategy:
    def __init__(self, params: TrendPullbackParams, universe_symbols: list[str]):
        self.params = params
        self.universe = coerce_universe(universe_symbols)

    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot
    ) -> DailySignalBundle:
        signals = []
        required_bars = max(self.params.ma_short, self.params.ma_long)
        
        for symbol in self.universe.symbols_as_of(context.as_of_date):
            history = market_data.history(symbol, limit=required_bars)
            
            # Check for insufficient history
            if len(history) < required_bars:
                # Skip symbol decision due to INSUFFICIENT_HISTORY
                continue
                
            # Compute moving averages (using close prices)
            close_prices = [bar.close for bar in history]
            ma_short = sum(close_prices[-self.params.ma_short:]) / self.params.ma_short
            ma_long = sum(close_prices[-self.params.ma_long:]) / self.params.ma_long
            
            latest_bar = history[-1]
            latest_close = latest_bar.close
            
            # Check if we have a position
            has_pos = symbol in portfolio.positions and portfolio.positions[symbol].quantity > 0
            
            if not has_pos:
                # Entry Logic
                # close > ma_long (uptrend) AND ma_short > ma_long (bullish) AND close < ma_short (pullback)
                if latest_close > ma_long and ma_short > ma_long and latest_close < ma_short:
                    signals.append(
                        SignalItem(
                            signal_id=f"sig-{context.as_of_date.strftime('%Y%m%d')}-{symbol}-buy",
                            symbol=symbol,
                            action="BUY",
                            reference_price=float(latest_close / 10000.0),
                            reason_code="TREND_PULLBACK_ENTRY"
                        )
                    )
            else:
                # Exit Logic
                pos = portfolio.positions[symbol]
                if pos.is_long_term:
                    continue
                entry_price = pos.entry_price
                
                # Check Stop Loss (SL)
                sl_price = entry_price * (1 - self.params.stop_loss_bps / 10000.0)
                # Check Take Profit (TP)
                tp_price = entry_price * (1 + self.params.take_profit_bps / 10000.0)
                
                is_sl = latest_close <= sl_price
                is_tp = latest_close >= tp_price
                is_trend_exit = latest_close < ma_long
                
                if is_sl or is_tp or is_trend_exit:
                    reason = "TREND_PULLBACK_EXIT"
                    if is_sl:
                        reason = "STOP_LOSS_EXIT"
                    elif is_tp:
                        reason = "TAKE_PROFIT_EXIT"
                        
                    signals.append(
                        SignalItem(
                            signal_id=f"sig-{context.as_of_date.strftime('%Y%m%d')}-{symbol}-sell",
                            symbol=symbol,
                            action="SELL",
                            reference_price=float(latest_close / 10000.0),
                            reason_code=reason
                        )
                    )
                    
        strategy_info = StrategyInfo(
            strategy_id=context.strategy_id,
            strategy_version=context.strategy_version,
            params_canonicalization="strategy-params-v1",
            params_hash=context.params_hash
        )
        
        return DailySignalBundle(
            schema_version="1.0",
            bundle_id=f"bundle-{context.as_of_date.strftime('%Y%m%d')}",
            run_id=context.run_id,
            approval_id=context.approval_id,
            strategy=strategy_info,
            signal_date=context.as_of_date,
            target_execution_date=context.as_of_date,  # Will be adjusted by calendar in runner
            market_data_cutoff=context.as_of_date,
            signals=signals
        )
