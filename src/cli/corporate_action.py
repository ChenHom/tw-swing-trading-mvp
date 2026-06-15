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


def cmd_corporate_action_record(args):
    """記錄公司行動（除息、配股等）。"""
    import uuid
    from datetime import datetime

    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)

    cursor = conn.cursor()

    action_id = args.action_id or uuid.uuid4().hex
    action_type = args.type.upper()

    if action_type == "CASH_DIVIDEND":
        if not args.cash_per_share:
            print("Error: --cash-per-share 必須指定現金股利")
            conn.close()
            return
        cash_per_share = int(float(args.cash_per_share) * 10000)
        cursor.execute(
            """
            INSERT INTO corporate_actions
            (action_id, symbol, action_type, ex_date, cash_per_share, source, memo, created_at)
            VALUES (?, ?, ?, ?, ?, 'MANUAL', ?, datetime('now'))
            """,
            (action_id, args.symbol, action_type, args.ex_date, cash_per_share, args.memo or "")
        )
    elif action_type == "STOCK_DIVIDEND":
        if not args.stock_ratio:
            print("Error: --stock-ratio 必須指定配股比率")
            conn.close()
            return
        stock_ratio = float(args.stock_ratio)
        cursor.execute(
            """
            INSERT INTO corporate_actions
            (action_id, symbol, action_type, ex_date, stock_ratio, source, memo, created_at)
            VALUES (?, ?, ?, ?, ?, 'MANUAL', ?, datetime('now'))
            """,
            (action_id, args.symbol, action_type, args.ex_date, stock_ratio, args.memo or "")
        )

    conn.commit()
    print(f"✅ 已記錄 {action_type} 事件 ({args.symbol}, ex_date={args.ex_date})")
    print(f"   action_id: {action_id}")
    conn.close()


def cmd_corporate_action_apply(args):
    """套用公司行動調整（更新均價、水位、現金）。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)

    cursor = conn.cursor()

    # 查詢要套用的公司行動
    if args.action_id:
        cursor.execute("SELECT * FROM corporate_actions WHERE action_id = ?", (args.action_id,))
    else:
        cursor.execute(
            "SELECT * FROM corporate_actions WHERE symbol = ? AND ex_date = ?",
            (args.symbol, args.ex_date)
        )

    row = cursor.fetchone()
    if not row:
        print("Error: 未找到該公司行動事件")
        conn.close()
        return

    action = dict(row)
    projection = PortfolioProjection(conn)

    # 套用調整（冪等）
    projection.apply_corporate_action(args.account_id, action)

    print(f"✅ 已套用 {action['action_type']} 調整 ({action['symbol']}, {args.account_id})")
    print(f"   ex_date: {action['ex_date']}")
    conn.close()


def cmd_corporate_action_list(args):
    """列出已記錄的公司行動事件。"""
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT action_id, symbol, action_type, ex_date, cash_per_share, stock_ratio, created_at
        FROM corporate_actions
        ORDER BY ex_date DESC
        """
    )

    rows = cursor.fetchall()
    if not rows:
        print("無已記錄的公司行動事件")
        conn.close()
        return

    print("已記錄的公司行動事件：")
    for row in rows:
        row = dict(row)
        detail = ""
        if row["action_type"] == "CASH_DIVIDEND":
            detail = f"現金股利 {row['cash_per_share']/10000:.2f} 元"
        elif row["action_type"] == "STOCK_DIVIDEND":
            detail = f"配股 {row['stock_ratio']:.2%}"
        print(f"  [{row['ex_date']}] {row['symbol']}: {detail} (ID: {row['action_id'][:8]}...)")

    conn.close()


def cmd_corporate_action_check(args):
    """盤點持倉並比對公司行動登錄狀態（純讀，供除息日前自查）。"""
    from datetime import date as _date
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)

    account_id = args.account or "simulation-main"
    today = _date.today().isoformat()

    # 監控資格集合（具 exit 區塊的策略）
    try:
        exit_ids = set(common.strategy_registry.load_exit_managed_definitions(settings))
    except Exception:
        exit_ids = set()

    # 持倉（含長期）
    positions = projection.get_strategy_positions(account_id, include_long_term=True)
    held_symbols = sorted({sym for (_sid, sym) in positions})

    if not held_symbols:
        print(f"帳戶 {account_id} 無持倉。")
        conn.close()
        return

    cursor = conn.cursor()
    # 各標的「未來除息事件」與「是否已套用」
    print(f"=== 公司行動盤點：{account_id}（今天 {today}）===\n")
    print(f"持倉標的 {len(held_symbols)} 檔：")
    for (sid, sym), pos in sorted(positions.items()):
        monitored = (sid != "MANUAL") and (sid in exit_ids) and not pos["is_long_term"]
        mark = "✓監控" if monitored else "—"
        print(f"  {sym} [{sid}] {pos['quantity']} 股 @ {pos['wavg_price']/10000:.2f} 元  {mark}")

    print("\n登錄之除息/除權事件（ex_date >= 今天）：")
    cursor.execute(
        """
        SELECT ca.action_id, ca.symbol, ca.action_type, ca.ex_date, ca.cash_per_share, ca.stock_ratio,
               (SELECT COUNT(*) FROM position_cost_adjustments pca WHERE pca.action_id = ca.action_id) AS applied_cnt
        FROM corporate_actions ca
        WHERE ca.ex_date >= ?
        ORDER BY ca.ex_date
        """,
        (today,)
    )
    upcoming = cursor.fetchall()
    registered_symbols = set()
    if not upcoming:
        print("  （無）")
    for r in upcoming:
        r = dict(r)
        registered_symbols.add(r["symbol"])
        if r["action_type"] == "CASH_DIVIDEND":
            detail = f"現金股利 {r['cash_per_share']/10000:.2f} 元/股"
        else:
            detail = f"配股 {r['stock_ratio']:.2%}"
        status = "已套用" if r["applied_cnt"] > 0 else "⚠未套用"
        held = "（持倉中）" if r["symbol"] in held_symbols else ""
        print(f"  [{r['ex_date']}] {r['symbol']}: {detail} — {status} {held}")

    # 持倉但無任何登錄事件 → 提醒自查
    unregistered = [s for s in held_symbols if s not in registered_symbols]
    if unregistered:
        print("\n⚠ 下列持倉標的無登錄之除息事件，請自公開資訊觀測站 / 證交所查 6–8 月除息日：")
        print(f"  {', '.join(unregistered)}")
        print("  （除息日前未登錄並套用調整，watermark / 停損基準會失真。）")

    conn.close()


