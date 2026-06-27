# tw-day-trading MCP CLI

This MCP server is a small wrapper around the existing project CLI. It does not
import CLI handlers, rewrite trading logic, or expose arbitrary shell commands.

## Shape

```text
MCP tool call
  -> static allowlist
  -> argv builder
  -> subprocess.run(argv, cwd=project root)
  -> stdout / stderr / exit_code / argv
  -> optional summary / parsed data for common outputs
```

## Phase Status

- P0 complete: project-local MCP server and safe CLI wrapper.
- P1 complete: OpenClaw-managed MCP server `tw-day-trading-cli` is configured.
- P2 complete: common outputs include lightweight `summary` / `data`.
- P3 backlog only: low-risk write tools, not implemented.
- P4 backlog only: long-running operator tools, not implemented.

OpenClaw MCP config:

```text
name: tw-day-trading-cli
command: /home/hom/services/stock/tw-day-trading/.venv/bin/python
args: -m src.mcp.server
cwd: /home/hom/services/stock/tw-day-trading
tools: list_cli_capabilities, run_cli, record_fill
```

## Tools

### `run_cli(command, args)`

Runs allowlisted read-only or dry-run CLI commands.

Allowed commands:

- `strategy inspect`
- `approval list`
- `approval status`
- `approval validate`
- `market validate`
- `signal list`
- `trade plan`
- `trade exit-check`
- `portfolio reconcile`
- `report pnl`
- `report daily`
- `corporate-action list`
- `corporate-action check`

P2 parsed outputs:

- `report pnl` with `by_strategy: true`: account, date, cash, strategy totals,
  positions, long-term marker, unrealized PnL.
- `signal list`: count and parsed signal rows.
- `portfolio reconcile`: account and reconciliation status.

All commands still return raw `stdout`, `stderr`, `exit_code`, and `argv`.

### `record_fill(args)`

Runs `python3 -m app trade record-fill`.

Required args:

- `symbol`
- `side`: `BUY` or `SELL`
- `quantity`
- `price`

Optional args:

- `account`, default `國泰`
- `strategy_id`
- `long_term`
- `date`

Example:

```json
{
  "symbol": "2330",
  "side": "BUY",
  "quantity": 10,
  "price": 1000.0,
  "strategy_id": "trend_breakout",
  "date": "2026-06-27"
}
```

Builds:

```bash
python3 -m app trade record-fill \
  --account 國泰 \
  --symbol 2330 \
  --side BUY \
  --quantity 10 \
  --price 1000.0 \
  --strategy-id trend_breakout \
  --date 2026-06-27
```

P2 returns a short summary and normalized fill args in `data`, alongside raw
CLI output.

## Not In Current Scope

- Generic mutation confirmation tokens
- Dynamic argparse introspection
- Policy engine files
- Operator-only commands
- Arbitrary shell command execution

Blocked examples:

- `trade close-all`
- `simulation execute-pending`
- `simulation reset`
- `account adjust-cash`

## Backlog To Remember

### P3: low-risk write tools

Do not implement until explicitly requested.

- `reject_signal`
- `un_reject_signal`
- `set_long_term`
- Possible corporate-action record/apply tools

When P3 starts, prefer dedicated tools per operation over a generic mutation
runner. Revisit confirmation only if the operation is hard to reverse.

### P4: long-running operator tools

Do not implement until explicitly requested.

- `simulation run-daily`
- `market sync`
- `market backfill`
- `backtest run`
- `market build-universe`

P4 needs timeout policy, log paths, duplicate-run protection, and clear operator
intent. Keep it separate from P0/P2 query tools.
