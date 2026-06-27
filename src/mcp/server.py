from __future__ import annotations

from typing import Any

from src.mcp.cli_registry import (
    CliPolicyError,
    build_readonly_argv,
    build_record_fill_argv,
    list_capabilities,
)
from src.mcp.cli_runner import run_argv
from src.mcp.output_parsers import enrich_result, record_fill_summary


def run_cli(command: str, args: dict[str, Any] | None = None) -> dict:
    """Run an allowlisted read-only/dry-run CLI command."""
    safe_args = args or {}
    try:
        argv = build_readonly_argv(command, safe_args)
    except CliPolicyError as exc:
        return {"ok": False, "error": str(exc), "risk": "blocked"}
    result = run_argv(argv)
    result["risk"] = "readonly"
    return enrich_result(command, safe_args, result)


def record_fill(args: dict[str, Any]) -> dict:
    """Record a manual fill through the existing CLI. Defaults account to 國泰."""
    try:
        argv = build_record_fill_argv(args)
    except CliPolicyError as exc:
        return {"ok": False, "error": str(exc), "risk": "blocked"}
    result = run_argv(argv)
    result["risk"] = "p0_mutating"
    result.update(record_fill_summary(args, argv))
    return result


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional runtime extra
        raise RuntimeError(
            "MCP SDK is not installed. Install requirements-mcp.txt to run this server."
        ) from exc

    mcp = FastMCP("tw-day-trading-cli")
    run_cli_impl = globals()["run_cli"]
    record_fill_impl = globals()["record_fill"]

    @mcp.tool()
    def list_cli_capabilities() -> dict:
        """List P0 CLI commands exposed by this MCP server."""
        return list_capabilities()

    @mcp.tool()
    def run_cli(command: str, args: dict[str, Any] | None = None) -> dict:
        """Run an allowlisted read-only/dry-run tw-day-trading CLI command."""
        return run_cli_impl(command, args)

    @mcp.tool()
    def record_fill(args: dict[str, Any]) -> dict:
        """Record a manual fill. Defaults account to 國泰 when omitted."""
        return record_fill_impl(args)

    return mcp


def main() -> None:
    create_mcp_server().run()


if __name__ == "__main__":
    main()
