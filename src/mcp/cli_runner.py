from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT_SECONDS = 60


def run_argv(argv: Sequence[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run a project CLI argv list and return a small MCP-friendly result."""
    result = subprocess.run(
        list(argv),
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "argv": list(argv),
        "cwd": str(PROJECT_ROOT),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def python_argv() -> list[str]:
    python_bin = os.environ.get("TW_DAY_TRADING_PYTHON", "python3")
    return [python_bin, "-m", "app"]

