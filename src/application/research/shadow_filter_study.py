"""breakout_shadow_filter 主檢定：訊號層級配對分析（thesis：docs/strategies/breakout_shadow_filter.md §B）。

組合回測受容量限制（同時 5 檔、每日 2 檔）、替補與持倉路徑影響，量到的是「濾網＋替補＋路徑」
的合成效果。這裡改在訊號層級回答「被濾網剔除的候選，事後是否比保留的差」：

1. 每個 PIT 交易日 D，對當日 universe 以**原封不動的 TrendBreakoutStrategy**（空持倉）列出全部候選；
2. 每筆候選獨立模擬：D+1 開盤買（同 FakeBroker 滑價／鎖漲停／零量規則）、每日收盤依 exit: 四條件
   （同 RiskExitEngine.explain_exit 的定義與優先序）判斷、出場訊號隔日開盤賣；
3. 依 D 當日的 7 日影線（U > L）分組，比較淨報酬，cluster bootstrap（以進場訊號日為群）＋隨機濾網置換檢定。

所有隨機性固定 seed，同資料重跑結果逐位元相同。本模組不寫 DB（腳本另記 research_ledger）。
"""
from __future__ import annotations

import bisect
import random
import sqlite3
import statistics
from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional, Sequence

from src.contracts.models import ExitParams, TrendBreakoutParams
from src.strategy.base import PortfolioSnapshot, SignalGenerationContext
from src.strategy.breakout_shadow_filter import shadow_sums
from src.strategy.trend_breakout import TrendBreakoutStrategy

FEE_RATE = 0.001425
MIN_FEE = 20
TAX_RATE = 0.003
ODD_LOT_SLIPPAGE_MULTIPLIER = 3  # 同 FakeBroker
# 與 FakeBroker 相同的鎖死判定門檻
PRICE_LIMIT_REGIME_CHANGE = date(2015, 6, 1)
LIMIT_LOCK_PCT = 0.095
LIMIT_LOCK_PCT_PRE_2015 = 0.065

SEED = 1337
BOOTSTRAP_ITERATIONS = 2000
PLACEBO_ITERATIONS = 2000


# ---------------------------------------------------------------- 資料存取

@dataclass(frozen=True, slots=True)
class LiteBar:
    """策略與模擬只讀這幾個欄位；不建完整 pydantic LiteBar，避免 ~90 萬根 bar 的驗證與記憶體成本。"""
    symbol: str
    trade_date: date
    open: int
    high: int
    low: int
    close: int
    volume: int


class InMemoryBars:
    """一次載入 market_bars（單一 price_basis、is_complete=1），提供與 SqlitePointInTimeMarketData
    同語意的 history(symbol, limit)（trade_date <= as_of、時間升冪、取最後 limit 根）。"""

    def __init__(self, conn: sqlite3.Connection, symbols: Sequence[str], price_basis: str = "raw"):
        self._bars: dict[str, list[LiteBar]] = {}
        self._dates: dict[str, list[date]] = {}
        cursor = conn.cursor()
        for symbol in symbols:
            cursor.execute(
                """
                SELECT trade_date, open, high, low, close, volume FROM market_bars
                WHERE symbol = ? AND is_complete = 1 AND price_basis = ?
                ORDER BY trade_date ASC
                """,
                (symbol, price_basis),
            )
            bars = [
                LiteBar(symbol, date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5])
                for r in cursor.fetchall()
            ]
            self._bars[symbol] = bars
            self._dates[symbol] = [b.trade_date for b in bars]

    def bars(self, symbol: str) -> list[LiteBar]:
        return self._bars.get(symbol, [])

    def index_through(self, symbol: str, as_of: date) -> int:
        """回 trade_date <= as_of 的 bar 數（即 bars[:n] 為 as_of 可見的全部）。"""
        return bisect.bisect_right(self._dates.get(symbol, []), as_of)

    def as_of(self, as_of: date) -> "InMemoryPIT":
        return InMemoryPIT(self, as_of)


class InMemoryPIT:
    def __init__(self, store: InMemoryBars, as_of: date):
        self._store = store
        self._as_of = as_of

    @property
    def as_of_date(self) -> date:
        return self._as_of

    def history(self, symbol: str, limit: int) -> list[LiteBar]:
        n = self._store.index_through(symbol, self._as_of)
        return self._store.bars(symbol)[max(0, n - limit):n]

    def latest(self, symbol: str) -> Optional[LiteBar]:
        h = self.history(symbol, 1)
        return h[0] if h else None


# ---------------------------------------------------------------- 單筆模擬

def _locked(bar: LiteBar, prev_close: Optional[int], side: str) -> bool:
    """同 FakeBroker._unfilled_reason：零量、或一價到底且收在漲（買）／跌（賣）停附近 → 無法成交。"""
    if bar.volume == 0:
        return True
    if prev_close is None or prev_close <= 0 or bar.high != bar.low:
        return False
    pct = LIMIT_LOCK_PCT_PRE_2015 if bar.trade_date < PRICE_LIMIT_REGIME_CHANGE else LIMIT_LOCK_PCT
    if side == "BUY":
        return bar.close >= prev_close * (1 + pct)
    return bar.close <= prev_close * (1 - pct)


def exit_reason(
    bars: list[LiteBar], t: int, wavg: int, high_close: int, holding_days: int, p: ExitParams,
) -> Optional[str]:
    """第 t 根收盤時的出場判斷，定義與優先序同 RiskExitEngine.explain_exit。
    high_close＝自建倉日起（含）至 t 的最高收盤：回測在評估出場前先寫 watermark，
    故 explain_exit 取到的 high 恆為此值（不與 wavg 取 max）。"""
    close = bars[t].close
    if close <= wavg * (1 - p.fixed_stop_loss_bps / 10000.0):
        return "FIXED_STOP_EXIT"
    high = high_close
    if close <= high * (1 - p.trailing_stop_bps / 10000.0):
        return "TRAILING_STOP_EXIT"
    period, confirm = p.ma_break_period, p.ma_break_confirm_days
    if t + 1 >= period + confirm - 1:
        closes = [b.close for b in bars[t + 1 - (period + confirm - 1):t + 1]]
        broken_all = True
        for i in range(confirm):
            end = len(closes) - i
            sma = sum(closes[end - period:end]) / period
            if closes[end - 1] >= sma * (1 - p.ma_break_buffer_bps / 10000.0):
                broken_all = False
                break
        if broken_all:
            return "MA_BREAK_EXIT"
    return_bps = (close - wavg) / wavg * 10000.0
    if holding_days >= p.time_stop_days and return_bps < p.time_stop_min_return_bps:
        return "TIME_STOP_EXIT"
    return None


@dataclass
class TradeOutcome:
    symbol: str
    signal_date: str
    filtered: bool          # U > L（被濾網剔除）
    tie: bool               # U == L
    status: str             # CLOSED / OPEN_AT_END / UNFILLED_ENTRY / UNFILLED_NO_BAR /
                            # NO_NEXT_SESSION / BUDGET_TOO_SMALL / STALE_SIGNAL
    entry_date: Optional[str] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None
    net_return: Optional[float] = None


def _fills(open_price: int, quantity: int, side: str, slippage_bps: int) -> list[tuple[int, int]]:
    """同 planner/allocator 拆單（整張 + 零股）與 FakeBroker 滑價（零股 ×3）：回 [(股數, 成交價)]。"""
    parts = []
    board = (quantity // 1000) * 1000
    if board:
        parts.append((board, slippage_bps))
    if quantity - board:
        parts.append((quantity - board, slippage_bps * ODD_LOT_SLIPPAGE_MULTIPLIER))
    sign = 1 if side == "BUY" else -1
    return [(q, int(round(open_price * (1 + sign * bps / 10000)))) for q, bps in parts]


def _value(qty: int, price: int) -> int:
    return int(round(qty * price / 10000.0))


def _fee(value: int) -> int:
    return max(MIN_FEE, int(round(value * FEE_RATE)))


def _next_session(sessions: Sequence[date], d: date) -> Optional[date]:
    i = bisect.bisect_right(sessions, d)
    return sessions[i] if i < len(sessions) else None


def simulate_candidate(
    bars: list[LiteBar], signal_idx: int, p: ExitParams, slippage_bps: int,
    sessions: Sequence[date], end_date: date, order_budget_twd: int,
) -> dict:
    """從訊號日 bars[signal_idx] 起模擬單筆交易，回 status/entry/exit/net_return。與回測同源之處：
    - 股數＝order_budget_twd // 訊號日收盤（同 allocator）；wavg＝成交股數加權價（同 projection）。
    - 委託只在「下一個交易所交易日」撮合；該日無 bar（停牌）→ 不成交（FakeBroker UNFILLED_NO_BAR，不重試）。
    - 每個交易日收盤重新評估出場；賣單只在隔一交易日有效，鎖跌停／零量／無 bar 則作廢，下一收盤再評估
      （條件不再成立就不賣）。停牌日以最後已知 bar（stale close、watermark 不更新）評估，同回測。
    - holding_days 以交易所日曆計（同 explain_exit：sessions_between(建倉日, as_of) − 1）。"""
    ref_price = bars[signal_idx].close / 10000.0
    quantity = int(order_budget_twd // ref_price) if ref_price > 0 else 0
    if quantity <= 0:
        return {"status": "BUDGET_TOO_SMALL"}
    entry_date = _next_session(sessions, bars[signal_idx].trade_date)
    if entry_date is None or entry_date > end_date:
        return {"status": "NO_NEXT_SESSION"}
    entry_idx = signal_idx + 1
    if entry_idx >= len(bars) or bars[entry_idx].trade_date != entry_date:
        return {"status": "UNFILLED_NO_BAR"}
    entry_bar = bars[entry_idx]
    if _locked(entry_bar, bars[signal_idx].close, "BUY"):
        return {"status": "UNFILLED_ENTRY"}
    buys = _fills(entry_bar.open, quantity, "BUY", slippage_bps)
    wavg = int(sum(q * px for q, px in buys) / quantity)
    buy_cost = sum(_value(q, px) + _fee(_value(q, px)) for q, px in buys)

    def holding(as_of: date) -> int:
        return bisect.bisect_right(sessions, as_of) - bisect.bisect_right(sessions, entry_date)

    def result(status: str, exit_bar: LiteBar, sells: list[tuple[int, int]], reason: Optional[str]) -> dict:
        proceeds = 0
        for q, px in sells:
            v = _value(q, px)
            proceeds += v - _fee(v) - int(round(v * TAX_RATE))
        return {
            "status": status, "entry_date": entry_date.isoformat(),
            "exit_date": exit_bar.trade_date.isoformat(), "exit_reason": reason,
            "net_return": proceeds / buy_cost - 1.0,
        }

    high_close: Optional[int] = None
    reason: Optional[str] = None      # 最近一次收盤評估的出場原因
    sell_on: Optional[date] = None    # 該評估產生的賣單撮合日
    last = entry_idx
    for t in range(entry_idx, len(bars)):
        bar = bars[t]
        if bar.trade_date > end_date:
            break
        if t > entry_idx:
            # 前一根 bar 與本根之間的停牌交易日：回測仍以 stale close 逐日評估，最後一個停牌日的評估決定本日是否賣
            prev = bars[t - 1]
            gap_last = None
            i = bisect.bisect_right(sessions, prev.trade_date)
            while i < len(sessions) and sessions[i] < bar.trade_date:
                gap_last = sessions[i]
                i += 1
            if gap_last is not None:
                reason = exit_reason(bars, t - 1, wavg, high_close, holding(gap_last), p)
                sell_on = _next_session(sessions, gap_last) if reason else None
            if sell_on == bar.trade_date and not _locked(bar, prev.close, "SELL"):
                return result("CLOSED", bar, _fills(bar.open, quantity, "SELL", slippage_bps), reason)
        last = t
        high_close = bar.close if high_close is None else max(high_close, bar.close)
        reason = exit_reason(bars, t, wavg, high_close, holding(bar.trade_date), p)
        sell_on = _next_session(sessions, bar.trade_date) if reason else None
    # 窗末仍持有：以最後可見收盤、無滑價扣賣出費稅設算（thesis 預先規定）
    return result("OPEN_AT_END", bars[last], [(quantity, bars[last].close)], reason)


# ---------------------------------------------------------------- 候選列舉

def enumerate_outcomes(
    store: InMemoryBars, universe, sessions: Sequence[date], index_symbol: str,
    entry_params: TrendBreakoutParams, exit_params: ExitParams, shadow_window_days: int,
    slippage_bps: int, end_date: date,
) -> list[TradeOutcome]:
    """sessions：研究窗內全部交易所交易日（訊號日與撮合日都以此為準）。
    最新 bar 不是 D 的訊號（停牌中 stale 重發）記為 STALE_SIGNAL：同一筆突破在停牌期間會每天重發，
    回測靠「已持有不再進場」壓掉，獨立模擬沒有持倉，若不排除會把同一筆交易重複計入。"""
    empty = PortfolioSnapshot(available_cash=0, positions={})
    outcomes: list[TradeOutcome] = []
    for D in sessions:
        members = universe.symbols_as_of(D)
        if not members:
            continue
        base = TrendBreakoutStrategy(entry_params, members, index_symbol)
        ctx = SignalGenerationContext(
            as_of_date=D, strategy_id="trend_breakout", strategy_version="study",
            run_id="study", approval_id="study", params_hash="study",
        )
        pit = store.as_of(D)
        for signal in base.generate(ctx, pit, empty).signals:
            window = pit.history(signal.symbol, shadow_window_days)
            upper, lower, _ = shadow_sums(window)
            filtered = len(window) == shadow_window_days and upper > lower
            bars = store.bars(signal.symbol)
            signal_idx = store.index_through(signal.symbol, D) - 1
            if bars[signal_idx].trade_date != D:
                sim = {"status": "STALE_SIGNAL"}
            else:
                sim = simulate_candidate(
                    bars, signal_idx, exit_params, slippage_bps, sessions, end_date,
                    entry_params.order_budget_twd,
                )
            outcomes.append(TradeOutcome(
                symbol=signal.symbol, signal_date=D.isoformat(), filtered=filtered,
                tie=upper == lower, **sim,
            ))
    return outcomes


# ---------------------------------------------------------------- 統計

def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _delta(kept: list[float], removed: list[float]) -> Optional[float]:
    if not kept or not removed:
        return None
    return _mean(kept) - _mean(removed)


def cluster_bootstrap_delta_lower(
    rows: list[tuple[str, bool, float]], iterations: int, rng: random.Random, alpha: float = 0.05,
) -> Optional[float]:
    """以訊號日為群重抽（同日候選高度相關），回 Δ = mean(保留) − mean(剔除) 的單尾 alpha 下界。"""
    clusters: dict[str, list[tuple[bool, float]]] = {}
    for d, filtered, r in rows:
        clusters.setdefault(d, []).append((filtered, r))
    keys = sorted(clusters)
    if not keys:
        return None
    deltas = []
    for _ in range(iterations):
        kept, removed = [], []
        for _ in range(len(keys)):
            for filtered, r in clusters[keys[rng.randrange(len(keys))]]:
                (removed if filtered else kept).append(r)
        d = _delta(kept, removed)
        if d is not None:
            deltas.append(d)
    if not deltas:
        return None
    deltas.sort()
    return deltas[int(alpha * len(deltas))]


def placebo_p_value(
    returns: list[float], n_removed: int, observed_kept_mean: float, iterations: int, rng: random.Random,
) -> Optional[float]:
    """隨機剔除同樣筆數的候選 iterations 次；p = (1 + #{安慰劑保留組平均 ≥ 實際}) / (1 + iterations)。"""
    n = len(returns)
    if n_removed <= 0 or n_removed >= n:
        return None
    total = sum(returns)
    hits = 0
    for _ in range(iterations):
        removed = rng.sample(range(n), n_removed)
        kept_mean = (total - sum(returns[i] for i in removed)) / (n - n_removed)
        if kept_mean >= observed_kept_mean:
            hits += 1
    return (1 + hits) / (1 + iterations)


def summarize(outcomes: list[TradeOutcome]) -> dict:
    """依 thesis §B 預先登錄的判定，回完整統計與 verdict。"""
    evaluated = [o for o in outcomes if o.net_return is not None]
    kept = [o.net_return for o in evaluated if not o.filtered]
    removed = [o.net_return for o in evaluated if o.filtered]
    all_returns = [o.net_return for o in evaluated]

    rng = random.Random(SEED)
    rows = [(o.signal_date, o.filtered, o.net_return) for o in evaluated]
    delta = _delta(kept, removed)
    delta_lower = cluster_bootstrap_delta_lower(rows, BOOTSTRAP_ITERATIONS, rng)
    p_value = (
        placebo_p_value(all_returns, len(removed), _mean(kept), PLACEBO_ITERATIONS, rng)
        if kept else None
    )
    # P3 剔除率：候選集合（排除 stale 重發）中 U > L 的比例，與能否成交無關（thesis §B P3）
    candidates = [o for o in outcomes if o.status != "STALE_SIGNAL"]
    rejection_rate = (sum(1 for o in candidates if o.filtered) / len(candidates)) if candidates else None

    # 最小可偵測差異（只用合併樣本標準差，不看分組結果）：雙樣本、單尾 5%、檢定力 80% 近似。
    mde = None
    if len(all_returns) > 2 and kept and removed:
        sd = statistics.pstdev(all_returns)
        mde = 2.49 * sd * (1 / len(kept) + 1 / len(removed)) ** 0.5

    checks = {
        "P1_delta_ci_lower_gt_0": delta_lower is not None and delta_lower > 0,
        "P2_placebo_p_lt_0_05": p_value is not None and p_value < 0.05,
        "P3_rejection_rate_5_to_60pct": rejection_rate is not None and 0.05 <= rejection_rate <= 0.60,
        "P4_kept_mean_net_return_gt_0": bool(kept) and _mean(kept) > 0,
    }
    verdict = "FILTER_EFFECTIVE" if all(checks.values()) else "NO_INCREMENT"

    def exit_mix(group: list[TradeOutcome]) -> dict:
        """只有實際平倉者以出場原因歸類；窗末未平倉一律記 OPEN_AT_END（即使最後一日已觸發條件）。"""
        mix: dict[str, int] = {}
        for o in group:
            key = o.exit_reason if o.status == "CLOSED" else o.status
            mix[key] = mix.get(key, 0) + 1
        return dict(sorted(mix.items()))

    status_counts: dict[str, int] = {}
    for o in outcomes:
        status_counts[o.status] = status_counts.get(o.status, 0) + 1

    return {
        "candidates_total": len(candidates),
        "stale_signals_skipped": len(outcomes) - len(candidates),
        "status_counts": dict(sorted(status_counts.items())),
        "evaluated": len(evaluated),
        "kept_n": len(kept),
        "removed_n": len(removed),
        "tie_share": (sum(1 for o in candidates if o.tie) / len(candidates)) if candidates else None,
        "rejection_rate": rejection_rate,
        "mean_net_return_all": _mean(all_returns),
        "mean_net_return_kept": _mean(kept),
        "mean_net_return_removed": _mean(removed),
        "delta": delta,
        "delta_cluster_bootstrap_lower_5pct": delta_lower,
        "placebo_p_value": p_value,
        "minimum_detectable_delta": mde,
        "exit_mix_kept": exit_mix([o for o in evaluated if not o.filtered]),
        "exit_mix_removed": exit_mix([o for o in evaluated if o.filtered]),
        "checks": checks,
        "verdict": verdict,
        "seed": SEED,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "placebo_iterations": PLACEBO_ITERATIONS,
    }


def outcomes_as_dicts(outcomes: list[TradeOutcome]) -> list[dict]:
    return [asdict(o) for o in outcomes]
