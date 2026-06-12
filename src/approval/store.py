"""多策略授權儲存：active-approvals.json 以 strategy_id 為鍵的 active map。

同一策略同時只能有一份有效授權；不同策略各自獨立啟用/停用。
向下相容：map 檔不存在時回讀舊單一 pointer（active-approval.json）。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.contracts.models import StrategyApprovalManifest


def _load_manifest_file(approvals_dir: Path, approval_id: str) -> Optional[StrategyApprovalManifest]:
    manifest_path = approvals_dir / f"{approval_id}.json"
    if not manifest_path.exists():
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        return StrategyApprovalManifest(**json.load(f))


def load_active_manifests(settings) -> dict[str, StrategyApprovalManifest]:
    """strategy_id -> active manifest."""
    approvals_dir = Path(settings.trading.approval.approvals_dir)
    map_path = Path(settings.trading.approval.active_map_path)

    result: dict[str, StrategyApprovalManifest] = {}
    if map_path.exists():
        with open(map_path, "r", encoding="utf-8") as f:
            active_map = json.load(f)
        for strategy_id, entry in active_map.items():
            approval_id = entry.get("approval_id") if isinstance(entry, dict) else entry
            if not approval_id:
                continue
            manifest = _load_manifest_file(approvals_dir, approval_id)
            if manifest:
                result[strategy_id] = manifest
        return result

    # Legacy fallback: single active-approval.json pointer
    legacy_path = Path(settings.trading.approval.active_pointer_path)
    if legacy_path.exists():
        with open(legacy_path, "r", encoding="utf-8") as f:
            pointer = json.load(f)
        approval_id = pointer.get("approval_id")
        if approval_id:
            manifest = _load_manifest_file(approvals_dir, approval_id)
            if manifest:
                result[manifest.strategy.strategy_id] = manifest
    return result


def activate_manifest(settings, manifest: StrategyApprovalManifest, manifest_dict: dict) -> None:
    """Store the manifest file and point the strategy's active entry at it."""
    approvals_dir = Path(settings.trading.approval.approvals_dir)
    approvals_dir.mkdir(parents=True, exist_ok=True)
    stored_path = approvals_dir / f"{manifest.approval_id}.json"
    with open(stored_path, "w", encoding="utf-8") as f:
        json.dump(manifest_dict, f, indent=2, ensure_ascii=False)

    map_path = Path(settings.trading.approval.active_map_path)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    active_map = {}
    if map_path.exists():
        with open(map_path, "r", encoding="utf-8") as f:
            active_map = json.load(f)
    active_map[manifest.strategy.strategy_id] = {
        "approval_id": manifest.approval_id,
        "activated_at": datetime.now(timezone.utc).astimezone().isoformat()
    }
    _atomic_write_json(map_path, active_map)


def deactivate_strategy(settings, strategy_id: str) -> bool:
    """Remove the strategy's active approval. Returns True if one was removed."""
    map_path = Path(settings.trading.approval.active_map_path)
    if not map_path.exists():
        return False
    with open(map_path, "r", encoding="utf-8") as f:
        active_map = json.load(f)
    if strategy_id not in active_map:
        return False
    del active_map[strategy_id]
    _atomic_write_json(map_path, active_map)
    return True


def _atomic_write_json(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)
