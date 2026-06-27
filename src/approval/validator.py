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
        # backtest replay 歷史：current_time 是被重播的歷史 session 日，manifest 的「核准生效窗」
        # 是 live 營運管制（這策略今天可不可下單），不該 gate 研究用的歷史回放——否則整段歷史
        # 都被「核准尚未生效」擋掉（R-T3 實測 863 筆 APPROVAL_INVALID）。digest/issuer/revocation/
        # mode 檢查仍保留，確保 manifest 為真且允許 backtest。
        if execution_mode != "backtest":
            valid_from = self._parse_manifest_time(manifest.validity.valid_from, current_time)
            expires_at = self._parse_manifest_time(manifest.validity.expires_at, current_time)

            if current_time.tzinfo is None and valid_from.tzinfo is not None:
                raise ValueError("Timezone mismatch: current_time is naive but manifest times are localized")

            if current_time < valid_from:
                raise ValueError(f"MANIFEST_NOT_YET_VALID: manifest valid from {manifest.validity.valid_from}, current time is {current_time.isoformat()}")

            if current_time >= expires_at:
                raise ValueError(f"MANIFEST_EXPIRED: manifest expired at {manifest.validity.expires_at}, current time is {current_time.isoformat()}")

        # 5. Execution mode permission check
        if execution_mode not in manifest.permissions.execution_modes:
            raise ValueError(f"EXECUTION_MODE_NOT_ALLOWED: mode {execution_mode} is not allowed by manifest")

    @staticmethod
    def _parse_manifest_time(value: str, current_time: datetime) -> datetime:
        parsed = datetime.fromisoformat(value)
        if current_time.tzinfo is not None and parsed.tzinfo is None:
            return parsed.replace(tzinfo=current_time.tzinfo)
        return parsed
