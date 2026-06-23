from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo, TrendRiderParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.filters import index_above_ma


class TrendRiderStrategy:
    """順勢交易者（讓贏家跑）：進場選確立的中期上升趨勢——close > 上升 60MA + 創 60 日新高 +
    大盤 60MA 多頭濾網。**僅產生 BUY；出場交 risk_exit**，靠 YAML exit: 的寬鬆參數
    （time_stop 停用、寬移動停利、長均線跌破）讓贏家跑久，不像 breakout 20 天就砍。
    保留 index 濾網＝崩盤防守不丟。portfolio.positions 為本策略自身持倉視角（已持有不再進場）。"""

    def __init__(self, params: TrendRiderParams, universe_symbols: list[str], index_symbol: str):
        self.params = params
        self.universe_symbols = universe_symbols
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
        # 需要趨勢均線 + 新高回看 + 「均線上升」判斷（拿 trend_ma_period 天前的均線比較）
        required_bars = max(p.breakout_lookback_days + 1, p.trend_ma_period * 2)

        market_ok = index_above_ma(market_data, self.index_symbol, p.index_ma_period)

        if market_ok:
            for symbol in self.universe_symbols:
                if symbol in portfolio.positions and portfolio.positions[symbol].quantity > 0:
                    continue

                history = market_data.history(symbol, limit=required_bars)
                if len(history) < required_bars:
                    continue

                closes = [bar.close for bar in history]
                latest_close = closes[-1]

                prior_high = max(closes[-(p.breakout_lookback_days + 1):-1])
                sma_trend = sum(closes[-p.trend_ma_period:]) / p.trend_ma_period
                # 均線上升：今日 trend MA > trend_ma_period 天前的 trend MA（確認趨勢方向向上）
                sma_trend_prev = sum(closes[-2 * p.trend_ma_period:-p.trend_ma_period]) / p.trend_ma_period

                is_new_high = latest_close > prior_high
                is_above_trend = latest_close > sma_trend
                is_trend_rising = sma_trend > sma_trend_prev

                if is_new_high and is_above_trend and is_trend_rising:
                    signals.append(
                        SignalItem(
                            signal_id=f"sig-{date_tag}-{context.strategy_id}-{symbol}-buy",
                            symbol=symbol,
                            action="BUY",
                            reference_price=float(latest_close / 10000.0),
                            reason_code="TREND_RIDER_ENTRY",
                            ranking_score=(latest_close / sma_trend - 1.0) if sma_trend > 0 else 0.0,
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
