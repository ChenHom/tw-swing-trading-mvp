import pytest
import json
from datetime import date
from pydantic import ValidationError
from src.contracts.models import DailySignalBundle
from src.strategy.parser import SignalBundleParser

@pytest.fixture
def valid_bundle_dict():
    return {
        "schema_version": "1.0",
        "bundle_id": "bundle-20260610",
        "run_id": "daily:2026-06-10",
        "approval_id": "approval-trend-pullback-v1",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": "sha256:d8a7c2b3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1"
        },
        "signal_date": "2026-06-10",
        "target_execution_date": "2026-06-11",
        "market_data_cutoff": "2026-06-10",
        "signals": [
            {
                "signal_id": "bundle-20260610:2330:buy",
                "symbol": "2330",
                "action": "BUY",
                "reference_price": 1020.0,
                "reason_code": "TREND_PULLBACK_ENTRY"
            }
        ]
    }

def test_signal_bundle_parser_valid(valid_bundle_dict):
    json_str = json.dumps(valid_bundle_dict)
    
    # Target params_hash matches the bundle's strategy.params_hash
    target_hash = "sha256:d8a7c2b3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1"
    
    bundle = SignalBundleParser.parse(json_str, target_hash)
    assert bundle.bundle_id == "bundle-20260610"
    assert len(bundle.signals) == 1
    assert bundle.signals[0].symbol == "2330"
    assert bundle.signals[0].action == "BUY"

def test_signal_bundle_parser_hash_mismatch(valid_bundle_dict):
    json_str = json.dumps(valid_bundle_dict)
    
    # Target hash does not match
    target_hash = "sha256:differenthash"
    
    with pytest.raises(ValueError, match="PARAMS_HASH_MISMATCH"):
        SignalBundleParser.parse(json_str, target_hash)

def test_signal_bundle_parser_schema_invalid(valid_bundle_dict):
    # Remove a required field
    del valid_bundle_dict["bundle_id"]
    json_str = json.dumps(valid_bundle_dict)
    
    with pytest.raises(ValidationError):
        # Pydantic validation error or JSON schema error
        SignalBundleParser.parse(json_str, "sha256:d8a7c2b3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1")
