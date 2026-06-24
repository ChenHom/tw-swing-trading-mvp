from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo, PullbackReboundParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.filters import index_above_ma
from src.strategy.universe import coerce_universe


class PullbackReboundStrategy:
    """回檔轉強：多頭結構下回踩月線且 K 線轉強 + 大盤 60MA 濾網。
    僅產生 BUY；出場由 risk_exit 引擎依 YAML exit: 參數執行。
    portfolio.positions 必須是本策略自己的持倉視角（已持有即不再進場、不加碼）。"""

    def __init__(self, params: PullbackReboundParams, universe_symbols: list[str], index_symbol: str):
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
        required_bars = p.ma_long + 1  # +1 for previous close

        market_ok = index_above_ma(market_data, self.index_symbol, p.index_ma_period)

        if market_ok:
            for symbol in self.universe.symbols_as_of(context.as_of_date):
                if symbol in portfolio.positions and portfolio.positions[symbol].quantity > 0:
                    continue

                history = market_data.history(symbol, limit=required_bars)
                if len(history) < required_bars:
                    continue

                closes = [bar.close for bar in history]
                latest = history[-1]
                prev_close = closes[-2]
                sma_short = sum(closes[-p.ma_short:]) / p.ma_short
                sma_long = sum(closes[-p.ma_long:]) / p.ma_long

                is_uptrend = latest.close > sma_long and sma_short > sma_long
                touched_support = latest.low <= sma_short * (1 + p.pullback_touch_buffer_bps / 10000.0)
                is_strong_candle = latest.close > latest.open and latest.close > prev_close

                if is_uptrend and touched_support and is_strong_candle:
                    signals.append(
                        SignalItem(
                            signal_id=f"sig-{date_tag}-{context.strategy_id}-{symbol}-buy",
                            symbol=symbol,
                            action="BUY",
                            reference_price=float(latest.close / 10000.0),
                            reason_code="PULLBACK_REBOUND_ENTRY",
                            ranking_score=(latest.close - prev_close) / prev_close if prev_close > 0 else 0.0,
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
