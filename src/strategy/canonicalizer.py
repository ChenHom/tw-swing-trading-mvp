import hashlib
import json
from pydantic import BaseModel

class StrategyParameterCanonicalizer:
    @staticmethod
    def canonicalize(params: BaseModel) -> str:
        # Dump model to dict including all defaults
        dumped = params.model_dump()
        return json.dumps(
            dumped,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":")
        )

    @classmethod
    def compute_hash(cls, params: BaseModel) -> str:
        canonical_str = cls.canonicalize(params)
        digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @classmethod
    def compute_strategy_hash(cls, params: BaseModel, exit_params: BaseModel | None) -> str:
        """
        Canonical hash over the full strategy parameter surface. The exit block
        is included so changing exit parameters requires re-approval (§2.10).
        """
        envelope = {
            "parameters": params.model_dump(),
            "exit": exit_params.model_dump() if exit_params is not None else None,
        }
        canonical_str = json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":")
        )
        digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
