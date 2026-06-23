"""策略註冊表：strategy_id → 參數模型 / 進場策略工廠 / exit 參數。

新增策略時在此登錄參數模型與工廠，並建立 config/strategies/<id>.yaml；
risk_exit 監控範圍 = 「YAML 具有 exit: 區塊」的策略（MANUAL 結構性排除）。
"""
from dataclasses import dataclass
from typing import Callable, Optional
from pydantic import BaseModel

from src.config import AppSettings, StrategyConfig
from src.contracts.models import (
    TrendPullbackParams, TrendBreakoutParams, PullbackReboundParams, TrendRiderParams, ExitParams
)
from src.strategy.canonicalizer import StrategyParameterCanonicalizer


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    strategy_version: str
    params: BaseModel
    exit_params: Optional[ExitParams]
    params_hash: str
    order_budget_twd: int


PARAMS_MODELS: dict[str, type[BaseModel]] = {
    "trend_pullback": TrendPullbackParams,   # retired: exit-managed only, no new entries
    "trend_breakout": TrendBreakoutParams,
    "pullback_rebound": PullbackReboundParams,
    "trend_rider": TrendRiderParams,
}

# Strategies whose entry logic can be instantiated (trend_pullback is retired
# and intentionally has no factory here for the daily pipeline; backtest may
# still instantiate it explicitly).
def _build_trend_breakout(params, universe_symbols, index_symbol):
    from src.strategy.trend_breakout import TrendBreakoutStrategy
    return TrendBreakoutStrategy(params, universe_symbols, index_symbol)

def _build_pullback_rebound(params, universe_symbols, index_symbol):
    from src.strategy.pullback_rebound import PullbackReboundStrategy
    return PullbackReboundStrategy(params, universe_symbols, index_symbol)

def _build_trend_pullback(params, universe_symbols, index_symbol):
    from src.strategy.trend_pullback import TrendPullbackStrategy
    return TrendPullbackStrategy(params, universe_symbols)

def _build_trend_rider(params, universe_symbols, index_symbol):
    from src.strategy.trend_rider import TrendRiderStrategy
    return TrendRiderStrategy(params, universe_symbols, index_symbol)

ENTRY_FACTORIES: dict[str, Callable] = {
    "trend_breakout": _build_trend_breakout,
    "pullback_rebound": _build_pullback_rebound,
    "trend_pullback": _build_trend_pullback,  # legacy; not part of the default pipeline
    "trend_rider": _build_trend_rider,
}


def load_strategy_definition(settings: AppSettings, strategy_id: str) -> StrategyDefinition:
    if strategy_id not in PARAMS_MODELS:
        raise ValueError(f"UNKNOWN_STRATEGY: {strategy_id} 未登錄於 strategy registry")
    raw: StrategyConfig = settings.load_strategy_config(strategy_id)
    if raw.strategy_id != strategy_id:
        raise ValueError(
            f"STRATEGY_ID_MISMATCH: YAML strategy_id ({raw.strategy_id}) != 檔名 ({strategy_id})"
        )
    params = PARAMS_MODELS[strategy_id](**raw.parameters)
    exit_params = ExitParams(**raw.exit) if raw.exit is not None else None
    # exit 納入 hash（§2.10）：變更退出參數需重新簽發授權
    params_hash = StrategyParameterCanonicalizer.compute_strategy_hash(params, exit_params)
    return StrategyDefinition(
        strategy_id=strategy_id,
        strategy_version=raw.strategy_version,
        params=params,
        exit_params=exit_params,
        params_hash=params_hash,
        order_budget_twd=getattr(params, "order_budget_twd", 20000),
    )


def load_exit_managed_definitions(settings: AppSettings) -> dict[str, StrategyDefinition]:
    """All registered strategies whose YAML carries an exit: block (risk_exit scope)."""
    result = {}
    for strategy_id in PARAMS_MODELS:
        try:
            defn = load_strategy_definition(settings, strategy_id)
        except FileNotFoundError:
            continue
        if defn.exit_params is not None:
            result[strategy_id] = defn
    return result


def build_entry_strategy(defn: StrategyDefinition, universe_symbols: list[str], index_symbol: str):
    if defn.strategy_id not in ENTRY_FACTORIES:
        raise ValueError(f"NO_ENTRY_FACTORY: {defn.strategy_id} 無進場策略工廠")
    return ENTRY_FACTORIES[defn.strategy_id](defn.params, universe_symbols, index_symbol)
