from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo, TrendBreakoutParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.filters import index_above_ma
from src.strategy.universe import coerce_universe


class TrendBreakoutStrategy:
    """趨勢帶量突破：20 日新高 + 1.5 倍量 + 個股/大盤 60MA 濾網。
    僅產生 BUY；出場由 risk_exit 引擎依 YAML exit: 參數執行。
    portfolio.positions 必須是本策略自己的持倉視角（已持有即不再進場、不加碼）。"""

    def __init__(self, params: TrendBreakoutParams, universe_symbols: list[str], index_symbol: str):
        self.params = params
        self.universe = coerce_universe(universe_symbols)
        self.index_symbol = index_symbol

    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot
    ) -> DailySignalBundle:
        signals = []
        p = self.params
        date_tag = context.as_of_date.strftime('%Y%m%d')
        required_bars = max(p.breakout_lookback_days + 1, p.volume_avg_days + 1, p.ma_trend_period)

        market_ok = index_above_ma(market_data, self.index_symbol, p.index_ma_period)

        if market_ok:
            for symbol in self.universe.symbols_as_of(context.as_of_date):
                if symbol in portfolio.positions and portfolio.positions[symbol].quantity > 0:
                    continue

                history = market_data.history(symbol, limit=required_bars)
                if len(history) < required_bars:
                    continue

                closes = [bar.close for bar in history]
                volumes = [bar.volume for bar in history]
                latest_close = closes[-1]
                latest_volume = volumes[-1]

                prior_high = max(closes[-(p.breakout_lookback_days + 1):-1])
                avg_volume = sum(volumes[-(p.volume_avg_days + 1):-1]) / p.volume_avg_days
                sma_trend = sum(closes[-p.ma_trend_period:]) / p.ma_trend_period

                is_breakout = latest_close > prior_high
                volume_ratio = (latest_volume / avg_volume) if avg_volume > 0 else 0.0
                is_volume_confirmed = volume_ratio > p.volume_multiple_pct / 100.0
                is_uptrend = latest_close > sma_trend

                if is_breakout and is_volume_confirmed and is_uptrend:
                    signals.append(
                        SignalItem(
                            signal_id=f"sig-{date_tag}-{context.strategy_id}-{symbol}-buy",
                            symbol=symbol,
                            action="BUY",
                            reference_price=float(latest_close / 10000.0),
                            reason_code="TREND_BREAKOUT_ENTRY",
                            ranking_score=volume_ratio,
                            strategy_id=context.strategy_id,
                            signal_source="ENTRY"
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
            bundle_id=f"bundle-{date_tag}-{context.strategy_id}",
            run_id=context.run_id,
            approval_id=context.approval_id,
            strategy=strategy_info,
            signal_date=context.as_of_date,
            target_execution_date=context.as_of_date,  # adjusted by calendar in runner
            market_data_cutoff=context.as_of_date,
            signals=signals
        )
