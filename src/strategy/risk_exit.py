"""持股風險退出引擎 (Risk Exit Engine)。

部位監控執行引擎：不選股、不擁有自己的參數，每個持倉 position 依其
strategy_id 回溯所屬策略 YAML 的 exit: 區塊取得參數（§1.3）。

- 監控對象：strategy_id 有 exit 參數定義、且 is_long_term = 0 的彙總部位。
  MANUAL 與長期持倉結構性排除。
- 判斷單位：strategy_id + symbol 彙總部位；任一條件觸發即全數出場。
- SELL 訊號 strategy_id = 原持倉策略，signal_source = 'RISK_EXIT'。
- 每個有出場訊號的策略各產生一份 exit bundle（bundle_id 後綴 -exit）。
"""
from datetime import date

from src.calendar.calendar import TradingCalendar
from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo, ExitParams
from src.market_data.repository import PointInTimeMarketData
from src.portfolio.projection import PortfolioProjection
from src.strategy.registry import StrategyDefinition


class RiskExitEngine:
    def __init__(
        self,
        exit_definitions: dict[str, StrategyDefinition],
        projection: PortfolioProjection,
        calendar: TradingCalendar,
    ):
        # Only strategies with an exit: block are accepted; defensive filter.
        self.exit_definitions = {
            sid: d for sid, d in exit_definitions.items() if d.exit_params is not None
        }
        self.projection = projection
        self.calendar = calendar

    def generate_exit_bundles(
        self,
        as_of_date: date,
        account_id: str,
        market_data: PointInTimeMarketData,
        run_id: str,
    ) -> list[DailySignalBundle]:
        date_tag = as_of_date.strftime('%Y%m%d')
        positions = self.projection.get_strategy_positions(account_id, include_long_term=False)

        signals_by_strategy: dict[str, list[SignalItem]] = {}
        for (strategy_id, symbol), pos in sorted(positions.items()):
            defn = self.exit_definitions.get(strategy_id)
            if defn is None:
                continue  # MANUAL / unmanaged strategies are structurally excluded

            reason = self._evaluate_position(as_of_date, account_id, pos, defn.exit_params, market_data)
            if reason is None:
                continue

            latest = market_data.latest(symbol)
            if latest is None:
                continue
            signals_by_strategy.setdefault(strategy_id, []).append(
                SignalItem(
                    signal_id=f"sig-{date_tag}-{strategy_id}-{symbol}-sell",
                    symbol=symbol,
                    action="SELL",
                    reference_price=float(latest.close / 10000.0),
                    reason_code=reason,
                    strategy_id=strategy_id,
                    signal_source="RISK_EXIT"
                )
            )

        bundles = []
        for strategy_id in sorted(signals_by_strategy):
            defn = self.exit_definitions[strategy_id]
            strategy_info = StrategyInfo(
                strategy_id=strategy_id,
                strategy_version=defn.strategy_version,
                params_canonicalization="strategy-params-v1",
                params_hash=defn.params_hash
            )
            bundles.append(
                DailySignalBundle(
                    schema_version="1.0",
                    bundle_id=f"bundle-{date_tag}-{strategy_id}-exit",
                    run_id=run_id,
                    # SELLs are never approval-gated (§2.7); placeholder id keeps schema satisfied.
                    approval_id="risk-exit",
                    strategy=strategy_info,
                    signal_date=as_of_date,
                    target_execution_date=as_of_date,  # adjusted by calendar in runner
                    market_data_cutoff=as_of_date,
                    signals=signals_by_strategy[strategy_id]
                )
            )
        return bundles

    def _evaluate_position(
        self,
        as_of_date: date,
        account_id: str,
        pos: dict,
        exit_params: ExitParams,
        market_data: PointInTimeMarketData,
    ) -> str | None:
        """Return the exit reason code if any condition triggers, else None."""
        symbol = pos["symbol"]
        strategy_id = pos["strategy_id"]
        wavg_price = pos["wavg_price"]
        first_date = pos["first_acquired_at"][:10]

        latest = market_data.latest(symbol)
        if latest is None or wavg_price <= 0:
            return None
        close = latest.close

        # 1. 固定停損：收盤跌破加權均價 × (1 - fixed_stop_loss_bps)
        if close <= wavg_price * (1 - exit_params.fixed_stop_loss_bps / 10000.0):
            return "FIXED_STOP_EXIT"

        # 2. 移動停利：自持有後最高收盤價回落 trailing_stop_bps
        #    最高價來自 position_high_watermarks 事實表（§2.2），視窗起點為現存
        #    lot 的最早取得日；無 watermark（如建倉首日）以 max(買入均價, 當日收盤) 保守初始化。
        high = self.projection.get_position_high(account_id, strategy_id, symbol, first_date)
        if high is None:
            high = max(wavg_price, close)
        if close <= high * (1 - exit_params.trailing_stop_bps / 10000.0):
            return "TRAILING_STOP_EXIT"

        # 3. 均線失效：連續 N 日收盤低於 sma × (1 - buffer)
        period = exit_params.ma_break_period
        confirm = exit_params.ma_break_confirm_days
        history = market_data.history(symbol, limit=period + confirm - 1)
        if len(history) >= period + confirm - 1:
            closes = [bar.close for bar in history]
            broken_all = True
            for i in range(confirm):
                # day offset i from the most recent (i=0 latest)
                end = len(closes) - i
                day_close = closes[end - 1]
                sma = sum(closes[end - period:end]) / period
                if day_close >= sma * (1 - exit_params.ma_break_buffer_bps / 10000.0):
                    broken_all = False
                    break
            if broken_all:
                return "MA_BREAK_EXIT"

        # 4. 時間停損：持有達 time_stop_days 個交易日且累計報酬未達門檻
        sessions = self.calendar.sessions_between(date.fromisoformat(first_date), as_of_date)
        holding_days = max(0, len(sessions) - 1)
        if holding_days >= exit_params.time_stop_days:
            return_bps = (close - wavg_price) / wavg_price * 10000.0
            if return_bps < exit_params.time_stop_min_return_bps:
                return "TIME_STOP_EXIT"

        return None
