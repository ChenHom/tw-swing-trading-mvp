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
    # 雙價模型：同一 (symbol, trade_date) 在 research.db 可有 raw 與 adjusted 兩筆 canonical bar。
    # price_basis='raw' 時 open/high/low/close 為原始成交價；'adjusted' 時為還原權值後價格，
    # adjustment_factor 為該日對 raw 的累積還原因子（adjusted = raw * adjustment_factor）。
    price_basis: Literal["raw", "adjusted"] = "raw"
    adjustment_factor: float = 1.0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class CorporateAction(BaseModel):
    action_id: str
    symbol: str
    action_type: Literal["CASH_DIVIDEND", "STOCK_DIVIDEND", "SPLIT", "CAPITAL_REDUCTION"]
    ex_date: date
    cash_per_share: Optional[int] = None  # 元 x 10000
    stock_ratio: Optional[float] = None
    source: str
    memo: Optional[str] = None
    # PIT 時間語意：effective_date 為調整生效日（多數情況=ex_date）；known_at 為此事件
    # 「公開可得」的時間點——回測 D 日只能讀 known_at <= D 的事件，避免用到日後才公告的公司行動。
    effective_date: Optional[date] = None
    known_at: Optional[str] = None
    ingested_at: Optional[str] = None
    source_payload_hash: Optional[str] = None
    created_at: Optional[str] = None

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


class TrendRiderParams(BaseModel):
    """順勢交易者（讓贏家跑）進場參數。進場選「確立的中期上升趨勢」，出場交 risk_exit 以
    寬鬆 exit config 實現「讓贏家跑」（time_stop 停用、寬移動停利、長均線跌破）。
    保留 index 60MA 濾網作崩盤防守。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    trend_ma_period: int = Field(default=60, ge=5)          # close 須在此上升均線之上
    breakout_lookback_days: int = Field(default=60, ge=10)  # 創 N 日新高＝確立趨勢（較 breakout 的 20 長）
    index_ma_period: int = Field(default=60, ge=5)          # 大盤多頭濾網（崩盤防守）
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


