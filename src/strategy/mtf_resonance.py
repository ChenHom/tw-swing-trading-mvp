"""多週期共振（mtf_resonance）：日＋週 MACD 共振進場，出場交 risk_exit（trend_rider 式寬移動停利）。

進場（多方共振才放行，僅產生 BUY）：
  1. 大盤 60MA 多頭濾網（崩盤防守，沿用專案慣例）。
  2. 週 K MACD：上一個「完整週」的 DIF_w > 0（中期動能偏多，「pos」變體）。
     註：研究掃描比較過 pos（僅 DIF>0）vs pos_up（另要求 DIF 較前一週上升）——
     pos(E2) PF 1.63 ≥ pos_up(E0) PF 1.59 且兩段更穩，故定版刻意選 pos、不要求上升。
  3. 日 K MACD 金叉：今日 DIF 上穿 DEA（昨日 DIF ≤ DEA、今日 DIF > DEA）；
     require_daily_zero_axis 時另要求今日 DIF > 0（零軸上金叉，較強）。

PIT：全用 history(limit) 內（as_of_date 及以前）的日 K；週 K 由日 K 依 ISO 週 resample，
週濾網取「上一個完整週」（weekly[-2]），不使用當週未完成資訊。出場不在此，交 risk_exit
以 YAML exit: 的寬移動停利實現「讓贏家跑」（回測顯示：成敗全在出場，緊出場必虧）。
"""
from src.contracts.models import DailySignalBundle, SignalItem, StrategyInfo, MtfResonanceParams
from src.market_data.repository import PointInTimeMarketData
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot
from src.strategy.filters import index_above_ma
from src.strategy.universe import coerce_universe


def _ema_series(vals: list[float], n: int) -> list:
    """EMA，前 n-1 根為 None（以前 n 根 SMA 作種子）。"""
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    e = sum(vals[:n]) / n
    out[n - 1] = e
    k = 2 / (n + 1)
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def _macd(closes: list[float], fast: int, slow: int, signal: int):
    """回傳 (DIF, DEA)，各與 closes 等長、warmup 段為 None。DIF=EMA(fast)-EMA(slow)、DEA=EMA(signal) of DIF。"""
    e_fast, e_slow = _ema_series(closes, fast), _ema_series(closes, slow)
    dif = [(a - b) if (a is not None and b is not None) else None for a, b in zip(e_fast, e_slow)]
    idx = [i for i, d in enumerate(dif) if d is not None]
    dea_vals = _ema_series([dif[i] for i in idx], signal)
    dea = [None] * len(closes)
    for j, i in enumerate(idx):
        dea[i] = dea_vals[j]
    return dif, dea


def _weekly_closes(bars) -> list[float]:
    """日 K → 週 K：每 ISO 週取最後一根 close（依日期遞增；最後一個元素為當週/未完成週）。"""
    out, last_key = [], None
    for bar in bars:
        key = bar.trade_date.isocalendar()[:2]
        if key != last_key:
            out.append(bar.close)
            last_key = key
        else:
            out[-1] = bar.close
    return out


class MtfResonanceStrategy:
    def __init__(self, params: MtfResonanceParams, universe_symbols: list[str], index_symbol: str):
        self.params = params
        self.universe = coerce_universe(universe_symbols)
        self.index_symbol = index_symbol

    def generate(
        self,
        context: SignalGenerationContext,
        market_data: PointInTimeMarketData,
        portfolio: PortfolioSnapshot,
    ) -> DailySignalBundle:
        p = self.params
        signals = []
        date_tag = context.as_of_date.strftime('%Y%m%d')
        # B7：EMA 種子（首 n 根 SMA）在滑動窗內逐日漂移——窗太短時同一週的 DIF_w 每天算出來
        # 都略不同，與研究端全歷史計算的訊號日可能不一致。取 3×(slow+signal) 週，種子誤差
        # 衰減至 <1%（(1-2/(slow+1))^(2(slow+signal)-slow) ≈ 0.3%），換算日 K ×5。
        # 預設參數（26,9）下 = 525 根日 K：歷史不足 525 天的標的不出訊號（準確優先於樣本數）。
        required_bars = (p.macd_slow + p.macd_signal) * 3 * 5

        market_ok = index_above_ma(market_data, self.index_symbol, p.index_ma_period)
        if market_ok:
            for symbol in self.universe.symbols_as_of(context.as_of_date):
                pos = portfolio.positions.get(symbol)
                if pos is not None and pos.quantity > 0:
                    continue  # 已持有不加碼

                history = market_data.history(symbol, limit=required_bars)
                if len(history) < required_bars:
                    continue
                closes = [bar.close for bar in history]

                # 日 K MACD 金叉（今日上穿昨日）
                dif_d, dea_d = _macd(closes, p.macd_fast, p.macd_slow, p.macd_signal)
                if None in (dif_d[-1], dea_d[-1], dif_d[-2], dea_d[-2]):
                    continue
                cross = dif_d[-2] <= dea_d[-2] and dif_d[-1] > dea_d[-1]
                if not cross:
                    continue
                if p.require_daily_zero_axis and not (dif_d[-1] > 0):
                    continue

                # 週 K MACD：上一個完整週 DIF_w > 0
                wk = _weekly_closes(history)
                dif_w, _ = _macd(wk, p.macd_fast, p.macd_slow, p.macd_signal)
                if len(dif_w) < 2 or dif_w[-2] is None or not (dif_w[-2] > 0):
                    continue

                latest_close = closes[-1]
                signals.append(
                    SignalItem(
                        signal_id=f"sig-{date_tag}-{context.strategy_id}-{symbol}-buy",
                        symbol=symbol,
                        action="BUY",
                        reference_price=float(latest_close / 10000.0),
                        reason_code="MTF_RESONANCE_ENTRY",
                        # 排序：日 MACD 柱狀（DIF-DEA）相對股價越大＝動能越強，優先配置。
                        ranking_score=(dif_d[-1] - dea_d[-1]) / latest_close if latest_close > 0 else 0.0,
                        strategy_id=context.strategy_id,
                        signal_source="ENTRY",
                    )
                )

        strategy_info = StrategyInfo(
            strategy_id=context.strategy_id,
            strategy_version=context.strategy_version,
            params_canonicalization="strategy-params-v1",
            params_hash=context.params_hash,
        )
        return DailySignalBundle(
            schema_version="1.0",
            bundle_id=f"bundle-{date_tag}-{context.strategy_id}",
            run_id=context.run_id,
            approval_id=context.approval_id,
            strategy=strategy_info,
            signal_date=context.as_of_date,
            target_execution_date=context.as_of_date,
            market_data_cutoff=context.as_of_date,
            signals=signals,
        )
