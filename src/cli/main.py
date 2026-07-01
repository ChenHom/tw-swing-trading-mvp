import argparse
import sys
import os
import fcntl
import json
import hashlib
import sqlite3
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.config import AppSettings
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection, MANUAL_STRATEGY_ID
from src.portfolio.ledger import PortfolioLedger
from src.contracts.models import (
    TrendPullbackParams, StrategyApprovalManifest, StrategyInfo, LimitsInfo,
    ValidityInfo, IntegrityInfo, PermissionsInfo, DailySignalBundle, SignalItem, ExecutionContext
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.market_data.provider import ShioajiMarketDataProvider
from src.application.runners.backtest import BacktestRunner
from src.application.runners.simulation import DailySimulationRunner, EntryStrategySpec
from src.approval.validator import ManifestValidator
from src.approval.store import load_active_manifests, activate_manifest, deactivate_strategy
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.strategy import registry as strategy_registry
from src.strategy.base import SignalGenerationContext, PortfolioSnapshot, PositionSnapshot
from src.trading.planner import OrderPlanner, PortfolioState
from src.trading.allocator import GlobalLimits
from src.broker.fake_broker import FakeBroker
from src.application.execution.engine import TradeExecutionEngine
from src.application.services import trade_write
from src.cli import common
from src.cli.market import cmd_market_backfill, cmd_market_backfill_history, cmd_market_sync, cmd_market_sync_chips, cmd_market_sync_names, cmd_market_validate, cmd_market_build_universe
from src.cli.strategy import cmd_strategy_inspect
from src.cli.approval import cmd_approval_create, cmd_approval_validate, cmd_approval_activate, cmd_approval_deactivate, cmd_approval_list, cmd_approval_status
from src.cli.account import cmd_account_init, cmd_account_adjust_cash, cmd_account_adjust
from src.cli.backtest import cmd_backtest_run
from src.cli.simulation import cmd_simulation_run_daily, cmd_simulation_reset, cmd_simulation_execute_pending
from src.cli.signal import cmd_signal_generate, cmd_signal_list
from src.cli.trade import cmd_trade_plan, cmd_trade_reject_signal, cmd_trade_un_reject_signal, cmd_trade_record_fill, cmd_trade_close_all, cmd_trade_exit_check, cmd_trade_set_long_term, cmd_trade_backfill_names
from src.cli.portfolio import cmd_portfolio_reconcile, cmd_portfolio_rebuild_projections
from src.cli.report import cmd_report_pnl, cmd_report_daily
from src.cli.corporate_action import cmd_corporate_action_record, cmd_corporate_action_apply, cmd_corporate_action_list, cmd_corporate_action_check


def main():
    parser = argparse.ArgumentParser(description="台股波段量化交易系統 MVP 命令列介面 (CLI)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # 1. market group
    parser_market = subparsers.add_parser("market", help="市場行情數據管理")
    market_subs = parser_market.add_subparsers(dest="subcommand", required=True)
    
    parser_backfill = market_subs.add_parser("backfill", help="回補歷史日 K 線行情")
    parser_backfill.add_argument("--calendar-days", type=int, default=100, help="往回追溯的日曆天數")
    
    parser_sync = market_subs.add_parser("sync", help="同步特定日期的 K 線行情")
    parser_sync.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")

    parser_sync_chips = market_subs.add_parser("sync-chips", help="同步籌碼（三大法人/融資券）供 LLM 顧問")
    parser_sync_chips.add_argument("--days", type=int, default=90, help="往回追溯日曆天數（預設 90）")
    parser_sync_chips.add_argument("--symbols", type=str, default=None, help="逗號分隔代碼，預設用 universe.yaml")
    parser_sync_chips.add_argument("--date", type=str, default=None, help="結束日期 YYYY-MM-DD（預設今天）")
    
    parser_sync_names = market_subs.add_parser("sync-names", help="全市場代碼→中文名補齊（FinMind，含 ETF；修復顯示空白）")

    parser_validate = market_subs.add_parser("validate", help="驗證資料庫中的日 K 線行情")
    parser_validate.add_argument("--last-sessions", type=int, default=60, help="驗證最近幾筆交易日的行情數據")

    parser_backfill_history = market_subs.add_parser(
        "backfill-history", help="research.db 深歷史回補（雙價/CA 帳本，含 2022）"
    )
    parser_backfill_history.add_argument("--from", dest="start_date", required=True, help="起始日期 YYYY-MM-DD")
    parser_backfill_history.add_argument("--to", dest="end_date", required=True, help="結束日期 YYYY-MM-DD")
    parser_backfill_history.add_argument("--symbols", type=str, default=None, help="逗號分隔標的代碼，預設用 universe.yaml")
    parser_backfill_history.add_argument("--roster", type=str, choices=["twse"], default=None,
                                         help="改用 FinMind 全市場 roster（twse 全上市，含部分下市股）回補；raw-only、跳股利")
    parser_backfill_history.add_argument("--db", type=str, default="data/research.db", help="research SQLite 路徑")
    parser_backfill_history.add_argument(
        "--source", type=str, choices=["finmind", "twse", "auto"], default="auto", help="資料來源"
    )

    parser_build_universe = market_subs.add_parser(
        "build-universe", help="建 PIT 流動性 top-N universe_policy（月再平衡、成交額排序）"
    )
    parser_build_universe.add_argument("--policy-version", dest="policy_version", type=str, required=True,
                                       help="policy_version（不可含 'diagnostic'），如 liquidity-top150-v1")
    parser_build_universe.add_argument("--from", dest="start_date", type=str, required=True, help="起始日期 YYYY-MM-DD")
    parser_build_universe.add_argument("--to", dest="end_date", type=str, required=True, help="結束日期 YYYY-MM-DD")
    parser_build_universe.add_argument("--top-n", dest="top_n", type=int, default=150, help="每再平衡日取流動性前 N 名")
    parser_build_universe.add_argument("--lookback", type=int, default=20, help="流動性視窗交易日數")
    parser_build_universe.add_argument("--db", type=str, default="data/research.db", help="research SQLite 路徑")

    # 2. strategy group
    parser_strategy = subparsers.add_parser("strategy", help="策略輔助指令")
    strategy_subs = parser_strategy.add_subparsers(dest="subcommand", required=True)
    
    parser_inspect = strategy_subs.add_parser("inspect", help="檢查策略設定並輸出參數 SHA-256 指紋")
    parser_inspect.add_argument("config_path", type=str, help="策略 YAML 設定檔路徑")
    
    # 3. approval group
    parser_approval = subparsers.add_parser("approval", help="策略授權清單 (Manifest) 管理")
    approval_subs = parser_approval.add_subparsers(dest="subcommand", required=True)
    
    parser_app_create = approval_subs.add_parser("create", help="建立已簽署的策略授權清單 JSON 檔")
    parser_app_create.add_argument("--strategy", type=str, required=True, help="策略 YAML 設定檔")
    parser_app_create.add_argument("--expires-at", type=str, required=True, help="授權到期時間 (ISO 格式字串)")
    parser_app_create.add_argument("--output", type=str, required=True, help="輸出的授權清單 JSON 檔案路徑")
    parser_app_create.add_argument("--issuer", type=str, default="manual-research-review", help="發行者識別 ID")
    parser_app_create.add_argument("--max-order-value", type=int, default=35000, help="單筆委託最大金額 (TWD)")
    parser_app_create.add_argument("--max-daily-buy-value", type=int, default=150000, help="單日累計買入最大金額 (TWD)")
    parser_app_create.add_argument("--max-open-positions", type=int, default=5, help="最大持倉部位限制數量")
    parser_app_create.add_argument("--valid-from", type=str, help="授權起始時間 (ISO 格式字串)")
    
    parser_app_val = approval_subs.add_parser("validate", help="驗證授權清單的完整性與簽章")
    parser_app_val.add_argument("manifest_path", type=str, help="授權清單 JSON 檔案路徑")
    
    parser_app_act = approval_subs.add_parser("activate", help="啟用授權清單（依 manifest 內的 strategy_id 設為該策略的有效授權）")
    parser_app_act.add_argument("manifest_path", type=str, help="授權清單 JSON 檔案路徑")

    parser_app_deact = approval_subs.add_parser("deactivate", help="停用指定策略的有效授權（該策略 BUY 將被阻擋，SELL 不受影響）")
    parser_app_deact.add_argument("--strategy", type=str, required=True, help="策略 ID")

    approval_subs.add_parser("list", help="列出各策略當前有效授權與到期日")

    approval_subs.add_parser("status", help="顯示各策略啟用授權清單的每日預檢狀態 (Preflight)")
    
    # 4. account group
    parser_account = subparsers.add_parser("account", help="帳戶管理")
    account_subs = parser_account.add_subparsers(dest="subcommand", required=True)
    
    parser_acc_init = account_subs.add_parser("init", help="初始化投資組合帳戶")
    parser_acc_init.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_acc_init.add_argument("--initial-cash", type=int, required=True, help="初始台幣現金金額")
    
    parser_acc_adjust = account_subs.add_parser("adjust-cash", help="重設帳戶的初始入金總額（會刪除並重寫 INITIAL_DEPOSIT；要 append-only 異動請用 adjust）")
    parser_acc_adjust.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_acc_adjust.add_argument("--amount", type=int, required=True, help="設定的新初始台幣現金金額")

    parser_acc_adj = account_subs.add_parser("adjust", help="現金異動（append-only）：提領用負值、補入用正值，不改寫初始入金歷史")
    parser_acc_adj.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_acc_adj.add_argument("--amount", type=int, required=True, help="異動金額（整數元，提領為負、補入為正）")
    parser_acc_adj.add_argument("--reason", type=str, required=True, help="異動原因（存入 cash_ledger.memo 供稽核）")
    
    # 5. backtest group
    parser_backtest = subparsers.add_parser("backtest", help="歷史回測執行")
    backtest_subs = parser_backtest.add_subparsers(dest="subcommand", required=True)
    
    parser_bt_run = backtest_subs.add_parser("run", help="執行歷史回測")
    parser_bt_run.add_argument("--from", dest="start", type=str, required=True, help="回測開始日期 YYYY-MM-DD")
    parser_bt_run.add_argument("--to", type=str, required=True, help="回測結束日期 YYYY-MM-DD")
    parser_bt_run.add_argument("--initial-cash", type=int, default=300000, help="初始現金金額")
    parser_bt_run.add_argument("--strategy", type=str, default="trend_breakout", help="進場策略 ID（出場由 risk_exit 依該策略 exit: 參數執行）")
    parser_bt_run.add_argument("--db", type=str, default=None, help="回測資料庫路徑（預設用 settings 的 app.db；研究用 data/research.db）")
    parser_bt_run.add_argument("--universe-policy", dest="universe_policy", type=str, default=None,
                               help="PIT 流動性 universe 的 policy_version（如 liquidity-top150-v1）；不給則用 universe.yaml 固定清單（diagnostic、必 INVALID）")

    # 6. simulation group
    parser_sim = subparsers.add_parser("simulation", help="模擬交易執行器指令")
    sim_subs = parser_sim.add_subparsers(dest="subcommand", required=True)
    
    parser_sim_daily = sim_subs.add_parser("run-daily", help="執行每日模擬交易工作流")
    parser_sim_daily.add_argument("--date", type=str, help="執行日期 YYYY-MM-DD")
    parser_sim_daily.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    parser_sim_daily.add_argument("--no-auto-execute", action="store_true", help="真實帳號用：跳過自動成交，只產生訊號/風控/下次執行計畫（不經 FakeBroker 下單）")
    
    parser_sim_exec = sim_subs.add_parser("execute-pending", help="執行待處理的交易訊號包")
    parser_sim_exec.add_argument("--execution-date", type=str, help="執行委託的日期 YYYY-MM-DD")
    parser_sim_exec.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_sim_reset = sim_subs.add_parser("reset", help="重置特定日期的模擬狀態與已產生的交易訊號")
    parser_sim_reset.add_argument("--date", type=str, required=True, help="指定重置的日期 YYYY-MM-DD")
    
    # 7. signal group
    parser_sig = subparsers.add_parser("signal", help="交易訊號產生器指令")
    sig_subs = parser_sig.add_subparsers(dest="subcommand", required=True)
    
    parser_sig_gen = sig_subs.add_parser("generate", help="手動產生收盤交易訊號")
    parser_sig_gen.add_argument("--as-of-date", type=str, help="作為基準的收盤日期 YYYY-MM-DD")
    parser_sig_gen.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    parser_sig_gen.add_argument("--strategy", type=str, default="trend_breakout", help="進場策略 ID")
    
    parser_sig_list = sig_subs.add_parser("list", help="查詢並列出已產生的交易訊號")
    parser_sig_list.add_argument("--date", type=str, help="過濾特定的訊號產生日期 YYYY-MM-DD")
    parser_sig_list.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    # 8. trade group
    parser_trade = subparsers.add_parser("trade", help="交易與委託單指令")
    trade_subs = parser_trade.add_subparsers(dest="subcommand", required=True)
    
    parser_trade_plan = trade_subs.add_parser("plan", help="產生委託計畫預覽")
    parser_trade_plan.add_argument("--bundle", type=str, required=True, help="訊號包 JSON 檔案路徑，或資料庫中的日期/Bundle ID")
    parser_trade_plan.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    
    parser_trade_record = trade_subs.add_parser(
        "record-fill",
        help="手動錄入成交交易資料 (MANUAL_IMPORT)。此指令為 Manifest 授權的例外路徑，僅限事後補錄已在外部券商發生的真實成交事實，不受 Manifest 有效期或額度限制查驗。"
    )
    parser_trade_record.add_argument("--symbol", type=str, required=True, help="股票代號")
    parser_trade_record.add_argument("--side", type=str, required=True, choices=["BUY", "SELL"], help="交易動作 (BUY/SELL)")
    parser_trade_record.add_argument("--quantity", type=int, required=True, help="交易股數")
    parser_trade_record.add_argument("--price", type=float, required=True, help="每股成交價格")
    parser_trade_record.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    parser_trade_record.add_argument(
        "--strategy-id", type=str, default=None,
        help="策略歸屬 ID（如 trend_breakout / pullback_rebound）。預設 MANUAL（結構性排除於 risk_exit 監控）。"
             "依策略訊號自行手動下單者應指定，使該部位納入 risk_exit 停損監控與策略別損益歸因。"
    )
    parser_trade_record.add_argument("--long-term", action="store_true", help="設定此成交為長期持有部位，免受策略自動出場訊號影響")
    parser_trade_record.add_argument("--date", type=str, default=None, help="成交日 YYYY-MM-DD（補錄昨日/前日的真實成交；省略＝今天）。決定現金事件/庫存/損益的經濟日期與 run_id。")

    parser_trade_reject = trade_subs.add_parser(
        "reject-signal",
        help="拒絕執行一個訊號 (REJECTED)，trade plan 與模擬執行將跳過此訊號"
    )
    parser_trade_reject.add_argument("--signal-id", type=str, required=True, help="要拒絕的訊號 ID")
    parser_trade_reject.add_argument("--reason", type=str, default="手動拒絕", help="拒絕原因（顯示於 trade plan）")

    parser_trade_un_reject = trade_subs.add_parser(
        "un-reject-signal",
        help="取消訊號的拒絕標記，恢復為待執行狀態"
    )
    parser_trade_un_reject.add_argument("--signal-id", type=str, required=True, help="要恢復的訊號 ID")
    
    parser_trade_backfill = trade_subs.add_parser(
        "backfill-names",
        help="掃所有持倉，把名稱空白的代號自 Shioaji 查補進 config/stock_names_auto.yaml（修復顯示空白）"
    )
    parser_trade_backfill.add_argument("--account", type=str, default=None, help="目標帳戶名稱（保留參數；持倉掃描為全帳戶）")

    parser_trade_close = trade_subs.add_parser("close-all", help="強制平倉所有持有部位（緊急避險退出）")
    parser_trade_close.add_argument("--broker", type=str, default="fake", help="券商介面名稱")
    parser_trade_close.add_argument("--reason", type=str, required=True, help="緊急平倉的原因")

    parser_trade_exit_check = trade_subs.add_parser(
        "exit-check",
        help="單筆部位出場試算（dry-run）：套某策略 exit 規則跑一次、報告各條件目前數值與是否觸發。純唯讀、不寫入、不發 SELL。"
    )
    parser_trade_exit_check.add_argument("--symbol", type=str, required=True, help="要試算的持倉股票代號")
    parser_trade_exit_check.add_argument("--strategy", type=str, required=True, help="套用其 exit 規則的策略 ID（須具 exit: 區塊，如 trend_breakout）")
    parser_trade_exit_check.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    parser_trade_exit_check.add_argument("--as-of", type=str, default=None, help="試算基準日 YYYY-MM-DD（預設今天，行情取截止當日的最近收盤）")

    parser_trade_set_lt = trade_subs.add_parser(
        "set-long-term",
        help="把既有部位重分類為長期持有（免受策略自動出場），或以 --unset 取消。更新 fills 並重建投影。"
    )
    parser_trade_set_lt.add_argument("--symbol", type=str, required=True, help="要重分類的股票代號")
    parser_trade_set_lt.add_argument("--account", type=str, default=None, help="目標帳戶名稱")
    parser_trade_set_lt.add_argument(
        "--strategy-id", type=str, default=None,
        help="限定要重分類的策略 bucket（預設 MANUAL）。同一 symbol 若同時有手動長期持倉與策略交易部位，"
             "預設只動 MANUAL bucket，避免誤把策略部位排除於 risk_exit 監控。"
    )
    parser_trade_set_lt.add_argument("--unset", action="store_true", help="取消長期標記（改回非長期、可被策略管理）")

    # 9. portfolio group
    parser_portfolio = subparsers.add_parser("portfolio", help="投資組合對帳與投影重建")
    portfolio_subs = parser_portfolio.add_subparsers(dest="subcommand", required=True)
    
    parser_port_rec = portfolio_subs.add_parser("reconcile", help="進行持倉投影與現金流水帳對帳")
    parser_port_rec.add_argument("--account", type=str, default=None, help="帳戶名稱")
    
    parser_port_reb = portfolio_subs.add_parser("rebuild-projections", help="從交易歷史事實重建持倉投影表")
    parser_port_reb.add_argument("--account", type=str, default=None, help="帳戶名稱")
    
    # 10. report group
    parser_report = subparsers.add_parser("report", help="損益與資產淨值曲線報告")
    report_subs = parser_report.add_subparsers(dest="subcommand", required=True)
    
    parser_rep_pnl = report_subs.add_parser("pnl", help="顯示帳戶的損益對帳單摘要")
    parser_rep_pnl.add_argument("--account", type=str, default=None, help="帳戶名稱")
    parser_rep_pnl.add_argument("--date", type=str, help="指定報告日期 YYYY-MM-DD")
    parser_rep_pnl.add_argument("--source", type=str, choices=["all", "strategy", "manual"], default="all", help="篩選成交來源：all (全部), strategy (僅策略), manual (僅手動錄入)")
    parser_rep_pnl.add_argument("--by-strategy", action="store_true", dest="by_strategy", help="依策略 (strategy_id) 分組顯示損益歸因報表")

    parser_rep_daily = report_subs.add_parser("daily", help="產生每日影子報告（與 cron/shadow_daily.sh 同源，純唯讀、落檔）")
    parser_rep_daily.add_argument("--account", type=str, default=None, help="帳戶名稱（預設取 DB 第一個）")
    parser_rep_daily.add_argument("--date", type=str, help="報告日期 YYYY-MM-DD（預設今天）")
    parser_rep_daily.add_argument("--dir", type=str, default="artifacts/reports/daily", help="報告輸出目錄")

    # 11. corporate-action group
    parser_corpact = subparsers.add_parser("corporate-action", help="公司行動（除息、配股）管理")
    corpact_subs = parser_corpact.add_subparsers(dest="subcommand", required=True)

    parser_corp_record = corpact_subs.add_parser("record", help="記錄公司行動事件")
    parser_corp_record.add_argument("--symbol", type=str, required=True, help="標的代號")
    parser_corp_record.add_argument("--type", type=str, required=True, choices=["CASH_DIVIDEND", "STOCK_DIVIDEND"], help="公司行動類型")
    parser_corp_record.add_argument("--ex-date", type=str, required=True, help="除息/除權日期 (YYYY-MM-DD)")
    parser_corp_record.add_argument("--cash-per-share", type=str, help="現金股利（元）")
    parser_corp_record.add_argument("--stock-ratio", type=str, help="配股比率（如 0.1 表示每股配 0.1 股）")
    parser_corp_record.add_argument("--action-id", type=str, help="自訂 action_id（不指定時自動生成）")
    parser_corp_record.add_argument("--memo", type=str, help="備註")

    parser_corp_apply = corpact_subs.add_parser("apply", help="套用公司行動調整（更新均價、水位、現金）")
    parser_corp_apply.add_argument("--account-id", type=str, default="simulation-main", help="帳戶 ID")
    parser_corp_apply.add_argument("--action-id", type=str, help="action_id（--action-id 或 --symbol + --ex-date 二選一）")
    parser_corp_apply.add_argument("--symbol", type=str, help="標的代號")
    parser_corp_apply.add_argument("--ex-date", type=str, help="除息/除權日期 (YYYY-MM-DD)")

    parser_corp_list = corpact_subs.add_parser("list", help="列出已記錄的公司行動事件")

    parser_corp_check = corpact_subs.add_parser("check", help="盤點持倉並比對公司行動登錄狀態")
    parser_corp_check.add_argument("--account", type=str, default="simulation-main", help="帳戶 ID")

    # Dispatching commands
    args = parser.parse_args()

    handlers = {
        ("market", "backfill"): cmd_market_backfill,
        ("market", "backfill-history"): cmd_market_backfill_history,
        ("market", "build-universe"): cmd_market_build_universe,
        ("market", "sync"): cmd_market_sync,
        ("market", "sync-chips"): cmd_market_sync_chips,
        ("market", "sync-names"): cmd_market_sync_names,
        ("market", "validate"): cmd_market_validate,
        ("strategy", "inspect"): cmd_strategy_inspect,
        ("approval", "create"): cmd_approval_create,
        ("approval", "validate"): cmd_approval_validate,
        ("approval", "activate"): cmd_approval_activate,
        ("approval", "deactivate"): cmd_approval_deactivate,
        ("approval", "list"): cmd_approval_list,
        ("approval", "status"): cmd_approval_status,
        ("account", "init"): cmd_account_init,
        ("account", "adjust-cash"): cmd_account_adjust_cash,
        ("account", "adjust"): cmd_account_adjust,
        ("backtest", "run"): cmd_backtest_run,
        ("simulation", "run-daily"): cmd_simulation_run_daily,
        ("simulation", "execute-pending"): cmd_simulation_execute_pending,
        ("simulation", "reset"): cmd_simulation_reset,
        ("signal", "generate"): cmd_signal_generate,
        ("signal", "list"): cmd_signal_list,
        ("trade", "plan"): cmd_trade_plan,
        ("trade", "record-fill"): cmd_trade_record_fill,
        ("trade", "backfill-names"): cmd_trade_backfill_names,
        ("trade", "close-all"): cmd_trade_close_all,
        ("trade", "reject-signal"): cmd_trade_reject_signal,
        ("trade", "un-reject-signal"): cmd_trade_un_reject_signal,
        ("trade", "exit-check"): cmd_trade_exit_check,
        ("trade", "set-long-term"): cmd_trade_set_long_term,
        ("portfolio", "reconcile"): cmd_portfolio_reconcile,
        ("portfolio", "rebuild-projections"): cmd_portfolio_rebuild_projections,
        ("report", "pnl"): cmd_report_pnl,
        ("report", "daily"): cmd_report_daily,
        ("corporate-action", "record"): cmd_corporate_action_record,
        ("corporate-action", "apply"): cmd_corporate_action_apply,
        ("corporate-action", "list"): cmd_corporate_action_list,
        ("corporate-action", "check"): cmd_corporate_action_check,
    }
    
    key = (args.command, args.subcommand) if hasattr(args, "subcommand") else (args.command, None)
    
    # Simple fix for status commands that do not have sub-subcommand
    if args.command == "approval" and args.subcommand == "status":
        key = ("approval", "status")
        
    handler = handlers.get(key)
    if handler:
        handler(args)
    else:
        parser.print_help()

