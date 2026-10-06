from src.contracts.models import DailySignalBundle, BreakoutShadowFilterParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.trend_breakout import TrendBreakoutStrategy


def shadow_sums(bars) -> tuple[int, int, int]:
    """回 (上影線總和, 下影線總和, 高低振幅總和)，價格單位同 MarketBar（元×10000），全程整數。"""
    upper = sum(b.high - max(b.open, b.close) for b in bars)
    lower = sum(min(b.open, b.close) - b.low for b in bars)
    span = sum(b.high - b.low for b in bars)
    return upper, lower, span


class BreakoutShadowFilterStrategy:
    """研究 Challenger：trend_breakout 1.0.0 進場條件 + 影線濾網（近 N 日上影線總和 > 下影線總和 → 不進場）。
    thesis：docs/strategies/breakout_shadow_filter.md。

    以組合方式重用 TrendBreakoutStrategy 產生候選訊號再過濾，保證基礎條件與基準逐字相同、
    不動 live 的 trend_breakout 程式碼。僅產生 BUY；出場由 risk_exit 依 YAML exit: 執行。"""

    REASON_CODE = "BREAKOUT_SHADOW_FILTER_ENTRY"

    def __init__(self, params: BreakoutShadowFilterParams, universe_symbols: list[str], index_symbol: str):
        self.params = params
        self._base = TrendBreakoutStrategy(params.base_params(), universe_symbols, index_symbol)

    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot
    ) -> DailySignalBundle:
        bundle = self._base.generate(context, market_data, portfolio)
        window = self.params.shadow_window_days
        kept = []
        for signal in bundle.signals:
            bars = market_data.history(signal.symbol, limit=window)
            upper, lower, _span = shadow_sums(bars)
            if len(bars) == window and upper > lower:
                continue
            kept.append(signal.model_copy(update={"reason_code": self.REASON_CODE}))
        return bundle.model_copy(update={"signals": kept})
