import os
import json
import yaml
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, ValidationError
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

class StrategyConfig(BaseModel):
    """Raw strategy YAML. `parameters`/`exit` are validated against the per-strategy
    models in src/strategy/registry.py (this module stays schema-agnostic)."""
    strategy_id: str
    strategy_version: str
    parameters: dict
    exit: Optional[dict] = None

class BacktestConfig(BaseModel):
    initial_cash_twd: int
    slippage_bps: int
    fee_model_version: str

class ApprovalConfig(BaseModel):
    expiry_warning_sessions: int = 3
    allowlist_path: str = "config/issuer-allowlist.json"
    revoked_path: str = "config/revoked-approvals.json"
    active_pointer_path: str = "artifacts/approvals/active-approval.json"  # legacy single pointer
    active_map_path: str = "artifacts/approvals/active-approvals.json"      # strategy_id -> approval_id
    approvals_dir: str = "artifacts/approvals/"

class PipelineConfig(BaseModel):
    # Deterministic daily order: risk_exit runs first, then these entry strategies.
    entry_strategies: list[str] = Field(default_factory=lambda: ["trend_breakout", "pullback_rebound"])
    index_symbol: str = "TSE"
    # Per-account entry-strategy override (account_id -> strategy list). Lets the real
    # account (國泰) and the shadow account run different strategies — e.g. retire a
    # REJECTED strategy from real while keeping it observable on shadow. Falls back to
    # entry_strategies when an account isn't listed. SELL/risk_exit is unaffected
    # (exits are per-account and never gated by this).
    account_overrides: dict[str, list[str]] = Field(default_factory=dict)
    # Per-account universe override (account_id -> universe_policy.policy_version).
    # Absent/None → fixed config/universe.yaml list (current behaviour). Lets the
    # shadow account screen a broad PIT liquidity universe while the real account
    # (國泰) stays on the curated fixed list. Falls back to fixed when the policy
    # has no constituents in app.db yet (e.g. data not backfilled).
    universe_overrides: dict[str, str] = Field(default_factory=dict)

class GlobalLimitsConfig(BaseModel):
    max_open_positions: int = 8
    max_daily_buy_value: int = 200000
    max_new_positions_per_day: int = 2

class TradingConfig(BaseModel):
    database_path: str = "data/app.db"
    approval: ApprovalConfig
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    global_limits: GlobalLimitsConfig = Field(default_factory=GlobalLimitsConfig)

class SymbolConfig(BaseModel):
    code: str
    exchange: str
    instrument_type: str

class UniverseConfig(BaseModel):
    symbols: list[SymbolConfig]
    indices: list[SymbolConfig] = Field(default_factory=list)

class CalendarOverridesConfig(BaseModel):
    open_dates: list[str] = Field(default_factory=list)
    closed_dates: list[str] = Field(default_factory=list)

class AppSettings:
    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = Path(config_dir) if config_dir else Path("config")
        
        # Verify and load credentials
        self.shioaji_api_key = os.getenv("SHIOAJI_API_KEY")
        self.shioaji_secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        if not self.shioaji_api_key or not self.shioaji_secret_key:
            raise ValueError("Environment variables SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY must be provided")

        self.trading = self._load_yaml("trading.yaml", TradingConfig)
        self.backtest = self._load_yaml("backtest.yaml", BacktestConfig)
        self.universe = self._load_yaml("universe.yaml", UniverseConfig)
        self.calendar_overrides = self._load_yaml("calendar_overrides.yaml", CalendarOverridesConfig)
        
        self.issuer_allowlist = self._load_json(Path(self.trading.approval.allowlist_path))
        self.revoked_approvals = self._load_json(Path(self.trading.approval.revoked_path))

    def _load_yaml(self, filename: str, model_cls):
        filepath = self.config_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Config file not found: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        try:
            return model_cls(**data)
        except ValidationError as e:
            raise ValueError(f"Validation failed for {filename}: {e}")

    def _load_json(self, relative_path: Path):
        # Allow path relative to config_dir or root
        filepath = self.config_dir.parent / relative_path
        if not filepath.exists():
            filepath = relative_path
        if not filepath.exists():
            raise FileNotFoundError(f"JSON file not found: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_strategy_config(self, strategy_name: str) -> StrategyConfig:
        filepath = self.config_dir / "strategies" / f"{strategy_name}.yaml"
        if not filepath.exists():
            raise FileNotFoundError(f"Strategy config file not found: {filepath}")
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        try:
            return StrategyConfig(**data)
        except ValidationError as e:
            raise ValueError(f"Validation failed for strategy config {strategy_name}: {e}")
