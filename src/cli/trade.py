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
from src.application.services import exit_check
from src.contracts.stock_names import stock_name

_MONITOR_NOTES = {
    "long_term_excluded": "（長期持有，結構性排除於 risk_exit 監控）",
    "manual_excluded": "（MANUAL，結構性排除於 risk_exit 監控）",
    "monitored": "（已納入 risk_exit 停損監控）",
    "not_monitored": "（此策略無 exit 區塊，不受 risk_exit 監控）",
    "indeterminate": "（策略 exit 設定載入失敗，無法判定監控狀態，請以 strategy inspect 確認）",
}


def cmd_trade_plan(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)
    manifests = common.load_active_manifests(settings)

    # Read bundle
    filepath = Path(args.bundle)
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            bundle_dict = json.load(f)
        bundle = DailySignalBundle(**bundle_dict)
        # File-based bundle: no user_override info available in memory
        user_overrides = {}
    else:
        # Try to load bundle from database by bundle_id or signal_date
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT bundle_id, run_id, approval_id, strategy_id, strategy_version, params_hash, signal_date, target_execution_date, market_data_cutoff
            FROM signal_bundles WHERE bundle_id = ? OR signal_date = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (args.bundle, args.bundle)
        )
        row = cursor.fetchone()
        if not row:
            print(f"找不到檔案且資料庫中也無此 Bundle ID 或日期：{args.bundle}")
            conn.close()
            sys.exit(1)
            
        bundle_id = row["bundle_id"]
        cursor.execute(
            "SELECT signal_id, symbol, action, reference_price, reason_code FROM signal_items WHERE bundle_id = ?",
            (bundle_id,)
        )
        items = []
        for item_row in cursor.fetchall():
            items.append(
                    SignalItem(
                        signal_id=item_row["signal_id"],
                        symbol=item_row["symbol"],
                        action=item_row["action"],
                        reference_price=float(item_row["reference_price"] / 10000.0),
                        reason_code=item_row["reason_code"]
                    )
                )

        # Load user_override map: signal_id -> (override, reason)
        cursor.execute(
            "SELECT signal_id, user_override, override_reason FROM signal_items WHERE bundle_id = ?",
            (bundle_id,)
        )
        user_overrides = {
            r["signal_id"]: (r["user_override"], r["override_reason"])
            for r in cursor.fetchall()
        }
            
        strategy_info = StrategyInfo(
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            params_canonicalization="strategy-params-v1",
            params_hash=row["params_hash"]
        )
        bundle = DailySignalBundle(
            schema_version="1.0",
            bundle_id=bundle_id,
            run_id=row["run_id"],
            approval_id=row["approval_id"],
            strategy=strategy_info,
            signal_date=date.fromisoformat(row["signal_date"]),
            target_execution_date=date.fromisoformat(row["target_execution_date"]),
            market_data_cutoff=date.fromisoformat(row["market_data_cutoff"]),
            signals=items
        )
    # End of DB-based bundle loading

    # Per-strategy manifest routing (§2.7)
    manifest = manifests.get(bundle.strategy.strategy_id)

    # We need to construct PortfolioState
    # Let's get current portfolio state for account
    account_id = common.resolve_account_id(conn, args.account)
    available_cash = projection.get_cash_balance(account_id)
    
    # Get positions
    positions = {}
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, SUM(quantity) as qty FROM position_lots WHERE account_id = ? GROUP BY symbol",
        (account_id,)
    )
    for row in cursor.fetchall():
        qty = row["qty"]
        if qty > 0:
            positions[row["symbol"]] = qty
            
    # Daily buy value spent
    cursor.execute(
        "SELECT SUM(quantity * price) as val FROM fills WHERE account_id = ? AND side = 'BUY' AND date(filled_at) = ?",
        (account_id, bundle.target_execution_date.isoformat())
    )
    val_row = cursor.fetchone()
    daily_buy_value_spent = int(val_row["val"] / 10000.0) if val_row["val"] else 0
    
    portfolio_state = PortfolioState(
        available_cash=available_cash,
        positions=positions,
        daily_buy_value_spent=daily_buy_value_spent
    )
    
    if not manifest:
        print("警告：未載入任何啟用授權清單。將使用預設限制。")
        limits = LimitsInfo(
            currency="TWD",
            max_order_value=35000,
            max_daily_buy_value=150000,
            max_open_positions=5
        )
    else:
        limits = manifest.limits
        
    print(f"帳戶：{account_id} | 可用現金：{available_cash:,} 元 | 當日已買入金額：{daily_buy_value_spent:,} 元")
    print(f"委託計畫預覽 (訊號包: {bundle.bundle_id})：\n")
    
    headers = ["代號", "名稱", "動作", "參考價格", "規劃數量", "單位", "預估金額", "規劃狀態"]
    table_rows = []
    
    planned_orders, signal_results = OrderPlanner.plan_all(
        signals=bundle.signals,
        portfolio=portfolio_state,
        strategy_budget=manifest.limits.max_order_value if manifest else 35000,
        manifest_limits=limits
    )
    
    for sig in bundle.signals:
        symbol = sig.symbol
        name = common.STOCK_NAMES.get(symbol, "未知")
        action = "買入" if sig.action == "BUY" else "賣出"

        # Check user override first
        override, override_reason = user_overrides.get(sig.signal_id, (None, None))
        if override == "REJECTED":
            reason_str = f"已拒絕 ({override_reason})" if override_reason else "已拒絕 (手動拒絕)"
            table_rows.append([
                symbol, name, action, f"{sig.reference_price:.2f}",
                "-", "-", "-", reason_str
            ])
            continue

        result = signal_results.get(sig.signal_id, [])
        if isinstance(result, str):
            table_rows.append([
                symbol, name, action, f"{sig.reference_price:.2f}",
                "阻擋", "-", "-", f"風控阻擋: {result}"
            ])
        elif not result:
            table_rows.append([
                symbol, name, action, f"{sig.reference_price:.2f}",
                "0", "股", "0", "無交易 (無持倉)"
            ])
        else:
            for order in result:
                qty = order["quantity"]
                unit = "張" if (qty >= 1000 and qty % 1000 == 0) else "股"
                display_qty = f"{qty // 1000}" if unit == "張" else f"{qty}"
                est_val = int(qty * sig.reference_price)
                
                table_rows.append([
                    symbol, name, action, f"{sig.reference_price:.2f}",
                    display_qty, unit, f"{est_val:,}", "成功 (待執行)"
                ])
            
    # Print table helper
    def display_len(s: str) -> int:
        length = 0
        for char in s:
            if ord(char) > 127:
                length += 2
            else:
                length += 1
        return length

    def pad_str(s: str, width: int) -> str:
        cur_len = display_len(s)
        if cur_len >= width:
            return s
        return s + " " * (width - cur_len)
        
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
    conn.close()


def cmd_trade_reject_signal(args):
    """標記一個訊號為 REJECTED，trade plan 與 execute-pending 將跳過此訊號。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    try:
        try:
            result = trade_write.reject_signal(
                conn, signal_id=args.signal_id, reason=getattr(args, "reason", None)
            )
        except trade_write.TradeWriteError as e:
            print(e.message)
            sys.exit(1)

        if result["status"] == "already_rejected":
            print(f"訊號 {result['signal_id']} ({result['symbol']} {result['action']}) 已經是拒絕狀態，無需重複設定。")
            return
        action_label = "買入" if result["action"] == "BUY" else "賣出"
        print(f"已拒絕訊號：{result['signal_id']}  ({result['symbol']} {action_label})  原因：{result['reason']}")
    finally:
        conn.close()


def cmd_trade_un_reject_signal(args):
    """取消訊號的 REJECTED 標記，恢復為正常待執行狀態。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    try:
        try:
            result = trade_write.un_reject_signal(conn, signal_id=args.signal_id)
        except trade_write.TradeWriteError as e:
            print(e.message)
            sys.exit(1)

        if result["status"] == "not_rejected":
            print(f"訊號 {result['signal_id']} 目前並非拒絕狀態 (user_override={result['user_override']})，無需操作。")
            return
        action_label = "買入" if result["action"] == "BUY" else "賣出"
        print(f"已恢復訊號：{result['signal_id']}  ({result['symbol']} {action_label})  → 恢復為待執行狀態")
    finally:
        conn.close()


def cmd_trade_record_fill(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    try:
        account_id = common.resolve_account_id(conn, args.account)
        side = args.side.upper()
        is_long_term = bool(getattr(args, "long_term", False))
        strategy_id = getattr(args, "strategy_id", None) or MANUAL_STRATEGY_ID

        # 監控資格的 exit 集合由 CLI 決定後傳入（service 保持為純函式輸入）。
        # 長期/MANUAL 結構性排除，無需查 exit 定義（亦避免不必要的 YAML IO）；
        # 其餘策略才載入，失敗則退 None → service 回報 indeterminate（不誤報錄入失敗）。
        if is_long_term or strategy_id == MANUAL_STRATEGY_ID:
            exit_strategy_ids = None
        else:
            try:
                exit_strategy_ids = set(common.strategy_registry.load_exit_managed_definitions(settings))
            except Exception:
                exit_strategy_ids = None

        try:
            result = trade_write.record_fill(
                conn,
                account_id=account_id,
                symbol=args.symbol,
                side=side,
                quantity=args.quantity,
                price=args.price,
                strategy_id=strategy_id,
                is_long_term=is_long_term,
                exit_strategy_ids=exit_strategy_ids,
                trade_date=getattr(args, "date", None),
            )
        except trade_write.TradeWriteError as e:
            print(e.message)
            sys.exit(1)
        except Exception as e:
            print(f"錄入成交資料失敗: {e}")
            sys.exit(1)

        monitor_note = _MONITOR_NOTES[result["monitor_status"]]
        trade_value = result["trade_value"]
        broker_fee = result["broker_fee"]
        tax = result["tax"]

        print("成功錄入成交資料：")
        print(f"  - 帳戶：{result['account_id']}")
        print(f"  - 標的：{result['symbol']}")
        print(f"  - 策略歸屬：{result['strategy_id']} {monitor_note}")
        print(f"  - 動作：{result['side']}")
        print(f"  - 數量：{result['quantity']} 股")
        print(f"  - 成交單價：{args.price:.2f} 元 (資料庫整數值: {result['price_scaled']})")
        print(f"  - 成交總額：{trade_value:,} TWD (單價 x 數量)")
        print(f"  - 估計手續費：{broker_fee:,} TWD")
        if tax > 0:
            print(f"  - 估計交易稅：{tax:,} TWD")
            total_net = trade_value - broker_fee - tax
            print(f"  - 估計淨收金額：{total_net:,} TWD")
        else:
            total_cost = trade_value + broker_fee
            print(f"  - 估計總付出成本：{total_cost:,} TWD")
    finally:
        conn.close()


def cmd_trade_close_all(args):
    # Manual emergency exit: close every non-long-term bucket (per strategy, FIFO-isolated)
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT strategy_id, symbol, SUM(quantity) as qty
        FROM position_lots
        WHERE is_long_term = 0
        GROUP BY strategy_id, symbol
        """
    )
    open_buckets = [
        (row["strategy_id"], row["symbol"], row["qty"])
        for row in cursor.fetchall() if row["qty"] > 0
    ]

    if not open_buckets:
        print("No open positions to close.")
        conn.close()
        return

    print(f"EMERGENCY CLOSE: Closing {len(open_buckets)} position bucket(s) due to: '{args.reason}'")
    # For each open position, we write a sell fill at today's close or entry price
    # In fake broker mode we simulate immediate closure
    today_dt = date.today()
    repo = SqliteMarketBarRepository(conn)

    for strategy_id, symbol, qty in open_buckets:
        bar = repo.find(symbol, today_dt)
        close_price = bar.close if bar else 1000000 # Default fallback

        # Apply fill transaction
        fill_payload = {
            "fill_id": f"fill-emergency-{common.uuid_like()}",
            "account_id": "simulation-main",
            "run_id": f"emergency-{today_dt.strftime('%Y%m%d')}",
            "order_id": f"ord-emergency-{common.uuid_like()}",
            "execution_key": f"emergency-close-{strategy_id}-{symbol}-{today_dt.isoformat()}",
            "symbol": symbol,
            "side": "SELL",
            "quantity": qty,
            "price": close_price,
            "filled_at": datetime.now().isoformat(),
            "strategy_id": strategy_id
        }
        projection.apply_fill_transaction(fill_payload)
        print(f"  Closed {qty} shares of {symbol} [{strategy_id}] at price {close_price/10000.0:.2f}")

    conn.close()


def _hit_mark(hit: bool) -> str:
    return "✗ 觸發" if hit else "✓ 未觸發"


def cmd_trade_exit_check(args):
    """單筆部位出場試算（dry-run）：套某策略 exit 規則跑一次、報告各條件。純唯讀。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    try:
        account_id = common.resolve_account_id(conn, args.account)
        strategy_id = args.strategy
        try:
            defn = strategy_registry.load_strategy_definition(settings, strategy_id)
        except (FileNotFoundError, ValueError) as e:
            print(f"載入策略設定失敗: {e}")
            sys.exit(1)

        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

        result = exit_check.dry_run_exit(
            conn,
            account_id=account_id,
            symbol=args.symbol,
            definition=defn,
            as_of_date=as_of,
        )

        name = stock_name(args.symbol)
        title = f"{args.symbol}" + (f"（{name}）" if name else "")
        if result["status"] != "OK":
            print(result["message"])
            return

        pos = result["position"]
        d = result["detail"]
        long_tag = "（長期）" if pos["is_long_term"] else ""
        print(f"=== 出場試算：{title} × 策略 {strategy_id} ===")
        print(f"帳戶：{account_id}　試算日：{result['as_of_date']}")
        print(f"持倉：{pos['quantity']} 股{long_tag}　歸屬策略：{pos['strategy_id']}　建倉日：{pos['first_acquired_at'][:10]}")
        print(f"收盤價：{d['close']:.2f} 元　加權均價：{d['wavg']:.2f} 元　當前報酬：{d['time_stop']['return_bps']/100:+.2f}%")
        print("-" * 48)

        fs = d["fixed_stop"]
        print(f"固定停損（-{fs['stop_loss_bps']/100:.1f}%）：跌破 {fs['level']:.2f} 元 → {_hit_mark(fs['hit'])}")

        tr = d["trailing"]
        wm = "watermark 累積最高" if tr["high_from_watermark"] else "max(均價,收盤) 保守初始（無 watermark）"
        print(f"移動停利（-{tr['trailing_stop_bps']/100:.1f}%）：最高 {tr['high']:.2f}（{wm}）→ 跌破 {tr['level']:.2f} 元 → {_hit_mark(tr['hit'])}")

        mb = d["ma_break"]
        if mb["evaluable"]:
            print(f"均線失效（{mb['period']}MA，連 {mb['confirm_days']} 日、buffer {mb['buffer_bps']/100:.1f}%）：最新 SMA {mb['sma']:.2f} 元 → {_hit_mark(mb['hit'])}")
        else:
            print(f"均線失效（{mb['period']}MA）：歷史資料不足，未評估")

        ts = d["time_stop"]
        print(f"時間停損（持有≥{ts['time_stop_days']}日且報酬<{ts['min_return_bps']/100:.1f}%）：已持有 {ts['holding_days']} 交易日 → {_hit_mark(ts['hit'])}")

        print("-" * 48)
        if d["reason"]:
            print(f"結論：**會出場**，觸發條件 = {d['reason']}（以優先序第一個觸發者為準）")
        else:
            print("結論：未觸發任何出場條件，**不會賣出**。")
        print("（dry-run 試算，未寫入任何資料。實際賣出請用 trade record-fill --side SELL）")
    finally:
        conn.close()


def cmd_trade_set_long_term(args):
    """把既有部位重分類為長期持有（或以 --unset 取消）：更新 fills 並重建投影。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    try:
        account_id = common.resolve_account_id(conn, args.account)
        value = not args.unset
        strategy_id = args.strategy_id or MANUAL_STRATEGY_ID
        try:
            result = trade_write.set_long_term(
                conn, account_id=account_id, symbol=args.symbol, value=value, strategy_id=strategy_id
            )
        except trade_write.TradeWriteError as e:
            print(e.message)
            sys.exit(1)

        if result["affected"] == 0:
            print(f"帳戶 '{account_id}' 查無 {args.symbol}（策略 bucket：{strategy_id}）的成交紀錄，未變更。")
            return

        name = stock_name(args.symbol)
        label = "長期持有" if value else "非長期（可被策略管理）"
        print(f"已將 {args.symbol}{f'（{name}）' if name else ''} [{strategy_id}] 重分類為：{label}")
        print(f"  - 更新成交筆數：{result['affected']}")
        print(f"  - 重建後該 bucket 持倉：{result['position_qty']} 股（is_long_term={result['is_long_term']}）")
        recon = result["reconcile_status"]
        print(f"  - 對帳：{'通過' if recon == 'RECONCILE_OK' else recon}")
    finally:
        conn.close()


