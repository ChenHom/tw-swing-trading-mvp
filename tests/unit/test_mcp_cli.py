from __future__ import annotations

import subprocess

from src.mcp import server
from src.mcp.cli_registry import (
    CliPolicyError,
    build_readonly_argv,
    build_record_fill_argv,
    list_capabilities,
)
from src.mcp.cli_runner import run_argv


def test_capabilities_are_static_p0_list():
    caps = list_capabilities()
    assert "signal list" in caps["readonly"]
    assert caps["p0_mutating"] == ["trade record-fill"]
    assert "trade close-all" in caps["blocked"]
    assert caps["default_record_fill_account"] == "國泰"


def test_readonly_command_builds_argv():
    argv = build_readonly_argv(
        "signal list",
        {"account": "國泰", "date": "2026-06-27"},
    )
    assert argv[-6:] == ["signal", "list", "--date", "2026-06-27", "--account", "國泰"]


def test_unknown_command_is_blocked():
    try:
        build_readonly_argv("trade close-all", {"reason": "test"})
    except CliPolicyError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("expected CliPolicyError")


def test_unknown_args_are_blocked():
    try:
        build_readonly_argv("signal list", {"account": "國泰", "shell": "nope"})
    except CliPolicyError as exc:
        assert "Unknown args" in str(exc)
    else:
        raise AssertionError("expected CliPolicyError")


def test_record_fill_defaults_account_to_cathay():
    argv = build_record_fill_argv(
        {"symbol": "2330", "side": "buy", "quantity": 10, "price": 1000.0}
    )
    assert argv[-12:] == [
        "trade",
        "record-fill",
        "--account",
        "國泰",
        "--symbol",
        "2330",
        "--side",
        "BUY",
        "--quantity",
        "10",
        "--price",
        "1000.0",
    ]


def test_record_fill_adds_optional_fields():
    argv = build_record_fill_argv(
        {
            "symbol": "2330",
            "side": "SELL",
            "quantity": 10,
            "price": 1000.0,
            "account": "simulation-main",
            "strategy_id": "trend_breakout",
            "long_term": True,
            "date": "2026-06-27",
        }
    )
    assert "--account" in argv
    assert argv[argv.index("--account") + 1] == "simulation-main"
    assert "--strategy-id" in argv
    assert argv[argv.index("--strategy-id") + 1] == "trend_breakout"
    assert "--long-term" in argv
    assert "--date" in argv


def test_runner_returns_subprocess_result(monkeypatch):
    def fake_run(argv, cwd, text, capture_output, timeout):
        assert list(argv) == ["python3", "-m", "app", "--help"]
        assert text is True
        assert capture_output is True
        assert timeout == 5
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_argv(["python3", "-m", "app", "--help"], timeout=5)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"


def test_run_cli_adds_pnl_summary_and_data(monkeypatch):
    stdout = """
--- 帳戶 國泰 於 2026-06-27 的策略別損益報告 ---
可用現金：29,123 TWD

================ 策略 MANUAL ================
部位價值：544,570 TWD
已實現損益（淨額）：+31,639 TWD (毛損益: +33,050 TWD, 交易規費: -1,411 TWD)
未實現損益：+27,470 TWD
持倉部位：
  2330 台積電 [長期]: 66 股 @ 均價 1975.23 (現價: 2340.00) - 價值: 154,440 TWD (未實現: +24,074 TWD)

================ 策略 pullback_rebound ================
部位價值：8,120 TWD
已實現損益（淨額）：-60 TWD (毛損益: +0 TWD, 交易規費: -60 TWD)
未實現損益：-220 TWD
持倉部位：
  00994A 主動第一金台股優: 200 股 @ 均價 18.08 (現價: 17.20) - 價值: 3,440 TWD (未實現: -175 TWD)
"""

    monkeypatch.setattr(
        server,
        "run_argv",
        lambda argv: {
            "ok": True,
            "exit_code": 0,
            "argv": argv,
            "stdout": stdout,
            "stderr": "",
        },
    )

    result = server.run_cli("report pnl", {"account": "國泰", "by_strategy": True})

    assert result["summary"] == "國泰 2026-06-27: cash 29123 TWD, 2 positions, unrealized +23899 TWD"
    assert result["data"]["account"] == "國泰"
    assert result["data"]["cash_twd"] == 29123
    assert result["data"]["position_count"] == 2
    assert result["data"]["strategies"][0]["positions"][0]["symbol"] == "2330"
    assert result["data"]["strategies"][0]["positions"][0]["long_term"] is True


def test_run_cli_adds_signal_list_summary_and_data(monkeypatch):
    stdout = """訊號日期   | 執行日期   | 策略             | 來源      | 代號   | 名稱             | 動作 | 參考價格 | 原因                   | 狀態          | 訊號 ID
-----------+------------+------------------+-----------+--------+------------------+------+----------+------------------------+---------------+---------
2026-06-26 | 2026-06-29 | pullback_rebound | RISK_EXIT | 2324   | 仁寶             | SELL | 34.80    | FIXED_STOP_EXIT        | 無持倉 (跳過) | sig-1
2026-06-25 | 2026-06-26 | pullback_rebound | ENTRY     | 00994A | 主動第一金台股優 | BUY  | 18.02    | PULLBACK_REBOUND_ENTRY | 已過期/未成交 | sig-2
"""
    monkeypatch.setattr(
        server,
        "run_argv",
        lambda argv: {
            "ok": True,
            "exit_code": 0,
            "argv": argv,
            "stdout": stdout,
            "stderr": "",
        },
    )

    result = server.run_cli("signal list", {"account": "國泰"})

    assert result["summary"] == "2 signals"
    assert result["data"]["count"] == 2
    assert result["data"]["signals"][0]["symbol"] == "2324"
    assert result["data"]["signals"][1]["action"] == "BUY"


def test_run_cli_adds_reconcile_summary_and_data(monkeypatch):
    stdout = "Reconciling account 國泰...\nReconciliation successful: cash ledger balances match projections.\n"
    monkeypatch.setattr(
        server,
        "run_argv",
        lambda argv: {
            "ok": True,
            "exit_code": 0,
            "argv": argv,
            "stdout": stdout,
            "stderr": "",
        },
    )

    result = server.run_cli("portfolio reconcile", {"account": "國泰"})

    assert result["summary"] == "國泰 reconcile ok"
    assert result["data"] == {"account": "國泰", "reconciled": True}


def test_record_fill_adds_summary(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_argv",
        lambda argv: {
            "ok": True,
            "exit_code": 0,
            "argv": argv,
            "stdout": "recorded\n",
            "stderr": "",
        },
    )

    result = server.record_fill(
        {"symbol": "2330", "side": "buy", "quantity": 10, "price": 1000}
    )

    assert result["summary"] == "record_fill 國泰 BUY 2330 x10 @ 1000"
    assert result["data"]["account"] == "國泰"
