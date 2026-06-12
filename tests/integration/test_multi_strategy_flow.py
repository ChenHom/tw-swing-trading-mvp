"""多策略每日管線整合測試：

trend_breakout 進場（含大盤濾網與指數同步）→ watermark 持久化 →
risk_exit 固定停損出場 → 損益歸因至原策略。全程經 orchestrator run_daily。
"""
import json
import hashlib
import pytest
from datetime import date, datetime, timezone
from src.contracts.models import (
    StrategyApprovalManifest, MinuteBar, TrendBreakoutParams, ExitParams
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.portfolio.ledger import PortfolioLedger
from src.market_data.provider import FixtureMarketDataProvider
from src.application.runners.simulation import DailySimulationRunner, EntryStrategySpec
from src.strategy.registry import StrategyDefinition
from src.strategy.trend_breakout import TrendBreakoutStrategy
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.trading.allocator import GlobalLimits


def minute_bars(trade_date: date, open_p: float, close_p: float, volume: int) -> list[MinuteBar]:
    return [
        MinuteBar(
            time=datetime(trade_date.year, trade_date.month, trade_date.day, 9, 0, 0, tzinfo=timezone.utc),
            open=open_p, high=max(open_p, close_p), low=min(open_p, close_p), close=open_p,
            volume=volume // 2, amount=open_p * volume
        ),
        MinuteBar(
            time=datetime(trade_date.year, trade_date.month, trade_date.day, 13, 30, 0, tzinfo=timezone.utc),
            open=open_p, high=max(open_p, close_p), low=min(open_p, close_p), close=close_p,
            volume=volume - volume // 2, amount=close_p * volume
        )
    ]


def signed_manifest(strategy_id, params_hash):
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": f"app-{strategy_id}",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": strategy_id,
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": params_hash
        },
        "permissions": {
            "execution_modes": ["simulation"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": 50000,
            "max_daily_buy_value": 100000,
            "max_open_positions": 5
        },
        "validity": {
            "valid_from": "2026-01-01T00:00:00+08:00",
            "expires_at": "2026-12-31T23:59:59+08:00"
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": ""
        }
    }
    canonical_str = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    manifest_dict["integrity"]["digest"] = f"sha256:{digest}"
    return StrategyApprovalManifest(**manifest_dict)


def test_multi_strategy_entry_and_risk_exit_flow(tmp_path):
    db_file = tmp_path / "multi_flow.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    ledger = PortfolioLedger(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    provider = FixtureMarketDataProvider()

    account_id = "simulation-main"
    ledger.deposit(account_id, "init-run", 1000000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger(account_id)

    # 行情：6/1-6/8 平盤 100；6/9 帶量突破收 106；6/10 收 107；6/11 崩跌收 90；6/12 開 90
    days_flat = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8)]
    stock_bars = []
    for d in days_flat:
        stock_bars += minute_bars(d, 100.0, 100.0, 1000)
    stock_bars += minute_bars(date(2026, 6, 9), 105.0, 106.0, 5000)   # 突破 + 1.5x 量
    stock_bars += minute_bars(date(2026, 6, 10), 106.0, 107.0, 1200)  # BUY 於本日開盤執行
    stock_bars += minute_bars(date(2026, 6, 11), 95.0, 90.0, 3000)    # 觸發固定停損 (-7%)
    stock_bars += minute_bars(date(2026, 6, 12), 90.0, 91.0, 1000)    # SELL 於本日開盤執行
    provider.set_fixture_data("2330", stock_bars)

    # 大盤一路走多（高於 5MA）；指數成交量/金額為 0
    index_bars = []
    all_days = days_flat + [date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
    for i, d in enumerate(all_days):
        index_bars += minute_bars(d, 200.0 + i, 200.0 + i, 0)
    provider.set_fixture_data("TSE", index_bars)

    # 策略定義（exit 納入 hash）
    params = TrendBreakoutParams(
        breakout_lookback_days=5, volume_avg_days=5, volume_multiple_pct=150,
        ma_trend_period=5, index_ma_period=5, order_budget_twd=20000
    )
    exit_params = ExitParams(
        fixed_stop_loss_bps=700, trailing_stop_bps=800,
        ma_break_period=20, ma_break_buffer_bps=0, ma_break_confirm_days=2,
        time_stop_days=20, time_stop_min_return_bps=500
    )
    params_hash = StrategyParameterCanonicalizer.compute_strategy_hash(params, exit_params)
    defn = StrategyDefinition(
        strategy_id="trend_breakout", strategy_version="1.0.0",
        params=params, exit_params=exit_params,
        params_hash=params_hash, order_budget_twd=20000
    )
    manifest = signed_manifest("trend_breakout", params_hash)

    runner = DailySimulationRunner(
        db_conn=conn, calendar=calendar, market_provider=provider,
        market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifests={"trend_breakout": manifest},
        entry_specs=[EntryStrategySpec(definition=defn, strategy=TrendBreakoutStrategy(params, ["2330"], "TSE"))],
        exit_definitions={"trend_breakout": defn},
        global_limits=GlobalLimits(max_open_positions=8, max_daily_buy_value=200000, max_new_positions_per_day=2),
        index_symbols=[{"code": "TSE", "exchange": "TSE", "instrument_type": "INDEX"}],
        slippage_bps=10
    )

    # Day 1-6 (6/1-6/8)：平盤無訊號
    for d in days_flat:
        assert runner.run_daily(d, account_id, ["2330"]) == "COMPLETED"

    cursor = conn.cursor()
    # 指數 bar 已同步且型別正確
    cursor.execute("SELECT instrument_type, volume FROM market_bars WHERE symbol='TSE' AND trade_date='2026-06-08'")
    idx_row = cursor.fetchone()
    assert idx_row["instrument_type"] == "INDEX"
    assert idx_row["volume"] == 0

    # 6/9：帶量突破 → BUY bundle（含 strategy_id 的鍵）
    assert runner.run_daily(date(2026, 6, 9), account_id, ["2330"]) == "COMPLETED"
    cursor.execute("SELECT bundle_id, strategy_id FROM signal_bundles WHERE target_execution_date='2026-06-10'")
    row = cursor.fetchone()
    assert row is not None
    assert row["strategy_id"] == "trend_breakout"
    assert "trend_breakout" in row["bundle_id"]

    # 6/10：執行 BUY，watermark 寫入
    assert runner.run_daily(date(2026, 6, 10), account_id, ["2330"]) == "COMPLETED"
    cursor.execute("SELECT SUM(quantity) as q FROM position_lots WHERE symbol='2330' AND strategy_id='trend_breakout'")
    held_qty = cursor.fetchone()["q"]
    assert held_qty and held_qty > 0
    high = projection.get_position_high(account_id, "trend_breakout", "2330", "2026-06-10")
    assert high == 1070000  # 6/10 收盤 107

    # 6/11：收 90 → risk_exit 固定停損 SELL bundle（target 6/12）
    assert runner.run_daily(date(2026, 6, 11), account_id, ["2330"]) == "COMPLETED"
    cursor.execute(
        """
        SELECT i.action, i.reason_code, i.signal_source, b.strategy_id
        FROM signal_items i JOIN signal_bundles b ON i.bundle_id = b.bundle_id
        WHERE b.target_execution_date = '2026-06-12'
        """
    )
    exit_sig = cursor.fetchone()
    assert exit_sig is not None
    assert exit_sig["action"] == "SELL"
    assert exit_sig["reason_code"] == "FIXED_STOP_EXIT"
    assert exit_sig["signal_source"] == "RISK_EXIT"
    assert exit_sig["strategy_id"] == "trend_breakout"  # 歸屬原策略，FIFO 隔離成立

    # 6/12：SELL 執行，部位出清，損益歸因到 trend_breakout
    assert runner.run_daily(date(2026, 6, 12), account_id, ["2330"]) == "COMPLETED"
    cursor.execute("SELECT SUM(quantity) as q FROM position_lots WHERE symbol='2330'")
    assert (cursor.fetchone()["q"] or 0) == 0
    cursor.execute("SELECT strategy_id, realized_amount FROM realized_pnl")
    pnl = cursor.fetchall()
    assert len(pnl) == 1
    assert pnl[0]["strategy_id"] == "trend_breakout"
    assert pnl[0]["realized_amount"] < 0  # 停損為虧損出場

    # daily_runs 為單一 orchestrator run（strategy_id = MULTI）
    cursor.execute("SELECT DISTINCT strategy_id FROM daily_runs")
    assert [r["strategy_id"] for r in cursor.fetchall()] == ["MULTI"]

    # 帳務一致
    assert projection.reconcile(account_id)["status"] == "RECONCILE_OK"
    conn.close()
