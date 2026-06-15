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


def _report_pnl_by_strategy(conn, projection, account_id, report_date):
    """策略別損益歸因報表 (report pnl --by-strategy)。"""
    repo = SqliteMarketBarRepository(conn)
    cursor = conn.cursor()

    positions = projection.get_strategy_positions(account_id, include_long_term=True)

    # Gross realized per strategy (FIFO matches carry strategy_id after migration)
    cursor.execute(
        "SELECT strategy_id, SUM(realized_pnl) as gross FROM fifo_matches WHERE account_id = ? GROUP BY strategy_id",
        (account_id,)
    )
    gross_by_strategy = {r["strategy_id"]: r["gross"] or 0 for r in cursor.fetchall()}

    # Fees/taxes per strategy via fills attribution
    cursor.execute(
        """
        SELECT f.strategy_id as strategy_id, SUM(cl.amount) as fees
        FROM cash_ledger cl
        JOIN fills f ON cl.source_id = f.fill_id
        WHERE cl.account_id = ? AND cl.event_type IN ('BROKER_FEE', 'TRANSACTION_TAX')
        GROUP BY f.strategy_id
        """,
        (account_id,)
    )
    fees_by_strategy = {r["strategy_id"]: r["fees"] or 0 for r in cursor.fetchall()}

    all_strategies = sorted(
        set(gross_by_strategy) | set(fees_by_strategy) | {sid for (sid, _sym) in positions}
    )

    cash = projection.get_cash_balance(account_id)
    print(f"\n--- 帳戶 {account_id} 於 {report_date} 的策略別損益報告 ---")
    print(f"可用現金：{cash:,} TWD")

    for sid in all_strategies:
        gross = gross_by_strategy.get(sid, 0)
        fees = fees_by_strategy.get(sid, 0)
        net_realized = gross + fees  # fees are negative ledger amounts

        sid_positions = []
        for (pos_sid, symbol), pos in sorted(positions.items()):
            if pos_sid != sid:
                continue
            bar = repo.find(symbol, report_date)
            close_price = bar.close / 10000.0 if bar else pos["wavg_price"] / 10000.0
            entry_price = pos["wavg_price"] / 10000.0
            qty = pos["quantity"]
            sid_positions.append({
                "symbol": symbol,
                "quantity": qty,
                "entry_price": entry_price,
                "current_price": close_price,
                "value": int(qty * close_price),
                "unrealized_pnl": int(qty * (close_price - entry_price)),
                "is_long_term": pos["is_long_term"]
            })

        pos_val = sum(p["value"] for p in sid_positions)
        unrealized = sum(p["unrealized_pnl"] for p in sid_positions)

        print(f"\n================ 策略 {sid} ================")
        print(f"部位價值：{pos_val:,} TWD")
        print(f"已實現損益（淨額）：{net_realized:+,} TWD (毛損益: {gross:+,} TWD, 交易規費: {fees:,} TWD)")
        print(f"未實現損益：{unrealized:+,} TWD")
        print("持倉部位：")
        if not sid_positions:
            print("  無持有部位。")
        for pos in sid_positions:
            name = common.STOCK_NAMES.get(pos['symbol'], "未知")
            lt_tag = " [長期]" if pos["is_long_term"] else ""
            print(f"  {pos['symbol']} {name}{lt_tag}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD (未實現: {pos['unrealized_pnl']:+,} TWD)")


def cmd_report_pnl(args):
    settings = common.get_settings()
    conn = get_db_connection(settings.trading.database_path)
    projection = PortfolioProjection(conn)

    account_id = common.resolve_account_id(conn, args.account)
    report_date = date.fromisoformat(args.date) if args.date else date.today()
    filter_source = getattr(args, "source", "all")

    if getattr(args, "by_strategy", False):
        _report_pnl_by_strategy(conn, projection, account_id, report_date)
        conn.close()
        return

    cash = projection.get_cash_balance(account_id)
    
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT pl.symbol, SUM(pl.quantity) as qty,
               CAST(SUM(CAST(pl.quantity AS REAL) * pl.price) / SUM(pl.quantity) AS INTEGER) as avg_price,
               COALESCE(f.source, 'STRATEGY') as source
        FROM position_lots pl
        LEFT JOIN fills f ON pl.fill_id = f.fill_id
        WHERE pl.account_id = ?
        GROUP BY pl.symbol, source
        """,
        (account_id,)
    )
    
    strategy_positions = []
    manual_positions = []
    total_pos_value = 0
    repo = SqliteMarketBarRepository(conn)
    
    for row in cursor.fetchall():
        qty = row["qty"]
        if qty > 0:
            symbol = row["symbol"]
            source = row["source"]
            # Try to find closing price on report_date
            bar = repo.find(symbol, report_date)
            close_price = bar.close / 10000.0 if bar else row["avg_price"] / 10000.0
            pos_val = int(qty * close_price)
            total_pos_value += pos_val
            
            entry_price = row["avg_price"] / 10000.0
            unrealized_pnl = int(qty * (close_price - entry_price))
            
            pos_item = {
                "symbol": symbol,
                "quantity": qty,
                "entry_price": entry_price,
                "current_price": close_price,
                "value": pos_val,
                "unrealized_pnl": unrealized_pnl
            }
            
            if source == "STRATEGY":
                strategy_positions.append(pos_item)
            else:
                manual_positions.append(pos_item)
                
    def get_source_performance(src: str):
        # Gross realized from FIFO matches where buy fill belongs to the source
        cursor.execute(
            """
            SELECT SUM(m.realized_pnl) as gross_pnl
            FROM fifo_matches m
            JOIN fills f ON m.buy_fill_id = f.fill_id
            WHERE m.account_id = ? AND f.source = ?
            """,
            (account_id, src)
        )
        r_gross = cursor.fetchone()
        gross_pnl = r_gross["gross_pnl"] if r_gross["gross_pnl"] is not None else 0
        
        # Fees/taxes from cash_ledger associated with fills of this source
        cursor.execute(
            """
            SELECT SUM(cl.amount) as total_fees_taxes
            FROM cash_ledger cl
            JOIN fills f ON cl.source_id = f.fill_id
            WHERE cl.account_id = ? AND f.source = ? AND cl.event_type IN ('BROKER_FEE', 'TRANSACTION_TAX')
            """,
            (account_id, src)
        )
        r_fees = cursor.fetchone()
        fees_taxes = r_fees["total_fees_taxes"] if r_fees["total_fees_taxes"] is not None else 0
        
        return gross_pnl, fees_taxes

    strat_gross_pnl, strat_fees_taxes = get_source_performance("STRATEGY")
    strat_net_realized = strat_gross_pnl + strat_fees_taxes
    strat_unrealized = sum(p["unrealized_pnl"] for p in strategy_positions)
    strat_pos_val = sum(p["value"] for p in strategy_positions)
    
    manual_gross_pnl, manual_fees_taxes = get_source_performance("MANUAL_IMPORT")
    manual_net_realized = manual_gross_pnl + manual_fees_taxes
    manual_unrealized = sum(p["unrealized_pnl"] for p in manual_positions)
    manual_pos_val = sum(p["value"] for p in manual_positions)
    
    if filter_source == "strategy":
        print(f"\n--- 帳戶 {account_id} 於 {report_date} 的損益報告 (僅看策略交易) ---")
        print(f"部位價值：{strat_pos_val:,} TWD")
        print(f"已實現損益（淨額）：{strat_net_realized:+,} TWD (毛損益: {strat_gross_pnl:+,} TWD, 交易規費: {strat_fees_taxes:,} TWD)")
        print(f"未實現損益：{strat_unrealized:+,} TWD")
        print("\n持倉部位：")
        if not strategy_positions:
            print("  無持有部位。")
        for pos in strategy_positions:
            name = common.STOCK_NAMES.get(pos['symbol'], "未知")
            print(f"  {pos['symbol']} {name}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD (未實現: {pos['unrealized_pnl']:+,} TWD)")
            
    elif filter_source == "manual":
        print(f"\n--- 帳戶 {account_id} 於 {report_date} 的損益報告 (僅看手動錄入) ---")
        print(f"部位價值：{manual_pos_val:,} TWD")
        print(f"已實現損益（淨額）：{manual_net_realized:+,} TWD (毛損益: {manual_gross_pnl:+,} TWD, 交易規費: {manual_fees_taxes:,} TWD)")
        print(f"未實現損益：{manual_unrealized:+,} TWD")
        print("\n持倉部位：")
        if not manual_positions:
            print("  無持有部位。")
        for pos in manual_positions:
            name = common.STOCK_NAMES.get(pos['symbol'], "未知")
            print(f"  {pos['symbol']} {name}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD (未實現: {pos['unrealized_pnl']:+,} TWD)")
            
    else:
        print(f"\n--- 帳戶 {account_id} 於 {report_date} 的損益報告 ---")
        print(f"可用現金：{cash:,} TWD")
        print(f"部位總價值：{total_pos_value:,} TWD")
        print(f"總資產淨值：{cash + total_pos_value:,} TWD")
        
        print(f"\n================ 策略自動交易 (STRATEGY) ================")
        print(f"部位價值：{strat_pos_val:,} TWD")
        print(f"已實現損益（淨額）：{strat_net_realized:+,} TWD (毛損益: {strat_gross_pnl:+,} TWD, 交易規費: {strat_fees_taxes:,} TWD)")
        print(f"未實現損益：{strat_unrealized:+,} TWD")
        print("持倉部位：")
        if not strategy_positions:
            print("  無持有部位。")
        for pos in strategy_positions:
            name = common.STOCK_NAMES.get(pos['symbol'], "未知")
            print(f"  {pos['symbol']} {name}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD (未實現: {pos['unrealized_pnl']:+,} TWD)")
            
        print(f"\n================ 手動錄入交易 (MANUAL_IMPORT) ================")
        print(f"部位價值：{manual_pos_val:,} TWD")
        print(f"已實現損益（淨額）：{manual_net_realized:+,} TWD (毛損益: {manual_gross_pnl:+,} TWD, 交易規費: {manual_fees_taxes:,} TWD)")
        print(f"未實現損益：{manual_unrealized:+,} TWD")
        print("持倉部位：")
        if not manual_positions:
            print("  無持有部位。")
        for pos in manual_positions:
            name = common.STOCK_NAMES.get(pos['symbol'], "未知")
            print(f"  {pos['symbol']} {name}: {pos['quantity']} 股 @ 均價 {pos['entry_price']:.2f} (現價: {pos['current_price']:.2f}) - 價值: {pos['value']:,} TWD (未實現: {pos['unrealized_pnl']:+,} TWD)")
            
    conn.close()


