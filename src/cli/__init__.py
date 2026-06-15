from src.cli import common
from src.cli.common import resolve_account_id, sign_manifest
from src.cli.market import cmd_market_backfill, cmd_market_sync, cmd_market_validate
from src.cli.strategy import cmd_strategy_inspect
from src.cli.approval import cmd_approval_create, cmd_approval_validate, cmd_approval_activate, cmd_approval_deactivate, cmd_approval_list, cmd_approval_status
from src.cli.account import cmd_account_init, cmd_account_adjust_cash
from src.cli.backtest import cmd_backtest_run
from src.cli.simulation import cmd_simulation_run_daily, cmd_simulation_reset, cmd_simulation_execute_pending
from src.cli.signal import cmd_signal_generate, cmd_signal_list
from src.cli.trade import cmd_trade_plan, cmd_trade_reject_signal, cmd_trade_un_reject_signal, cmd_trade_record_fill, cmd_trade_close_all
from src.cli.portfolio import cmd_portfolio_reconcile, cmd_portfolio_rebuild_projections
from src.cli.report import cmd_report_pnl
from src.cli.corporate_action import cmd_corporate_action_record, cmd_corporate_action_apply, cmd_corporate_action_list, cmd_corporate_action_check
from src.cli.main import main
