import uuid
import sqlite3
import math
import random
import statistics
from datetime import date, datetime
from typing import Optional
from src.contracts.models import (
    DailySignalBundle, SignalItem, ExecutionContext, StrategyApprovalManifest, StrategyInfo
)
from src.calendar.calendar import TradingCalendar
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.strategy.registry import StrategyDefinition
from src.strategy.risk_exit import RiskExitEngine
from src.application.execution.engine import TradeExecutionEngine
from src.application.runners.simulation import EntryStrategySpec, _normalize_symbol_spec
from src.application.runners.fingerprint import compute_fingerprint, persist_fingerprint
from src.application.runners.verdict import evaluate_verdict, get_regime_gate_thresholds
from src.application.runners.research_ledger import record_research_attempt, count_research_trials
from src.trading.allocator import GlobalLimits

# 對 0050（市值型 ETF，買進持有 benchmark）算 Beta/Alpha（P1-T1）；非 settings.universe.indices
# 的大盤指數（那是給策略 regime filter 用）。
BENCHMARK_SYMBOL = "0050"
TRADING_DAYS_PER_YEAR = 252  # ponytail: 固定近似值，非實際 TWSE 年交易日數；精度要求提升時再用 calendar 推算
# P1-T5 穩健性檢定：固定種子保證可重現（呼應 P0-T8 版本指紋的 random_seed 欄位）。
BOOTSTRAP_RANDOM_SEED = 1337
BOOTSTRAP_ITERATIONS = 1000
BLOCK_BOOTSTRAP_BLOCK_SIZE = 5  # ponytail: 固定區塊長度近似週頻率自相關，非估計最佳區塊長度


def _max_drawdown_bound(peak_series: list, trough_series: list) -> float:
    """peak_series 與 trough_series 同一序列＝close-to-close 回撤；peak 用日高、trough 用日低
    則為刻意誇大的悲觀界線（worst_case_intraday_drawdown_bound）——非實際盤中同時發生的低點。"""
    peak = -1
    max_dd = 0.0
    for hi, lo in zip(peak_series, trough_series):
        if hi > peak:
            peak = hi
        dd = (peak - lo) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def _beta_alpha(equity_curve: list[dict], strategy_returns: list[float], mean_strategy_return: float):
    """對 BENCHMARK_SYMBOL 算 Beta/Alpha；benchmark 資料不全（如未落 0050 bar）時回 (None, None)，
    不假裝可算。"""
    benchmark_closes = [e.get("benchmark_close") for e in equity_curve]
    if len(benchmark_closes) < 2 or any(c is None for c in benchmark_closes):
        return None, None
    bench_returns = [
        (benchmark_closes[i] / benchmark_closes[i - 1] - 1.0) if benchmark_closes[i - 1] > 0 else 0.0
        for i in range(1, len(benchmark_closes))
    ]
    variance_bench = statistics.pvariance(bench_returns) if len(bench_returns) > 1 else 0.0
    if variance_bench <= 0:
        return None, None
    mean_bench_return = statistics.fmean(bench_returns)
    covariance = statistics.fmean(
        [(r - mean_strategy_return) * (b - mean_bench_return) for r, b in zip(strategy_returns, bench_returns)]
    )
    beta = covariance / variance_bench
    alpha = (mean_strategy_return * TRADING_DAYS_PER_YEAR) - beta * (mean_bench_return * TRADING_DAYS_PER_YEAR)
    return beta, alpha


def _deflated_sharpe_ratio(daily_returns: list[float], num_trials: int = 1) -> Optional[float]:
    """Deflated Sharpe Ratio（Bailey & Lopez de Prado）：用樣本偏度/峰度修正 Sharpe 的抽樣不確定性，
    並用 num_trials 修正多重檢定下「碰巧」出現高 Sharpe 的機率。num_trials=1（預設）時退化為對 SR=0
    的機率檢定（PSR）——試驗次數需接 Research Ledger（P2-T2）才有真實值，目前無來源故先假設單一試驗。"""
    n = len(daily_returns)
    if n < 3:
        return None
    mean_r = statistics.fmean(daily_returns)
    std_r = statistics.stdev(daily_returns)
    if std_r <= 0:
        return None
    sr_hat = mean_r / std_r  # 公式定義在「每期」尺度，非年化

    skew = statistics.fmean([(r - mean_r) ** 3 for r in daily_returns]) / (std_r ** 3)
    kurtosis = statistics.fmean([(r - mean_r) ** 4 for r in daily_returns]) / (std_r ** 4)

    sr_variance = (1 - skew * sr_hat + (kurtosis - 1) / 4 * sr_hat ** 2) / (n - 1)
    if sr_variance <= 0:
        return None

    normal = statistics.NormalDist()
    if num_trials <= 1:
        sr_0 = 0.0
    else:
        euler_mascheroni = 0.5772156649015329
        sr_0 = math.sqrt(sr_variance) * (
            (1 - euler_mascheroni) * normal.inv_cdf(1 - 1 / num_trials)
            + euler_mascheroni * normal.inv_cdf(1 - 1 / (num_trials * math.e))
        )

    return normal.cdf((sr_hat - sr_0) / math.sqrt(sr_variance))


def _block_bootstrap_annualized_return_ci(
    daily_returns: list[float], iterations: int, block_size: int, rng: random.Random
) -> tuple:
    """組合日報酬 block bootstrap（P1-T5）：以區塊重抽樣保留序列自相關，重算年化報酬分布的
    5%/95% CI。樣本不足兩個區塊時回 (None, None)，不假裝可估。"""
    n = len(daily_returns)
    if n < block_size * 2:
        return None, None
    num_blocks = -(-n // block_size)
    samples = []
    for _ in range(iterations):
        resampled = []
        for _ in range(num_blocks):
            start = rng.randrange(0, n - block_size + 1)
            resampled.extend(daily_returns[start:start + block_size])
        samples.append(statistics.fmean(resampled[:n]) * TRADING_DAYS_PER_YEAR)
    samples.sort()
    return samples[int(0.05 * iterations)], samples[int(0.95 * iterations) - 1]


def _iid_bootstrap_ci_lower(values: list[float], iterations: int, rng: random.Random) -> Optional[float]:
    """逐筆交易（視為近似獨立）bootstrap，回平均值分布的 5% CI 下界——餵 regime gate 的
    min_expectancy_ci_lower（P1-T7）。"""
    n = len(values)
    if n < 2:
        return None
    means = []
    for _ in range(iterations):
        means.append(statistics.fmean([values[rng.randrange(0, n)] for _ in range(n)]))
    means.sort()
    return means[int(0.05 * iterations)]


def _profit_herfindahl(trade_pnls: list) -> Optional[float]:
    """獲利 Herfindahl 集中度：只看正報酬交易對「總獲利」貢獻的平方和；越接近 1 代表
    獲利越依賴少數幾筆（餵 regime gate 的 max_profit_concentration，P1-T7）。"""
    profits = [p for p in trade_pnls if p > 0]
    total = sum(profits)
    if total <= 0:
        return None
    return sum((p / total) ** 2 for p in profits)


class BacktestRunner:
    """確定性回測：與每日模擬共用 TradeExecutionEngine 與 risk_exit 管線。
    單一進場策略 + risk_exit 出場引擎（與每日管線同構）。"""

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        calendar: TradingCalendar,
        market_repo: SqliteMarketBarRepository,
        projection: PortfolioProjection,
        allowed_issuers: list[str],
        revoked_approvals: list[str],
        manifest: StrategyApprovalManifest,
        strategy_budget: int,
        slippage_bps: int = 10,
        exit_definitions: Optional[dict[str, StrategyDefinition]] = None,
        index_symbols: Optional[list] = None,
        global_limits: Optional[GlobalLimits] = None
    ):
        self.db_conn = db_conn
        self.calendar = calendar
        self.market_repo = market_repo
        self.projection = projection
        self.allowed_issuers = allowed_issuers
        self.revoked_approvals = revoked_approvals
        self.manifest = manifest
        self.strategy_budget = strategy_budget
        self.slippage_bps = slippage_bps
        self.exit_definitions = exit_definitions or {}
        self.index_symbols = [_normalize_symbol_spec(s) for s in (index_symbols or [])]
        # Single-strategy backtest: global limits default to the manifest limits.
        self.global_limits = global_limits or GlobalLimits(
            max_open_positions=manifest.limits.max_open_positions,
            max_daily_buy_value=manifest.limits.max_daily_buy_value,
            max_new_positions_per_day=2
        )

    def run(
        self,
        start_date: date,
        end_date: date,
        initial_cash: int,
        universe_symbols: list[str],
        entry_spec: EntryStrategySpec
    ) -> dict:
        run_id = f"bt-{uuid.uuid4().hex[:8]}"
        account_id = f"backtest:{run_id}"
        strategy_id = entry_spec.definition.strategy_id

        # 1. Initialize cash deposit
        ledger = PortfolioLedger(self.db_conn)
        ledger.deposit(account_id, run_id, initial_cash, "TWD", start_date)
        self.projection.rebuild_from_ledger(account_id)

        sessions = self.calendar.sessions_between(start_date, end_date)
        if not sessions:
            raise ValueError(f"No trading days found between {start_date} and {end_date}")

        equity_curve = []
        # 缺檔當日跳過該檔、不中止整窗（P0-T6）：累計每檔缺檔天數，結束後輸出剔除清單+比例，
        # 而非讓單一標的缺資料縮短整個回測窗（重現舊版 DATASET_INCOMPLETE 行為）。
        missing_days: dict[str, int] = {s: 0 for s in universe_symbols}
        last_known_close: dict[str, int] = {}
        last_known_high: dict[str, int] = {}
        last_known_low: dict[str, int] = {}
        last_known_benchmark_close: Optional[int] = None
        # 等權 universe buy-hold benchmark（P1-T3）：每檔窗內第一筆/最後一筆已知 close。
        first_close: dict[str, int] = {}
        last_close: dict[str, int] = {}

        for D in sessions:
            for symbol in universe_symbols:
                bar = self.market_repo.find(symbol, D)
                if bar is None:
                    missing_days[symbol] += 1
                    continue
                first_close.setdefault(symbol, bar.close)
                last_close[symbol] = bar.close

            # A. Execute any PENDING bundles targeting D
            bundles = self._find_bundles_by_execution_date(run_id, D)
            if bundles:
                context = ExecutionContext(
                    run_id=run_id,
                    run_type="BACKTEST",
                    as_of_date=bundles[0].signal_date,
                    execution_date=D,
                    account_id=account_id
                )
                engine = TradeExecutionEngine(
                    db_conn=self.db_conn,
                    market_repo=self.market_repo,
                    projection=self.projection,
                    allowed_issuers=self.allowed_issuers,
                    revoked_approvals=self.revoked_approvals,
                    manifests={strategy_id: self.manifest},
                    strategy_budgets={strategy_id: self.strategy_budget},
                    global_limits=self.global_limits,
                    pipeline_order=[strategy_id],
                    slippage_bps=self.slippage_bps
                )
                engine.execute_bundles(context, bundles)

            # B. Persist trailing-stop watermarks at D close (before exit evaluation)
            self._update_watermarks(account_id, D)

            # C. Generate new signals as of D close: risk_exit first, then entry
            pit_data = self.market_repo.as_of(D)
            target_execution_date = self.calendar.next_trading_day(D)

            risk_exit = RiskExitEngine(self.exit_definitions, self.projection, self.calendar)
            for bundle in risk_exit.generate_exit_bundles(D, account_id, pit_data, run_id):
                self._save_bundle(bundle, target_execution_date)

            strategy_positions = self.projection.get_strategy_positions(account_id, include_long_term=True)
            positions = {}
            for (pos_sid, symbol), pos in strategy_positions.items():
                if pos_sid != strategy_id or pos["quantity"] <= 0:
                    continue
                positions[symbol] = PositionSnapshot(
                    symbol=symbol,
                    quantity=pos["quantity"],
                    entry_price=pos["wavg_price"],
                    is_long_term=pos["is_long_term"]
                )
            available_cash = self.projection.get_cash_balance(account_id)
            portfolio_snapshot = PortfolioSnapshot(
                available_cash=available_cash,
                positions=positions
            )

            sig_ctx = SignalGenerationContext(
                as_of_date=D,
                strategy_id=strategy_id,
                strategy_version=entry_spec.definition.strategy_version,
                run_id=run_id,
                approval_id=self.manifest.approval_id,
                params_hash=self.manifest.strategy.params_hash
            )
            new_bundle = entry_spec.strategy.generate(sig_ctx, pit_data, portfolio_snapshot)
            self._save_bundle(new_bundle, target_execution_date)

            # D. Record Equity for session D (all open positions for this account)
            # equity_high/equity_low：用當日 high/low 估值，供 worst_case_intraday_drawdown_bound（P1-T1）
            # 用——刻意誇大的悲觀界線，非同時發生的真實盤中低點。
            pos_value = 0
            pos_value_high = 0
            pos_value_low = 0
            for (_sid, symbol), pos in strategy_positions.items():
                if pos["quantity"] <= 0:
                    continue
                bar = self.market_repo.find(symbol, D)
                if bar is not None:
                    last_known_close[symbol] = bar.close
                    last_known_high[symbol] = bar.high
                    last_known_low[symbol] = bar.low
                # 缺檔當日（停牌等）沿用最後已知價估值，而非中止整個回測
                close = last_known_close.get(symbol, bar.close if bar else 0)
                high = last_known_high.get(symbol, bar.high if bar else 0)
                low = last_known_low.get(symbol, bar.low if bar else 0)
                pos_value += int(pos["quantity"] * close // 10000)
                pos_value_high += int(pos["quantity"] * high // 10000)
                pos_value_low += int(pos["quantity"] * low // 10000)

            benchmark_bar = self.market_repo.find(BENCHMARK_SYMBOL, D)
            if benchmark_bar is not None:
                last_known_benchmark_close = benchmark_bar.close

            equity_curve.append({
                "date": D,
                "cash": available_cash,
                "position_value": pos_value,
                "equity": available_cash + pos_value,
                "equity_high": available_cash + pos_value_high,
                "equity_low": available_cash + pos_value_low,
                "benchmark_close": last_known_benchmark_close,
            })

        # 3. Calculate Backtest Statistics
        stats = self._calculate_statistics(account_id, initial_cash, equity_curve)
        benchmarks = self._calculate_benchmarks(equity_curve, first_close, last_close)
        return_layers = self._calculate_return_layers(account_id, initial_cash, stats["final_equity"])

        # Research Ledger（P2-T2）：本次 run 入帳（append-only），再算同 strategy_id 累積試驗次數
        # 餵 DSR num_trials——取代 P1-T5 遺留的 num_trials=1 占位假設。
        record_research_attempt(
            self.db_conn, strategy_id=strategy_id, strategy_version=entry_spec.definition.strategy_version,
            params_hash=entry_spec.definition.params_hash, run_id=run_id,
        )
        num_trials = count_research_trials(self.db_conn, strategy_id)
        robustness = self._calculate_robustness_stats(account_id, equity_curve, num_trials=num_trials)
        cost_ratio = self._calculate_cost_ratio(account_id)
        yearly_breakdown = self._calculate_yearly_breakdown(account_id, equity_curve)
        total_sessions = len(sessions)
        data_availability = {
            symbol: {
                "missing_days": missing,
                "missing_ratio": round(missing / total_sessions, 4) if total_sessions else 0.0,
            }
            for symbol, missing in missing_days.items()
        }
        # 剔除清單：整窗完全無資料的標的（如上市日晚於窗起點），非僅部分缺檔
        excluded_symbols = [s for s, m in missing_days.items() if m == total_sessions]

        # 版本指紋（P0-T8）：同指紋應重現同結果，否則可反推差異來自資料/程式/參數哪一項。
        fingerprint = compute_fingerprint(
            conn=self.db_conn,
            run_id=run_id,
            strategy_version=entry_spec.definition.strategy_version,
            params_hash=entry_spec.definition.params_hash,
            universe_symbols=universe_symbols,
            # self.index_symbols 是 _normalize_symbol_spec 後的 dict；fingerprint 要的是 symbol code 字串
            # （與 universe_symbols 一致，用於 set/SQL IN/hash），故取 code。
            index_symbols=[s["code"] for s in self.index_symbols],
            start_date=start_date,
            end_date=end_date,
            slippage_bps=self.slippage_bps,
            initial_cash=initial_cash,
            manifest_digest=self.manifest.integrity.digest,
            random_seed=BOOTSTRAP_RANDOM_SEED,
        )
        persist_fingerprint(self.db_conn, fingerprint)

        result = {
            "run_id": run_id,
            "account_id": account_id,
            "equity_curve": equity_curve,
            "statistics": stats,
            "data_availability": data_availability,
            "excluded_symbols": excluded_symbols,
            "fingerprint": fingerprint,
            "benchmarks": benchmarks,
            "return_layers": return_layers,
            "robustness": robustness,
            "cost_ratio": cost_ratio,
            "yearly_breakdown": yearly_breakdown,
        }

        # S1 裁決（P1-T7）：今日固定清單目前一律落 universe_snapshot_id="diagnostic:..."
        # （見 fingerprint.py，真 PIT policy 尚未建），故現役策略現況必為 INVALID+diagnostic_result。
        is_diagnostic_universe = fingerprint["universe_snapshot_id"].startswith("diagnostic:")
        regime_gate = get_regime_gate_thresholds(self.db_conn, strategy_id, entry_spec.definition.strategy_version)
        result["verdict"] = evaluate_verdict(result, start_date, end_date, is_diagnostic_universe, regime_gate)

        return result

    def _update_watermarks(self, account_id: str, as_of_date: date) -> None:
        positions = self.projection.get_strategy_positions(account_id, include_long_term=False)
        for (strategy_id, symbol), pos in positions.items():
            if strategy_id == MANUAL_STRATEGY_ID or strategy_id not in self.exit_definitions:
                continue
            bar = self.market_repo.find(symbol, as_of_date)
            if not bar:
                continue
            self.projection.upsert_high_watermark(
                account_id, strategy_id, symbol, as_of_date.isoformat(), bar.close
            )

    def _save_bundle(self, bundle: DailySignalBundle, target_execution_date: date) -> None:
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT 1 FROM signal_bundles WHERE bundle_id = ?", (bundle.bundle_id,))
        if cursor.fetchone():
            return
        cursor.execute(
            """
            INSERT INTO signal_bundles (
                bundle_id, run_id, approval_id, strategy_id, strategy_version,
                params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                bundle.bundle_id,
                bundle.run_id,
                bundle.approval_id,
                bundle.strategy.strategy_id,
                bundle.strategy.strategy_version,
                bundle.strategy.params_hash,
                bundle.signal_date.isoformat(),
                target_execution_date.isoformat(),
                bundle.market_data_cutoff.isoformat()
            )
        )
        for sig in bundle.signals:
            cursor.execute(
                """
                INSERT INTO signal_items (
                    item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at, signal_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (
                    f"item-{uuid.uuid4().hex[:8]}",
                    bundle.bundle_id,
                    sig.signal_id,
                    sig.symbol,
                    sig.action,
                    int(round(sig.reference_price * 10000)),
                    sig.reason_code,
                    sig.signal_source
                )
            )
        self.db_conn.commit()

    def _find_bundles_by_execution_date(self, run_id: str, execution_date: date) -> list[DailySignalBundle]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
            FROM signal_bundles
            WHERE target_execution_date = ? AND run_id = ?
            ORDER BY bundle_id ASC
            """,
            (execution_date.isoformat(), run_id)
        )
        bundles = []
        for r in cursor.fetchall():
            cursor2 = self.db_conn.cursor()
            cursor2.execute(
                """
                SELECT signal_id, symbol, action, reference_price, reason_code, signal_source
                FROM signal_items
                WHERE bundle_id = ?
                """,
                (r["bundle_id"],)
            )
            items = []
            for row in cursor2.fetchall():
                items.append(
                    SignalItem(
                        signal_id=row["signal_id"],
                        symbol=row["symbol"],
                        action=row["action"],
                        reference_price=float(row["reference_price"] / 10000.0),
                        reason_code=row["reason_code"],
                        strategy_id=r["strategy_id"],
                        signal_source=row["signal_source"] or "ENTRY"
                    )
                )

            strategy_info = StrategyInfo(
                strategy_id=r["strategy_id"],
                strategy_version=r["strategy_version"],
                params_canonicalization="strategy-params-v1",
                params_hash=r["params_hash"]
            )
            bundles.append(
                DailySignalBundle(
                    schema_version="1.0",
                    bundle_id=r["bundle_id"],
                    run_id=r["run_id"],
                    approval_id=r["approval_id"],
                    strategy=strategy_info,
                    signal_date=date.fromisoformat(r["signal_date"]),
                    target_execution_date=date.fromisoformat(r["target_execution_date"]),
                    market_data_cutoff=date.fromisoformat(r["market_data_cutoff"]),
                    signals=items
                )
            )
        return bundles

    def _calculate_statistics(self, account_id: str, initial_cash: int, equity_curve: list[dict]) -> dict:
        cursor = self.db_conn.cursor()

        cursor.execute(
            """
            SELECT fm.realized_pnl, fm.strategy_id, fm.buy_price, fm.sell_price, fm.matched_at,
                   bf.filled_at AS buy_filled_at
            FROM fifo_matches fm
            JOIN fills bf ON bf.fill_id = fm.buy_fill_id
            WHERE fm.account_id = ?
            """,
            (account_id,)
        )
        match_rows = cursor.fetchall()
        matches = [r["realized_pnl"] for r in match_rows]

        trade_count = len(matches)
        win_count = sum(1 for p in matches if p > 0)
        loss_count = sum(1 for p in matches if p < 0)
        win_rate = win_count / trade_count if trade_count > 0 else 0.0

        total_profit = sum(p for p in matches if p > 0)
        total_loss = abs(sum(p for p in matches if p < 0))

        profit_factor = total_profit / total_loss if total_loss > 0 else (float('inf') if total_profit > 0 else 0.0)

        avg_profit = total_profit / win_count if win_count > 0 else 0.0
        avg_loss = total_loss / loss_count if loss_count > 0 else 0.0
        # 賺賠比（payoff ratio）：與 profit_factor 不同——只比「贏的時候贏多少 vs 輸的時候輸多少」，
        # 不看勝率。
        payoff_ratio = avg_profit / avg_loss if avg_loss > 0 else (float('inf') if avg_profit > 0 else 0.0)

        # Expectancy(R)：每筆損益 ÷ 該筆進場時的初始風險（買價 × 策略 fixed_stop_loss_bps）。
        # 沒有 exit 區塊（如 MANUAL 或無 fixed_stop_loss_bps）的交易無可比的 R 基準，排除而非假設。
        r_multiples = []
        for row in match_rows:
            definition = self.exit_definitions.get(row["strategy_id"]) if hasattr(self, "exit_definitions") else None
            exit_params = definition.exit_params if definition else None
            if exit_params is None or exit_params.fixed_stop_loss_bps <= 0 or row["buy_price"] <= 0:
                continue
            risk_per_share = row["buy_price"] * exit_params.fixed_stop_loss_bps / 10000.0
            r_multiples.append((row["sell_price"] - row["buy_price"]) / risk_per_share)
        expectancy_r = statistics.fmean(r_multiples) if r_multiples else None
        expectancy_r_sample_size = len(r_multiples)

        # 平均持有期（日曆天）：sell 撮合時間（matched_at）− buy 成交時間（buy_filled_at）。
        holding_days = []
        for row in match_rows:
            try:
                held = (datetime.fromisoformat(row["matched_at"]) - datetime.fromisoformat(row["buy_filled_at"])).days
                holding_days.append(held)
            except ValueError:
                continue
        avg_holding_period_days = statistics.fmean(holding_days) if holding_days else 0.0

        # 換手率：本窗總成交金額（買+賣，依 fills 全量含未平倉腳）÷ 期間平均權益。
        cursor.execute("SELECT quantity, price FROM fills WHERE account_id = ?", (account_id,))
        total_traded_value = sum(int(row["quantity"] * row["price"] // 10000) for row in cursor.fetchall())

        equities = [e["equity"] for e in equity_curve]
        equity_highs = [e.get("equity_high", e["equity"]) for e in equity_curve]
        equity_lows = [e.get("equity_low", e["equity"]) for e in equity_curve]
        n = len(equities)

        average_equity = statistics.fmean(equities) if equities else initial_cash
        turnover_rate = total_traded_value / average_equity if average_equity > 0 else 0.0

        # 回撤分三個不混用的輸出（P1-T1）：close_to_close 可精確重現；worst_case 為日高/低推估的
        # 悲觀界線（不得宣稱為實際盤中同時發生）；timestamped 需分鐘資料，目前無來源故維持 None。
        close_to_close_maxdd = _max_drawdown_bound(equities, equities)
        worst_case_intraday_drawdown_bound = _max_drawdown_bound(equity_highs, equity_lows)
        timestamped_intraday_maxdd = None

        daily_returns = [
            (equities[i] / equities[i - 1] - 1.0) if equities[i - 1] > 0 else 0.0
            for i in range(1, n)
        ]
        mean_daily_return = statistics.fmean(daily_returns) if daily_returns else 0.0
        annualized_return = mean_daily_return * TRADING_DAYS_PER_YEAR
        annualized_volatility = (
            statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS_PER_YEAR) if len(daily_returns) > 1 else 0.0
        )
        sharpe_ratio = (
            annualized_return / annualized_volatility if annualized_volatility > 0
            else (float('inf') if annualized_return > 0 else 0.0)
        )

        downside_sq_mean = statistics.fmean([min(r, 0.0) ** 2 for r in daily_returns]) if daily_returns else 0.0
        downside_deviation = math.sqrt(downside_sq_mean) * math.sqrt(TRADING_DAYS_PER_YEAR)
        sortino_ratio = (
            annualized_return / downside_deviation if downside_deviation > 0
            else (float('inf') if annualized_return > 0 else 0.0)
        )

        final_equity = equities[-1] if equities else initial_cash
        cagr = (
            (final_equity / initial_cash) ** (TRADING_DAYS_PER_YEAR / n) - 1.0
            if initial_cash > 0 and n > 0 else 0.0
        )
        calmar_ratio = (
            cagr / close_to_close_maxdd if close_to_close_maxdd > 0
            else (float('inf') if cagr > 0 else 0.0)
        )

        beta_vs_benchmark, alpha_vs_benchmark = _beta_alpha(equity_curve, daily_returns, mean_daily_return)

        total_pnl = final_equity - initial_cash
        total_pnl_bps = int(round(total_pnl / initial_cash * 10000)) if initial_cash > 0 else 0

        return {
            "initial_cash": initial_cash,
            "final_equity": final_equity,
            "total_pnl": total_pnl,
            "total_pnl_bps": total_pnl_bps,
            # 沿用舊欄名＝close_to_close_maxdd，避免破壞既有報表/CLI/web 消費端
            "max_drawdown": close_to_close_maxdd,
            "close_to_close_maxdd": close_to_close_maxdd,
            "worst_case_intraday_drawdown_bound": worst_case_intraday_drawdown_bound,
            "timestamped_intraday_maxdd": timestamped_intraday_maxdd,
            "cagr": cagr,
            "annualized_volatility": annualized_volatility,
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "calmar_ratio": calmar_ratio,
            "beta_vs_benchmark": beta_vs_benchmark,
            "alpha_vs_benchmark": alpha_vs_benchmark,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "payoff_ratio": payoff_ratio,
            "avg_profit": avg_profit,
            "avg_loss": avg_loss,
            "expectancy_r": expectancy_r,
            "expectancy_r_sample_size": expectancy_r_sample_size,
            "turnover_rate": turnover_rate,
            "avg_holding_period_days": avg_holding_period_days,
            "trade_count": trade_count
        }

    def _calculate_benchmarks(self, equity_curve: list[dict], first_close: dict, last_close: dict) -> dict:
        """S3 四條 benchmark（P1-T3）：①0050 原始買進持有 ②同平均曝險 0050+現金
        ③同波動目標 0050（槓桿係數縮放 0050 日報酬） ④等權 universe 買進持有
        （判斷選股是否優於同標的池，非只大盤 Beta）。資料不足時回 None，不假裝可算。"""
        benchmark_closes = [e.get("benchmark_close") for e in equity_curve]
        known_closes = [c for c in benchmark_closes if c is not None]
        raw_0050_return = (
            known_closes[-1] / known_closes[0] - 1.0
            if len(known_closes) >= 2 and known_closes[0] > 0 else None
        )

        exposures = [
            e.get("position_value", 0) / e["equity"] for e in equity_curve if e.get("equity", 0) > 0
        ]
        avg_exposure = statistics.fmean(exposures) if exposures else 0.0
        same_exposure_return = avg_exposure * raw_0050_return if raw_0050_return is not None else None

        equities = [e["equity"] for e in equity_curve]
        strategy_returns = [
            (equities[i] / equities[i - 1] - 1.0) if equities[i - 1] > 0 else 0.0
            for i in range(1, len(equities))
        ]
        bench_returns = []
        for i in range(1, len(benchmark_closes)):
            prev, curr = benchmark_closes[i - 1], benchmark_closes[i]
            bench_returns.append(curr / prev - 1.0 if prev and curr is not None and prev > 0 else None)

        vol_matched_return = None
        if raw_0050_return is not None and len(strategy_returns) > 1 and bench_returns and all(b is not None for b in bench_returns):
            bench_vol = statistics.stdev(bench_returns)
            if bench_vol > 0:
                # ponytail: 單一槓桿係數縮放 0050 報酬以匹配策略波動，忽略現金拖累的複利細節
                leverage = statistics.stdev(strategy_returns) / bench_vol
                vol_matched_return = leverage * raw_0050_return

        paired_returns = [
            last_close[s] / first_close[s] - 1.0
            for s in first_close if s in last_close and first_close[s] > 0
        ]
        equal_weight_universe_return = statistics.fmean(paired_returns) if paired_returns else None

        return {
            "benchmark_0050_buy_hold": raw_0050_return,
            "benchmark_0050_same_exposure": same_exposure_return,
            "benchmark_0050_vol_matched": vol_matched_return,
            "benchmark_equal_weight_universe": equal_weight_universe_return,
        }

    def _calculate_return_layers(self, account_id: str, initial_cash: int, final_equity: int) -> dict:
        """S2 報酬分層（P1-T4，Phase 1 範圍）。
        ①raw_signal_return：逐筆訊號報酬不計部位大小（訊號本身有無預測力），仍含實際成交滑價
        （buy/sell 為成交價而非理論訊號價，無法事後拆分）。
        ②③strategy/full_system_portfolio_return：本 runner 僅單策略單帳戶，尚無多策略 allocator
        疊加（P3-T4 後才可分離），故②③暫同——皆為補回手續費+交易稅後的總報酬，分離出
        「部位大小+持倉」效果，但仍含滑價。
        ④modeled_executable_return：本次模擬實際總報酬（已含 P0-T7 滑價/UNFILLED/零股折損+稅費）。
        """
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT buy_price, sell_price FROM fifo_matches WHERE account_id = ?", (account_id,))
        per_trade_returns = [
            (m["sell_price"] - m["buy_price"]) / m["buy_price"]
            for m in cursor.fetchall() if m["buy_price"] > 0
        ]
        raw_signal_return = statistics.fmean(per_trade_returns) if per_trade_returns else None

        total_fee_tax_paid = self._total_fee_tax_paid(account_id)

        modeled_executable_return = final_equity / initial_cash - 1.0 if initial_cash > 0 else 0.0
        strategy_portfolio_return = (
            (final_equity + total_fee_tax_paid) / initial_cash - 1.0 if initial_cash > 0 else 0.0
        )

        return {
            "raw_signal_return": raw_signal_return,
            "strategy_portfolio_return": strategy_portfolio_return,
            "full_system_portfolio_return": strategy_portfolio_return,
            "modeled_executable_return": modeled_executable_return,
            "fee_tax_drag": strategy_portfolio_return - modeled_executable_return,
        }

    def _calculate_robustness_stats(self, account_id: str, equity_curve: list[dict], num_trials: int = 1) -> dict:
        """P1-T5 多重檢定/穩健：DSR、組合日報酬 block bootstrap、進場日聚類有效樣本數、
        去最佳1%/5筆/最佳月重算、獲利 Herfindahl。**有效樣本數作 gate，原始筆數只作資訊**。
        產業/事件聚類需要尚未建立的產業分類資料來源，目前僅按進場日聚類（同日進場視為高度相關，
        保守下界，非精確 design-effect rho 估計）——未來工作，非假資料填補。
        num_trials 預設 1（無 Research Ledger 來源時的占位假設）；run() 會改傳 P2-T2
        Research Ledger 算出的同 strategy_id 累積試驗次數。"""
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT fm.realized_pnl, fm.matched_at, bf.filled_at AS buy_filled_at
            FROM fifo_matches fm JOIN fills bf ON bf.fill_id = fm.buy_fill_id
            WHERE fm.account_id = ?
            """,
            (account_id,)
        )
        rows = cursor.fetchall()
        trade_pnls = [r["realized_pnl"] for r in rows]
        entry_dates = [r["buy_filled_at"][:10] for r in rows]

        rng = random.Random(BOOTSTRAP_RANDOM_SEED)

        equities = [e["equity"] for e in equity_curve]
        daily_returns = [
            (equities[i] / equities[i - 1] - 1.0) if equities[i - 1] > 0 else 0.0
            for i in range(1, len(equities))
        ]

        ci_lower, ci_upper = _block_bootstrap_annualized_return_ci(
            daily_returns, BOOTSTRAP_ITERATIONS, BLOCK_BOOTSTRAP_BLOCK_SIZE, rng
        )
        expectancy_ci_lower = _iid_bootstrap_ci_lower(trade_pnls, BOOTSTRAP_ITERATIONS, rng)

        total_pnl = sum(trade_pnls)
        sorted_desc = sorted(trade_pnls, reverse=True)
        pnl_excl_1pct = None
        pnl_excl_5 = None
        if trade_pnls:
            k_1pct = max(1, round(len(sorted_desc) * 0.01))
            pnl_excl_1pct = sum(sorted_desc[k_1pct:])
            pnl_excl_5 = sum(sorted_desc[min(5, len(sorted_desc)):])

        pnl_excl_best_month = None
        if rows:
            monthly: dict[str, int] = {}
            for r in rows:
                month_key = r["matched_at"][:7]
                monthly[month_key] = monthly.get(month_key, 0) + r["realized_pnl"]
            pnl_excl_best_month = total_pnl - max(monthly.values())

        return {
            "trade_count_raw": len(trade_pnls),
            "effective_sample_size": len(set(entry_dates)),
            "deflated_sharpe_ratio": _deflated_sharpe_ratio(daily_returns, num_trials=num_trials),
            "num_trials_assumed": num_trials,
            "annualized_return_bootstrap_ci_lower": ci_lower,
            "annualized_return_bootstrap_ci_upper": ci_upper,
            "expectancy_bootstrap_ci_lower": expectancy_ci_lower,
            "profit_herfindahl_concentration": _profit_herfindahl(trade_pnls),
            "pnl_excluding_best_1pct_trades": pnl_excl_1pct,
            "pnl_excluding_best_5_trades": pnl_excl_5,
            "pnl_excluding_best_month": pnl_excl_best_month,
        }

    def _total_fee_tax_paid(self, account_id: str) -> int:
        cursor = self.db_conn.cursor()
        cursor.execute(
            "SELECT -SUM(amount) AS total FROM cash_ledger "
            "WHERE account_id = ? AND event_type IN ('BROKER_FEE', 'TRANSACTION_TAX')",
            (account_id,)
        )
        return cursor.fetchone()["total"] or 0

    def _calculate_cost_ratio(self, account_id: str) -> dict:
        """成本占已實現損益比例（P1-T6）：手續費+交易稅 ÷ 已實現毛損益（fifo_matches.realized_pnl，
        未扣費稅），回答「edge 是否幾乎全被費稅吃掉」。毛損益 ≤0 時沒有「邊際被吃掉多少」可言，回 None。
        滑價已內嵌成交價、無法事後乾淨拆出，不計入此比例（與 P1-T4 報酬分層同一限制）。"""
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT SUM(realized_pnl) AS total FROM fifo_matches WHERE account_id = ?", (account_id,))
        gross_realized_pnl = cursor.fetchone()["total"] or 0
        total_fee_tax_paid = self._total_fee_tax_paid(account_id)

        return {
            "gross_realized_pnl": gross_realized_pnl,
            "total_fee_tax_paid": total_fee_tax_paid,
            "cost_to_gross_pnl_ratio": (
                total_fee_tax_paid / gross_realized_pnl if gross_realized_pnl > 0 else None
            ),
        }

    def _calculate_yearly_breakdown(self, account_id: str, equity_curve: list[dict]) -> list[dict]:
        """分年表（P1-T6）：逐年 equity 報酬 + 已實現毛損益/勝率，用來看「賺得穩不穩」——
        整段彙總可能只是某一年撐起其餘年全虧。每年 start_equity 接續上一年 end_equity（非該年
        第一個資料點），首年則用該年自身首點（回測本就從那裡開始，無更早資料）。"""
        if not equity_curve:
            return []

        cursor = self.db_conn.cursor()
        cursor.execute("SELECT realized_pnl, matched_at FROM fifo_matches WHERE account_id = ?", (account_id,))
        matches_by_year: dict[int, list[int]] = {}
        for row in cursor.fetchall():
            year = int(row["matched_at"][:4])
            matches_by_year.setdefault(year, []).append(row["realized_pnl"])

        years_seen = sorted({e["date"].year for e in equity_curve})
        breakdown = []
        prev_year_end_equity = None
        for year in years_seen:
            year_points = [e for e in equity_curve if e["date"].year == year]
            start_equity = prev_year_end_equity if prev_year_end_equity is not None else year_points[0]["equity"]
            end_equity = year_points[-1]["equity"]
            year_pnls = matches_by_year.get(year, [])
            trade_count = len(year_pnls)
            win_count = sum(1 for p in year_pnls if p > 0)

            breakdown.append({
                "year": year,
                "start_equity": start_equity,
                "end_equity": end_equity,
                "year_return": end_equity / start_equity - 1.0 if start_equity > 0 else 0.0,
                "realized_pnl": sum(year_pnls),
                "trade_count": trade_count,
                "win_rate": win_count / trade_count if trade_count > 0 else 0.0,
            })
            prev_year_end_equity = end_equity
        return breakdown
