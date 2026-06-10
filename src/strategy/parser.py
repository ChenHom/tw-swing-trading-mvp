import json
from src.contracts.models import DailySignalBundle

class SignalBundleParser:
    @staticmethod
    def parse(json_str: str, expected_params_hash: str) -> DailySignalBundle:
        # Load and validate with Pydantic
        bundle = DailySignalBundle.model_validate_json(json_str)
        
        # Check params_hash mismatch
        if bundle.strategy.params_hash != expected_params_hash:
            raise ValueError(
                f"PARAMS_HASH_MISMATCH: Bundle strategy.params_hash ({bundle.strategy.params_hash}) "
                f"does not match expected hash ({expected_params_hash})"
            )
            
        return bundle
