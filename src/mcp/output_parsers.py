from __future__ import annotations

import re
from typing import Any


def enrich_result(command: str, args: dict[str, Any], result: dict) -> dict:
    if not result.get("ok"):
        return result

    stdout = result.get("stdout") or ""
    if command == "report pnl" and args.get("by_strategy"):
        data = parse_pnl_by_strategy(stdout)
        if data:
            result["data"] = data
            result["summary"] = (
                f"{data['account']} {data['date']}: cash {data['cash_twd']} TWD, "
                f"{data['position_count']} positions, unrealized "
                f"{_signed(data['total_unrealized_twd'])} TWD"
            )
    elif command == "signal list":
        data = parse_signal_list(stdout)
        result["data"] = data
        result["summary"] = f"{data['count']} signals"
    elif command == "portfolio reconcile":
        account = str(args.get("account") or "")
        reconciled = "Reconciliation successful" in stdout
        result["data"] = {"account": account, "reconciled": reconciled}
        result["summary"] = f"{account} reconcile {'ok' if reconciled else 'failed'}"
    return result


def record_fill_summary(args: dict[str, Any], argv: list[str]) -> dict:
    data = {
        "account": _argv_value(argv, "--account"),
        "symbol": _argv_value(argv, "--symbol"),
        "side": _argv_value(argv, "--side"),
        "quantity": _argv_value(argv, "--quantity"),
        "price": _argv_value(argv, "--price"),
        "strategy_id": _argv_value(argv, "--strategy-id"),
        "long_term": "--long-term" in argv,
        "date": _argv_value(argv, "--date"),
    }
    return {
        "data": data,
        "summary": (
            f"record_fill {data['account']} {data['side']} {data['symbol']} "
            f"x{data['quantity']} @ {data['price']}"
        ),
    }


def parse_pnl_by_strategy(stdout: str) -> dict[str, Any] | None:
    account_match = re.search(r"--- 帳戶 (.+?) 於 ([0-9-]+) 的策略別損益報告 ---", stdout)
    cash_match = re.search(r"可用現金：([+-]?[0-9,]+) TWD", stdout)
    if not account_match or not cash_match:
        return None

    data: dict[str, Any] = {
        "account": account_match.group(1),
        "date": account_match.group(2),
        "cash_twd": _int(cash_match.group(1)),
        "position_count": 0,
        "total_unrealized_twd": 0,
        "strategies": [],
    }
    current: dict[str, Any] | None = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        strategy_match = re.match(r"=+ 策略 (.+?) =+", line)
        if strategy_match:
            current = {
                "strategy": strategy_match.group(1),
                "position_value_twd": 0,
                "realized_pnl_twd": 0,
                "unrealized_pnl_twd": 0,
                "positions": [],
            }
            data["strategies"].append(current)
            continue
        if current is None:
            continue
        if line.startswith("部位價值："):
            current["position_value_twd"] = _int(line.removeprefix("部位價值：").split()[0])
        elif line.startswith("已實現損益（淨額）："):
            current["realized_pnl_twd"] = _int(line.split("：", 1)[1].split("TWD", 1)[0])
        elif line.startswith("未實現損益："):
            current["unrealized_pnl_twd"] = _int(line.removeprefix("未實現損益：").split()[0])
        else:
            position = _parse_position(line)
            if position:
                current["positions"].append(position)
                data["position_count"] += 1
                data["total_unrealized_twd"] += position["unrealized_twd"]
    return data


def parse_signal_list(stdout: str) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    lines = [line for line in stdout.splitlines() if "|" in line]
    if len(lines) < 2:
        return {"count": 0, "signals": []}

    headers = [_normalize_header(part) for part in lines[0].split("|")]
    for line in lines[1:]:
        if set(line.replace("+", "").replace("-", "").strip()) == set():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != len(headers):
            continue
        rows.append(dict(zip(headers, parts, strict=False)))
    return {"count": len(rows), "signals": rows}


def _parse_position(line: str) -> dict[str, Any] | None:
    match = re.match(
        r"(?P<symbol>\S+) (?P<name>.+?): (?P<quantity>[0-9,]+) 股 @ 均價 "
        r"(?P<avg_price>[0-9.]+) \(現價: (?P<market_price>[0-9.]+)\) - 價值: "
        r"(?P<market_value>[0-9,]+) TWD \(未實現: (?P<unrealized>[+-]?[0-9,]+) TWD\)",
        line,
    )
    if not match:
        return None
    name = match.group("name")
    long_term = name.endswith(" [長期]")
    if long_term:
        name = name.removesuffix(" [長期]")
    return {
        "symbol": match.group("symbol"),
        "name": name,
        "long_term": long_term,
        "quantity": _int(match.group("quantity")),
        "avg_price": float(match.group("avg_price")),
        "market_price": float(match.group("market_price")),
        "market_value_twd": _int(match.group("market_value")),
        "unrealized_twd": _int(match.group("unrealized")),
    }


def _normalize_header(value: str) -> str:
    return {
        "訊號日期": "signal_date",
        "執行日期": "execution_date",
        "策略": "strategy",
        "來源": "source",
        "代號": "symbol",
        "名稱": "name",
        "動作": "action",
        "參考價格": "reference_price",
        "原因": "reason",
        "狀態": "status",
        "訊號 ID": "signal_id",
    }.get(value.strip(), value.strip())


def _int(value: str) -> int:
    return int(value.replace(",", "").strip())


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _argv_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    index = argv.index(flag)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]
