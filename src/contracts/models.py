from datetime import date, datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict

class MinuteBar(BaseModel):
    time: datetime  # Expecting localized Asia/Taipei datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float

class MarketBar(BaseModel):
    symbol: str
    exchange: str
    instrument_type: str
    trade_date: date
    open: int  # price x 10000
    high: int  # price x 10000
    low: int   # price x 10000
    close: int # price x 10000
    volume: int
    amount: int  # TWD (integer)
    source: str
    source_timezone: str = "Asia/Taipei"
    is_complete: int = 1
    source_fetched_at: str
    raw_payload_checksum: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

# Strategy parameters model (defined in section 9.3)
class TrendPullbackParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ma_short: int = Field(default=20, ge=2)
    ma_long: int = Field(default=60, ge=5)
    stop_loss_bps: int = Field(default=500, ge=1, le=5000)
    take_profit_bps: int = Field(default=1200, ge=1, le=10000)
    order_budget_twd: int = Field(default=20000, ge=1000)


class ExitParams(BaseModel):
    """Per-strategy exit parameters (strategy YAML `exit:` block), executed by the risk_exit engine."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixed_stop_loss_bps: int = Field(ge=1, le=5000)
    trailing_stop_bps: int = Field(ge=1, le=5000)
    ma_break_period: int = Field(default=20, ge=2)
    ma_break_buffer_bps: int = Field(default=0, ge=0, le=2000)
    ma_break_confirm_days: int = Field(default=1, ge=1, le=10)
    time_stop_days: int = Field(ge=1)
    time_stop_min_return_bps: int = Field(ge=0)


class TrendBreakoutParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    breakout_lookback_days: int = Field(default=20, ge=5)
    volume_avg_days: int = Field(default=20, ge=5)
    volume_multiple_pct: int = Field(default=150, ge=100, le=1000)  # 150 = 1.5x
    ma_trend_period: int = Field(default=60, ge=5)
    index_ma_period: int = Field(default=60, ge=5)
    order_budget_twd: int = Field(default=20000, ge=1000)


class PullbackReboundParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ma_short: int = Field(default=20, ge=2)
    ma_long: int = Field(default=60, ge=5)
    pullback_touch_buffer_bps: int = Field(default=200, ge=0, le=1000)  # low <= sma_short * 1.02
    index_ma_period: int = Field(default=60, ge=5)
    order_budget_twd: int = Field(default=20000, ge=1000)

class StrategyInfo(BaseModel):
    strategy_id: str
    strategy_version: str
    params_canonicalization: str = "strategy-params-v1"
    params_hash: str

class PermissionsInfo(BaseModel):
    execution_modes: list[str]
    risk_increasing_actions: list[str]

class LimitsInfo(BaseModel):
    currency: str = "TWD"
    max_order_value: int
    max_daily_buy_value: int
    max_open_positions: int

class ValidityInfo(BaseModel):
    valid_from: str
    expires_at: str

class IntegrityInfo(BaseModel):
    algorithm: str = "sha256"
    canonicalization: str = "manifest-v1"
    digest: str

class StrategyApprovalManifest(BaseModel):
    schema_version: str = "1.0"
    approval_id: str
    issuer_id: str
    strategy: StrategyInfo
    permissions: PermissionsInfo
    limits: LimitsInfo
    validity: ValidityInfo
    integrity: IntegrityInfo

class SignalItem(BaseModel):
    signal_id: str
    symbol: str
    action: str
    reference_price: float
    reason_code: str
    ranking_score: Optional[float] = None
    # Owning strategy of the signal; SELLs from risk_exit carry the original
    # position's strategy_id so FIFO isolation and PnL attribution stay intact.
    strategy_id: Optional[str] = None
    signal_source: str = "ENTRY"  # ENTRY | RISK_EXIT | MANUAL


class DailySignalBundle(BaseModel):
    schema_version: str = "1.0"
    bundle_id: str
    run_id: str
    approval_id: str
    strategy: StrategyInfo
    signal_date: date
    target_execution_date: date
    market_data_cutoff: date
    signals: list[SignalItem]

class ExecutionContext(BaseModel):
    run_id: str
    run_type: Literal["BACKTEST", "DAILY_SIMULATION"]
    as_of_date: date
    execution_date: date
    account_id: str


