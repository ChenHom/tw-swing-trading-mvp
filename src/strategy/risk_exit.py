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
                    # account in the id keeps each account's exit bundle distinct, so the
                    # idempotent _save_bundle no longer lets the first account to run clobber
                    # the second account's exits (a per-account fact: cost / high-watermark).
                    bundle_id=f"bundle-{date_tag}-{strategy_id}-{account_id}-exit",
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
        """Return the exit reason code if any condition triggers, else None.

        單一真相來源：委派 explain_exit，取其 reason（保持原本「按優先序第一個觸發」語意）。
        """
        return self.explain_exit(as_of_date, account_id, pos, exit_params, market_data)["reason"]

    def explain_exit(
        self,
        as_of_date: date,
        account_id: str,
        pos: dict,
        exit_params: ExitParams,
        market_data: PointInTimeMarketData,
    ) -> dict:
        """逐條評估四項出場條件並回傳完整明細（供 dry-run 試算與 _evaluate_position 共用）。

        回傳 dict：
          - evaluable: 是否有行情/有效成本可評估
          - close, wavg（元）
          - fixed_stop / trailing / ma_break / time_stop：各自 {..., hit: bool}
          - reason: 按優先序（固定停損→移動停利→均線失效→時間停損）第一個觸發的 reason code；
            皆未觸發為 None。
        priority 與短路語意與舊 _evaluate_position 一致：reason 取第一個 hit 的條件。
        """
        symbol = pos["symbol"]
        strategy_id = pos["strategy_id"]
        wavg_price = pos["wavg_price"]
        first_date = pos["first_acquired_at"][:10]

        latest = market_data.latest(symbol)
        if latest is None or wavg_price <= 0:
            return {
                "evaluable": False,
                "symbol": symbol,
                "close": None,
                "wavg": _scaled_to_yuan(wavg_price) if wavg_price else None,
                "fixed_stop": None,
                "trailing": None,
                "ma_break": None,
                "time_stop": None,
                "reason": None,
            }
        close = latest.close

        # 1. 固定停損：收盤跌破加權均價 × (1 - fixed_stop_loss_bps)
        fixed_level = wavg_price * (1 - exit_params.fixed_stop_loss_bps / 10000.0)
        fixed_hit = close <= fixed_level
        fixed_stop = {
            "stop_loss_bps": exit_params.fixed_stop_loss_bps,
            "level": _scaled_to_yuan(fixed_level),
            "hit": fixed_hit,
        }

        # 2. 移動停利：自持有後最高收盤價回落 trailing_stop_bps
        #    最高價來自 position_high_watermarks 事實表（§2.2），視窗起點為現存
        #    lot 的最早取得日；無 watermark（如建倉首日或 MANUAL/長期未累積）以
        #    max(買入均價, 當日收盤) 保守初始化。
        high = self.projection.get_position_high(account_id, strategy_id, symbol, first_date)
        high_from_watermark = high is not None
        if high is None:
            high = max(wavg_price, close)
        trailing_level = high * (1 - exit_params.trailing_stop_bps / 10000.0)
        trailing_hit = close <= trailing_level
        trailing = {
            "trailing_stop_bps": exit_params.trailing_stop_bps,
            "high": _scaled_to_yuan(high),
            "high_from_watermark": high_from_watermark,
            "level": _scaled_to_yuan(trailing_level),
            "hit": trailing_hit,
        }

        # 3. 均線失效：連續 N 日收盤低於 sma × (1 - buffer)
        period = exit_params.ma_break_period
        confirm = exit_params.ma_break_confirm_days
        history = market_data.history(symbol, limit=period + confirm - 1)
        ma_evaluable = len(history) >= period + confirm - 1
        ma_hit = False
        latest_sma = None
        if ma_evaluable:
            closes = [bar.close for bar in history]
            broken_all = True
            for i in range(confirm):
                # day offset i from the most recent (i=0 latest)
                end = len(closes) - i
                day_close = closes[end - 1]
                sma = sum(closes[end - period:end]) / period
                if i == 0:
                    latest_sma = sma
                if day_close >= sma * (1 - exit_params.ma_break_buffer_bps / 10000.0):
                    broken_all = False
                    break
            ma_hit = broken_all
        ma_break = {
            "period": period,
            "confirm_days": confirm,
            "buffer_bps": exit_params.ma_break_buffer_bps,
            "sma": _scaled_to_yuan(latest_sma) if latest_sma is not None else None,
            "evaluable": ma_evaluable,
            "hit": ma_hit,
        }

        # 4. 時間停損：持有達 time_stop_days 個交易日且累計報酬未達門檻
        sessions = self.calendar.sessions_between(date.fromisoformat(first_date), as_of_date)
        holding_days = max(0, len(sessions) - 1)
        return_bps = (close - wavg_price) / wavg_price * 10000.0
        time_hit = (
            holding_days >= exit_params.time_stop_days
            and return_bps < exit_params.time_stop_min_return_bps
        )
        time_stop = {
            "time_stop_days": exit_params.time_stop_days,
            "min_return_bps": exit_params.time_stop_min_return_bps,
            "holding_days": holding_days,
            "return_bps": round(return_bps, 1),
            "hit": time_hit,
        }

        # 優先序：第一個觸發者為 reason（與舊短路邏輯一致）
        reason = None
        if fixed_hit:
            reason = "FIXED_STOP_EXIT"
        elif trailing_hit:
            reason = "TRAILING_STOP_EXIT"
        elif ma_hit:
            reason = "MA_BREAK_EXIT"
        elif time_hit:
            reason = "TIME_STOP_EXIT"

        return {
            "evaluable": True,
            "symbol": symbol,
            "close": _scaled_to_yuan(close),
            "wavg": _scaled_to_yuan(wavg_price),
            "fixed_stop": fixed_stop,
            "trailing": trailing,
            "ma_break": ma_break,
            "time_stop": time_stop,
            "reason": reason,
        }


def _scaled_to_yuan(scaled: float) -> float:
    """價格內部以 ×10000 整數儲存；轉回元（保留 2 位）供顯示/明細。"""
    return round(scaled / 10000.0, 2)
