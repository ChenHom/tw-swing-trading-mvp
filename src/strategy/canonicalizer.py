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
