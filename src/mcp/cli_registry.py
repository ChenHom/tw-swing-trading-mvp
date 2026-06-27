from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.mcp.cli_runner import python_argv


DEFAULT_RECORD_FILL_ACCOUNT = "國泰"


class CliPolicyError(ValueError):
    pass


# ponytail: static P0 allowlist; add commands here only after they are explicitly needed.
READONLY_COMMANDS: dict[str, dict[str, Any]] = {
    "strategy inspect": {
        "parts": ["strategy", "inspect"],
        "positionals": [("config_path", True)],
        "flags": {},
    },
    "approval list": {"parts": ["approval", "list"], "positionals": [], "flags": {}},
    "approval status": {"parts": ["approval", "status"], "positionals": [], "flags": {}},
    "approval validate": {
        "parts": ["approval", "validate"],
        "positionals": [("manifest_path", True)],
        "flags": {},
    },
    "market validate": {
        "parts": ["market", "validate"],
        "positionals": [],
        "flags": {"last_sessions": "--last-sessions"},
    },
    "signal list": {
        "parts": ["signal", "list"],
        "positionals": [],
        "flags": {"date": "--date", "account": "--account"},
    },
    "trade plan": {
        "parts": ["trade", "plan"],
        "positionals": [],
        "flags": {"bundle": "--bundle", "account": "--account"},
        "required_flags": ["bundle"],
    },
    "trade exit-check": {
        "parts": ["trade", "exit-check"],
        "positionals": [],
        "flags": {
            "symbol": "--symbol",
            "strategy": "--strategy",
            "account": "--account",
            "as_of": "--as-of",
        },
        "required_flags": ["symbol", "strategy"],
    },
    "portfolio reconcile": {
        "parts": ["portfolio", "reconcile"],
        "positionals": [],
        "flags": {"account": "--account"},
    },
    "report pnl": {
        "parts": ["report", "pnl"],
        "positionals": [],
        "flags": {
            "account": "--account",
            "date": "--date",
            "source": "--source",
            "by_strategy": "--by-strategy",
        },
    },
    "report daily": {
        "parts": ["report", "daily"],
        "positionals": [],
        "flags": {"account": "--account", "date": "--date", "dir": "--dir"},
    },
    "corporate-action list": {
        "parts": ["corporate-action", "list"],
        "positionals": [],
        "flags": {},
    },
    "corporate-action check": {
        "parts": ["corporate-action", "check"],
        "positionals": [],
        "flags": {"account": "--account"},
    },
}


def list_capabilities() -> dict:
    return {
        "readonly": sorted(READONLY_COMMANDS),
        "p0_mutating": ["trade record-fill"],
        "blocked": [
            "account adjust-cash",
            "simulation execute-pending",
            "simulation reset",
            "trade close-all",
        ],
        "default_record_fill_account": DEFAULT_RECORD_FILL_ACCOUNT,
    }


def build_readonly_argv(command: str, args: Mapping[str, Any] | None = None) -> list[str]:
    spec = READONLY_COMMANDS.get(command)
    if spec is None:
        raise CliPolicyError(f"CLI command is not allowed in P0: {command}")
    return _build_argv(spec, args or {})


def build_record_fill_argv(args: Mapping[str, Any]) -> list[str]:
    required = ["symbol", "side", "quantity", "price"]
    missing = [name for name in required if args.get(name) in (None, "")]
    if missing:
        raise CliPolicyError(f"Missing required record_fill args: {', '.join(missing)}")

    side = str(args["side"]).upper()
    if side not in {"BUY", "SELL"}:
        raise CliPolicyError("record_fill side must be BUY or SELL")

    argv = python_argv() + [
        "trade",
        "record-fill",
        "--account",
        str(args.get("account") or DEFAULT_RECORD_FILL_ACCOUNT),
        "--symbol",
        str(args["symbol"]),
        "--side",
        side,
        "--quantity",
        str(args["quantity"]),
        "--price",
        str(args["price"]),
    ]
    if args.get("strategy_id"):
        argv += ["--strategy-id", str(args["strategy_id"])]
    if args.get("long_term"):
        argv.append("--long-term")
    if args.get("date"):
        argv += ["--date", str(args["date"])]
    return argv


def _build_argv(spec: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    allowed = set(spec.get("flags", {})) | {name for name, _required in spec.get("positionals", [])}
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise CliPolicyError(f"Unknown args for command: {', '.join(unknown)}")

    missing_positionals = [
        name
        for name, required in spec.get("positionals", [])
        if required and args.get(name) in (None, "")
    ]
    missing_flags = [
        name for name in spec.get("required_flags", []) if args.get(name) in (None, "")
    ]
    missing = missing_positionals + missing_flags
    if missing:
        raise CliPolicyError(f"Missing required args: {', '.join(missing)}")

    argv = python_argv() + list(spec["parts"])
    for name, _required in spec.get("positionals", []):
        value = args.get(name)
        if value not in (None, ""):
            argv.append(str(value))

    for name, flag in spec.get("flags", {}).items():
        value = args.get(name)
        if value in (None, "", False):
            continue
        if isinstance(value, bool):
            argv.append(flag)
        else:
            argv += [flag, str(value)]
    return argv

