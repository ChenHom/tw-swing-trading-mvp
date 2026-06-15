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


def cmd_signal_generate(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    manifests = common.load_active_manifests(settings)

    as_of_date = date.fromisoformat(args.as_of_date) if args.as_of_date else date.today()
    symbols = [s.code for s in settings.universe.symbols]
    strategy_id = args.strategy
    index_symbol = settings.trading.pipeline.index_symbol

    try:
        defn = common.strategy_registry.load_strategy_definition(settings, strategy_id)
        strategy = common.strategy_registry.build_entry_strategy(defn, symbols, index_symbol)
    except (FileNotFoundError, ValueError) as e:
        print(f"載入策略失敗: {e}")
        sys.exit(1)

    pit_data = repo.as_of(as_of_date)

    # Verify complete data (stocks + index filter data)
    for symbol in symbols:
        if not repo.find(symbol, as_of_date):
            print(f"Error: Missing market bar for {symbol} on {as_of_date}. Run 'market sync' first.")
            sys.exit(1)
    for spec in settings.universe.indices:
        if not repo.find(spec.code, as_of_date):
            print(f"Error: Missing index bar for {spec.code} on {as_of_date}. Run 'market sync' first.")
            sys.exit(1)

    # Gather per-strategy portfolio snapshot (entry strategies see only their own lots)
    account_id = common.resolve_account_id(conn, args.account)
    available_cash = projection.get_cash_balance(account_id)
    positions = {}
    for (pos_sid, symbol), pos in projection.get_strategy_positions(account_id, include_long_term=True).items():
        if pos_sid != strategy_id or pos["quantity"] <= 0:
            continue
        positions[symbol] = PositionSnapshot(
            symbol=symbol,
            quantity=pos["quantity"],
            entry_price=pos["wavg_price"],
            is_long_term=pos["is_long_term"]
        )
    portfolio_snapshot = PortfolioSnapshot(available_cash=available_cash, positions=positions)

    manifest = manifests.get(strategy_id)
    sig_ctx = SignalGenerationContext(
        as_of_date=as_of_date,
        strategy_id=strategy_id,
        strategy_version=defn.strategy_version,
        run_id=f"manual-sig-{as_of_date.strftime('%Y%m%d')}",
        approval_id=manifest.approval_id if manifest else f"no-approval-{strategy_id}",
        params_hash=defn.params_hash
    )

    print(f"Generating [{strategy_id}] signals as of {as_of_date} close...")
    new_bundle = strategy.generate(sig_ctx, pit_data, portfolio_snapshot)

    target_execution_date = calendar.next_trading_day(as_of_date)

    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM signal_bundles WHERE bundle_id = ?", (new_bundle.bundle_id,))
    if cursor.fetchone():
        print(f"Bundle {new_bundle.bundle_id} 已存在（冪等跳過）。")
        conn.close()
        return
    cursor.execute(
        """
        INSERT INTO signal_bundles (
            bundle_id, run_id, approval_id, strategy_id, strategy_version,
            params_hash, signal_date, target_execution_date, market_data_cutoff, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            new_bundle.bundle_id, new_bundle.run_id, new_bundle.approval_id,
            new_bundle.strategy.strategy_id, new_bundle.strategy.strategy_version,
            new_bundle.strategy.params_hash, new_bundle.signal_date.isoformat(),
            target_execution_date.isoformat(), new_bundle.market_data_cutoff.isoformat()
        )
    )
    for sig in new_bundle.signals:
        cursor.execute(
            """
            INSERT INTO signal_items (
                item_id, bundle_id, signal_id, symbol, action, reference_price, reason_code, created_at, signal_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            """,
            (
                f"item-{hashlib.sha256(sig.signal_id.encode()).hexdigest()[:8]}",
                new_bundle.bundle_id, sig.signal_id, sig.symbol, sig.action,
                int(round(sig.reference_price * 10000)), sig.reason_code, sig.signal_source
            )
        )
    conn.commit()
    print(f"Signals generated successfully (Bundle ID: {new_bundle.bundle_id}, Target Execution Date: {target_execution_date})")
    conn.close()


def cmd_signal_list(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)

    manifests = common.load_active_manifests(settings)
    try:
        account_id = common.resolve_account_id(conn, getattr(args, "account", None))
    except Exception:
        account_id = "simulation-main"

    projection = PortfolioProjection(conn)

    query = """
        SELECT
            b.bundle_id,
            b.signal_date,
            b.target_execution_date,
            i.signal_id,
            i.symbol,
            i.action,
            i.reference_price,
            i.reason_code,
            b.strategy_id,
            i.user_override,
            i.override_reason,
            i.signal_source
        FROM signal_items i
        JOIN signal_bundles b ON i.bundle_id = b.bundle_id
    """
    params = []
    if args.date:
        query += " WHERE b.signal_date = ?"
        params.append(args.date)
        
    query += " ORDER BY b.signal_date DESC, i.symbol ASC"
    
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    if not rows:
        if args.date:
            print(f"查無日期 {args.date} 的訊號紀錄。")
        else:
            print("資料庫中無任何訊號紀錄。")
        conn.close()
        return
        
    # Get all execution keys for this account to check for executed fills
    cursor.execute("SELECT execution_key FROM fills WHERE account_id = ?", (account_id,))
    filled_keys = [row["execution_key"] for row in cursor.fetchall()]
    
    # Load current portfolio state for dynamic order planning status of future/today signals
    available_cash = projection.get_cash_balance(account_id)
    
    # Get positions
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (account_id,)
    )
    positions = {row["symbol"]: row["qty"] for row in cursor.fetchall() if row["qty"] > 0}
    
    # Calculate daily buy value spent today (using date.today())
    today = date.today()
    cursor.execute(
        "SELECT SUM(quantity * price) as val FROM fills WHERE account_id = ? AND side = 'BUY' AND date(filled_at) = ?",
        (account_id, today.isoformat())
    )
    val_row = cursor.fetchone()
    daily_buy_value_spent = int(val_row["val"] / 10000.0) if (val_row and val_row["val"]) else 0
    
    portfolio_state = PortfolioState(
        available_cash=available_cash,
        positions=positions,
        daily_buy_value_spent=daily_buy_value_spent
    )

    default_limits = LimitsInfo(
        currency="TWD",
        max_order_value=35000,
        max_daily_buy_value=150000,
        max_open_positions=5
    )

    # Group rows by bundle_id so we can plan them
    bundle_signals = {}
    for r in rows:
        bid = r["bundle_id"]
        if bid not in bundle_signals:
            bundle_signals[bid] = []
        bundle_signals[bid].append(r)

    # Run planner for each bundle to determine planning status for future/today bundles
    planned_results = {} # signal_id -> status_string/list
    for bid, r_list in bundle_signals.items():
        target_exec_date = date.fromisoformat(r_list[0]["target_execution_date"])
        # If target execution date is today or in the future, we plan it dynamically
        if target_exec_date >= today:
            bundle_manifest = manifests.get(r_list[0]["strategy_id"])
            limits = bundle_manifest.limits if bundle_manifest else default_limits
            strategy_budget = bundle_manifest.limits.max_order_value if bundle_manifest else 35000
            signals = [
                SignalItem(
                    signal_id=r["signal_id"],
                    symbol=r["symbol"],
                    action=r["action"],
                    reference_price=float(r["reference_price"] / 10000.0),
                    reason_code=r["reason_code"]
                )
                for r in r_list
            ]
            planned_orders, signal_results = OrderPlanner.plan_all(
                signals=signals,
                portfolio=portfolio_state,
                strategy_budget=strategy_budget,
                manifest_limits=limits
            )
            planned_results.update(signal_results)

    headers = ["訊號日期", "執行日期", "策略", "來源", "代號", "名稱", "動作", "參考價格", "原因", "狀態", "訊號 ID"]
    table_rows = []
    for r in rows:
        ref_price = r["reference_price"] / 10000.0
        symbol = r["symbol"]
        name = common.STOCK_NAMES.get(symbol, "未知")
        override = r["user_override"]
        signal_id = r["signal_id"]
        target_exec_date = date.fromisoformat(r["target_execution_date"])
        
        # Determine status
        if override == "REJECTED":
            status = f"已拒絕 ({r['override_reason'] or '手動拒絕'})"
        else:
            # 1. Check if the signal is filled (already executed)
            is_filled = any(signal_id in key for key in filled_keys)
            if is_filled:
                status = "已成交"
            else:
                # 2. Check if it's in the past (already expired/failed/blocked during execution)
                if target_exec_date < today:
                    status = "已過期/未成交"
                else:
                    # 3. Dynamic planning check for future/today signals
                    planner_res = planned_results.get(signal_id, [])
                    if isinstance(planner_res, str):
                        code = planner_res.split(":")[0].strip()
                        friendly_names = {
                            "MAX_OPEN_POSITIONS_EXCEEDED": "風控阻擋:持倉超限",
                            "DAILY_BUY_LIMIT_EXCEEDED": "風控阻擋:每日買額超限",
                            "INSUFFICIENT_CASH": "風控阻擋:資金不足",
                            "DAILY_NEW_BUY_LIMIT_EXCEEDED": "風控阻擋:新買入超限"
                        }
                        status = friendly_names.get(code, f"風控阻擋:{code}")
                    elif not planner_res and r["action"] == "SELL":
                        status = "無持倉 (跳過)"
                    else:
                        status = "待執行"
                        
        table_rows.append([
            r["signal_date"],
            r["target_execution_date"],
            r["strategy_id"],
            r["signal_source"] or "ENTRY",
            symbol,
            name,
            r["action"],
            f"{ref_price:.2f}",
            r["reason_code"],
            status,
            signal_id,
        ])
        
    # 計算顯示長度的輔助函式（中文字元計為 2 個寬度）
    def display_len(s: str) -> int:
        length = 0
        for char in s:
            if ord(char) > 127:
                length += 2
            else:
                length += 1
        return length

    # 填充顯示寬度的輔助函式（支援中文字元對齊）
    def pad_str(s: str, width: int) -> str:
        cur_len = display_len(s)
        if cur_len >= width:
            return s
        return s + " " * (width - cur_len)
        
    # 計算各欄顯示的最大寬度
    widths = [display_len(h) for h in headers]
    for row in table_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], display_len(str(val)))
            
    header_str = " | ".join(pad_str(h, widths[i]) for i, h in enumerate(headers))
    print(header_str)
    print("-+-".join("-" * w for w in widths))
    for row in table_rows:
        row_str = " | ".join(pad_str(str(val), widths[i]) for i, val in enumerate(row))
        print(row_str)
    print(f"\n共計: {len(table_rows)} 筆訊號。")
    print("\n提示：使用以下指令拒絕執行某筆訊號：")
    print("  python3 -m app trade reject-signal --signal-id <訊號 ID> [--reason '原因']")
    conn.close()


