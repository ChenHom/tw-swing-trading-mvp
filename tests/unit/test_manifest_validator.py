import pytest
from datetime import datetime, date
import json
import hashlib
from src.contracts.models import (
    StrategyApprovalManifest, StrategyInfo, PermissionsInfo, LimitsInfo, ValidityInfo, IntegrityInfo, TrendPullbackParams
)
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.approval.validator import ManifestValidator

@pytest.fixture
def sample_params_hash():
    params = TrendPullbackParams()
    return StrategyParameterCanonicalizer.compute_hash(params)

@pytest.fixture
def valid_manifest_dict(sample_params_hash):
    # Base dictionary before digest calculation
    manifest = {
        "schema_version": "1.0",
        "approval_id": "approval-trend-pullback-v1",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": sample_params_hash
        },
        "permissions": {
            "execution_modes": ["simulation", "backtest"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": 20000,
            "max_daily_buy_value": 40000,
            "max_open_positions": 3
        },
        "validity": {
            "valid_from": "2026-06-10T00:00:00+08:00",
            "expires_at": "2026-07-10T00:00:00+08:00"
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": ""
        }
    }
    
    # Calculate canonical digest
    # Remove digest
    manifest_copy = json.loads(json.dumps(manifest))
    manifest_copy["integrity"]["digest"] = ""
    canonical_str = json.dumps(
        manifest_copy,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    manifest["integrity"]["digest"] = f"sha256:{digest}"
    return manifest

def test_manifest_validator_valid(valid_manifest_dict):
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    allowlist = ["manual-research-review"]
    revoked = []
    
    # Validation at valid date
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    
    validator = ManifestValidator(allowlist, revoked)
    # This should not raise any exception
    validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_accepts_date_only_expiry(valid_manifest_dict):
    valid_manifest_dict["validity"]["expires_at"] = "2026-12-31"
    valid_manifest_dict["integrity"]["digest"] = ""
    canonical_str = json.dumps(
        valid_manifest_dict,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    valid_manifest_dict["integrity"]["digest"] = f"sha256:{digest}"

    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    validator = ManifestValidator(["manual-research-review"], [])

    validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_invalid_digest(valid_manifest_dict):
    valid_manifest_dict["integrity"]["digest"] = "sha256:wrongdigest"
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    validator = ManifestValidator(["manual-research-review"], [])
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    
    with pytest.raises(ValueError, match="INTEGRITY_INVALID"):
        validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_expired(valid_manifest_dict):
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    validator = ManifestValidator(["manual-research-review"], [])
    # Time is after expires_at
    current_time = datetime.fromisoformat("2026-07-15T10:00:00+08:00")
    
    with pytest.raises(ValueError, match="MANIFEST_EXPIRED"):
        validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_untrusted_issuer(valid_manifest_dict):
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    # Issuer not in allowlist
    validator = ManifestValidator(["other-issuer"], [])
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    
    with pytest.raises(ValueError, match="ISSUER_NOT_TRUSTED"):
        validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_revoked(valid_manifest_dict):
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    # approval_id is revoked
    validator = ManifestValidator(["manual-research-review"], ["approval-trend-pullback-v1"])
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    
    with pytest.raises(ValueError, match="MANIFEST_REVOKED"):
        validator.validate(manifest, current_time, "simulation")

def test_manifest_validator_mode_not_allowed(valid_manifest_dict):
    valid_manifest_dict["permissions"]["execution_modes"] = ["backtest"]
    # Recalculate digest for modified manifest
    valid_manifest_dict["integrity"]["digest"] = ""
    canonical_str = json.dumps(
        valid_manifest_dict,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    valid_manifest_dict["integrity"]["digest"] = f"sha256:{digest}"
    
    manifest = StrategyApprovalManifest(**valid_manifest_dict)
    validator = ManifestValidator(["manual-research-review"], [])
    current_time = datetime.fromisoformat("2026-06-15T10:00:00+08:00")
    
    with pytest.raises(ValueError, match="EXECUTION_MODE_NOT_ALLOWED"):
        # requesting simulation but only backtest is allowed
        validator.validate(manifest, current_time, "simulation")
