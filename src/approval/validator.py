import hashlib
import json
from datetime import datetime
from src.contracts.models import StrategyApprovalManifest

class ManifestValidator:
    def __init__(self, allowed_issuers: list[str], revoked_approval_ids: list[str]):
        self.allowed_issuers = set(allowed_issuers)
        self.revoked_approval_ids = set(revoked_approval_ids)

    def validate(self, manifest: StrategyApprovalManifest, current_time: datetime, execution_mode: str) -> None:
        # 1. Digest/Integrity verification
        manifest_dict = json.loads(manifest.model_dump_json())
        # Clear digest to calculate hash
        manifest_dict["integrity"]["digest"] = ""
        
        canonical_str = json.dumps(
            manifest_dict,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":")
        )
        calculated_digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        expected_digest = f"sha256:{calculated_digest}"
        
        if manifest.integrity.digest != expected_digest:
            raise ValueError(f"INTEGRITY_INVALID: digest mismatch. Expected {expected_digest}, got {manifest.integrity.digest}")
            
        # 2. Issuer check
        if manifest.issuer_id not in self.allowed_issuers:
            raise ValueError(f"ISSUER_NOT_TRUSTED: issuer {manifest.issuer_id} is not in the allowed list")
            
        # 3. Revocation check
        if manifest.approval_id in self.revoked_approval_ids:
            raise ValueError(f"MANIFEST_REVOKED: approval {manifest.approval_id} is revoked")
            
        # 4. Validity time range check
        valid_from = datetime.fromisoformat(manifest.validity.valid_from)
        expires_at = datetime.fromisoformat(manifest.validity.expires_at)
        
        if current_time.tzinfo is None and valid_from.tzinfo is not None:
            raise ValueError("Timezone mismatch: current_time is naive but manifest times are localized")
            
        if current_time < valid_from:
            raise ValueError(f"MANIFEST_NOT_YET_VALID: manifest valid from {manifest.validity.valid_from}, current time is {current_time.isoformat()}")
            
        if current_time >= expires_at:
            raise ValueError(f"MANIFEST_EXPIRED: manifest expired at {manifest.validity.expires_at}, current time is {current_time.isoformat()}")
            
        # 5. Execution mode permission check
        if execution_mode not in manifest.permissions.execution_modes:
            raise ValueError(f"EXECUTION_MODE_NOT_ALLOWED: mode {execution_mode} is not allowed by manifest")
