import pytest
import json
import hashlib
from datetime import date, datetime, timezone
from src.contracts.models import (
    MarketBar, TrendPullbackParams, StrategyApprovalManifest, StrategyInfo, MinuteBar
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.portfolio.ledger import PortfolioLedger
from src.market_data.provider import FixtureMarketDataProvider
from src.application.runners.simulation import DailySimulationRunner, EntryStrategySpec
from src.strategy.trend_pullback import TrendPullbackStrategy
from src.strategy.registry import StrategyDefinition
from src.trading.allocator import GlobalLimits

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_simulation_flow.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    yield conn
    conn.close()

def create_minute_bars(trade_date: date, open_p: float, close_p: float) -> list[MinuteBar]:
    return [
        MinuteBar(
            time=datetime(trade_date.year, trade_date.month, trade_date.day, 9, 0, 0, tzinfo=timezone.utc),
            open=open_p, high=max(open_p, close_p), low=min(open_p, close_p), close=open_p,
            volume=100, amount=open_p * 100
        ),
        MinuteBar(
            time=datetime(trade_date.year, trade_date.month, trade_date.day, 13, 30, 0, tzinfo=timezone.utc),
            open=open_p, high=max(open_p, close_p), low=min(open_p, close_p), close=close_p,
            volume=100, amount=close_p * 100
        )
    ]

def test_daily_simulation_flow_integration(temp_db):
    calendar = ExchangeCalendarsTradingCalendar()
    provider = FixtureMarketDataProvider()
    repo = SqliteMarketBarRepository(temp_db)
    projection = PortfolioProjection(temp_db)
    ledger = PortfolioLedger(temp_db)
    
    account_id = "simulation-main"
    
    # Setup initial cash balance (1,000,000 TWD)
    ledger.deposit(account_id, "init-run", 1000000, "TWD", date(2026, 6, 3))
    projection.rebuild_from_ledger(account_id)
    
    # 1. Setup fixture data for 6 consecutive trading days
    # Day 1: 2026-06-03 (Wed) -> close = 100.0
    # Day 2: 2026-06-04 (Thu) -> close = 100.0
    # Day 3: 2026-06-05 (Fri) -> close = 100.0
    # Day 4: 2026-06-08 (Mon) -> close = 110.0
    # Day 5: 2026-06-09 (Tue) -> close = 106.0  (Will trigger BUY signal for Day 6)
    # Day 6: 2026-06-10 (Wed) -> open = 107.0   (Will execute BUY order at 107.0 + slippage)
    
    provider.set_fixture_data("2330", 
        create_minute_bars(date(2026, 6, 3), 100.0, 100.0) +
        create_minute_bars(date(2026, 6, 4), 100.0, 100.0) +
        create_minute_bars(date(2026, 6, 5), 100.0, 100.0) +
        create_minute_bars(date(2026, 6, 8), 110.0, 110.0) +
        create_minute_bars(date(2026, 6, 9), 106.0, 106.0) +
        create_minute_bars(date(2026, 6, 10), 107.0, 107.0)
    )
    
    # 2. Setup active manifest with ma_short=2, ma_long=5
    params = TrendPullbackParams(
        ma_short=2,
        ma_long=5,
        stop_loss_bps=500,
        take_profit_bps=1200,
        order_budget_twd=300000
    )
    params_hash = hashlib.sha256(
        json.dumps(params.model_dump(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": "app-m4-flow",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": f"sha256:{params_hash}"
        },
        "permissions": {
            "execution_modes": ["simulation"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": 350000,
            "max_daily_buy_value": 500000,
            "max_open_positions": 5
        },
        "validity": {
            "valid_from": "2026-06-01T00:00:00+08:00",
            "expires_at": "2026-06-30T00:00:00+08:00"
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
    manifest = StrategyApprovalManifest(**manifest_dict)
    
    entry_spec = EntryStrategySpec(
        definition=StrategyDefinition(
            strategy_id="trend_pullback",
            strategy_version="1.0.0",
            params=params,
            exit_params=None,
            params_hash=f"sha256:{params_hash}",
            order_budget_twd=300000
        ),
        strategy=TrendPullbackStrategy(params, ["2330"])
    )
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=provider,
        market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifests={"trend_pullback": manifest},
        entry_specs=[entry_spec],
        exit_definitions={},
        global_limits=GlobalLimits(max_open_positions=5, max_daily_buy_value=500000, max_new_positions_per_day=2),
        slippage_bps=10
    )
    
    # Run Day 1 to 4:
    # Insufficient history, no signals
    for d in [date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8)]:
        status = runner.run_daily(d, account_id, ["2330"])
        assert status == "COMPLETED"
    
    # Run Day 5: 2026-06-09
    # Has 5 days of history: close prices [100.0, 100.0, 100.0, 110.0, 106.0]
    # ma_short = (110 + 106) / 2 = 108.0
    # ma_long = (100 + 100 + 100 + 110 + 106) / 5 = 103.2
    # latest_close = 106.0
    # Conditions:
    # 1. 106.0 > 103.2 (close > ma_long) -> True
    # 2. 108.0 > 103.2 (ma_short > ma_long) -> True
    # 3. 106.0 < 108.0 (close < ma_short) -> True
    # Triggers BUY signal targeting 2026-06-10
    status = runner.run_daily(date(2026, 6, 9), account_id, ["2330"])
    assert status == "COMPLETED"
    
    # Check that bundle targeting 2026-06-10 is in database
    cursor = temp_db.cursor()
    cursor.execute(
        "SELECT bundle_id, target_execution_date FROM signal_bundles WHERE target_execution_date = '2026-06-10'"
    )
    row = cursor.fetchone()
    assert row is not None
    bundle_id = row["bundle_id"]
    
    cursor.execute("SELECT symbol, action FROM signal_items WHERE bundle_id = ?", (bundle_id,))
    item = cursor.fetchone()
    assert item is not None
    assert item["symbol"] == "2330"
    assert item["action"] == "BUY"
    
    # Run Day 6: 2026-06-10
    # Will sync Day 6, execute pending BUY bundle at 107.0 + 10bps slippage = 107.107 (rounded price x 10000 = 1071070)
    # Budget = 300,000 TWD. Shares = 300000 // 107.107 = 2800 shares (整張 2000, 零股 800)
    status = runner.run_daily(date(2026, 6, 10), account_id, ["2330"])
    assert status == "COMPLETED"
    
    # Verify fill records
    cursor.execute("SELECT side, quantity, price FROM fills WHERE account_id = ?", (account_id,))
    fills = cursor.fetchall()
    assert len(fills) == 2  # Split into 2000 (standard lot) + 800 (odd lot)
    
    total_qty = sum(f["quantity"] for f in fills)
    assert total_qty == 2830
    
    # Check remaining cash
    # 1,000,000 - (trade_value + broker_fee) = 1,000,000 - (303113 + 432) = 696,455 TWD
    cash = projection.get_cash_balance(account_id)
    assert cash == 696455
    
    # Position Projection
    cursor.execute("SELECT symbol, quantity FROM position_lots WHERE account_id = ?", (account_id,))
    pos = cursor.fetchall()
    assert len(pos) == 2 # 2 lots (2000, 830)
    assert sum(p["quantity"] for p in pos) == 2830
