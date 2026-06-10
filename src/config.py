import os
import json
import yaml
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, ValidationError
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

class StrategyParamConfig(BaseModel):
    ma_short: int
    ma_long: int
    stop_loss_bps: int
    take_profit_bps: int
    order_budget_twd: int

class StrategyConfig(BaseModel):
    strategy_id: str
    strategy_version: str
    parameters: StrategyParamConfig

class BacktestConfig(BaseModel):
    initial_cash_twd: int
    slippage_bps: int
    fee_model_version: str

class ApprovalConfig(BaseModel):
    expiry_warning_sessions: int = 3
    allowlist_path: str = "config/issuer-allowlist.json"
    revoked_path: str = "config/revoked-approvals.json"
    active_pointer_path: str = "artifacts/approvals/active-approval.json"
    approvals_dir: str = "artifacts/approvals/"

class TradingConfig(BaseModel):
    database_path: str = "data/app.db"
    approval: ApprovalConfig

class SymbolConfig(BaseModel):
    code: str
    exchange: str
    instrument_type: str

class UniverseConfig(BaseModel):
    symbols: list[SymbolConfig]

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
