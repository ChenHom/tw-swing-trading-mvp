import pytest
import os
from pathlib import Path
from src.config import AppSettings

def test_settings_load_failure_without_env(monkeypatch):
    monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
    monkeypatch.delenv("SHIOAJI_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY"):
        AppSettings()

def test_settings_load_success(monkeypatch):
    monkeypatch.setenv("SHIOAJI_API_KEY", "test-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "test-secret")
    
    # Load using the default config dir in the workspace
    settings = AppSettings()
    assert settings.shioaji_api_key == "test-key"
    assert settings.shioaji_secret_key == "test-secret"
    assert settings.trading.database_path == "data/app.db"
    assert settings.backtest.initial_cash_twd == 300000
    assert len(settings.universe.symbols) > 0
    assert "manual-research-review" in settings.issuer_allowlist
