import pytest
import json
import hashlib
from datetime import date
from src.contracts.models import MarketBar, TrendPullbackParams, StrategyApprovalManifest
from src.calendar.calendar import ExchangeCalendarsTradingCalendar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.strategy.trend_pullback import TrendPullbackStrategy
from src.strategy.registry import StrategyDefinition
from src.application.runners.backtest import BacktestRunner
from src.application.runners.simulation import EntryStrategySpec

@pytest.fixture
def backtest_setup(tmp_path):
    db_file = tmp_path / "test_backtest.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    
    repo = SqliteMarketBarRepository(conn)
    projection = PortfolioProjection(conn)
    calendar = ExchangeCalendarsTradingCalendar()
    
    yield conn, repo, projection, calendar
    conn.close()

def test_backtest_runner_simple(backtest_setup):
    conn, repo, projection, calendar = backtest_setup
    
    # 1. Populate 6 consecutive trading sessions with market bars for 2330 & 2317
    # 2026-06-08 (Mon) to 2026-06-15 (Mon)
    sessions = calendar.sessions_between(date(2026, 6, 8), date(2026, 6, 15))
    assert len(sessions) == 6
    
    # Put MA parameters to ma_short=2, ma_long=5 (so we have enough history by day 5)
    params = TrendPullbackParams(ma_short=2, ma_long=5, order_budget_twd=20000)
    params_hash = StrategyParameterCanonicalizer.compute_hash(params)
    
    # Prices uptrend, golden cross, and then pullback on day 6
    # 2330 prices: Day 1: 100, Day 2: 101, Day 3: 102, Day 4: 115, Day 5: 116, Day 6: 110 (pullback!)
    # 2317 prices: flat 50.0
    prices_2330 = [100.0, 101.0, 102.0, 115.0, 116.0, 110.0]
    
    for i, s in enumerate(sessions):
        p_2330 = prices_2330[i]
        bar_30 = MarketBar(
            symbol="2330", exchange="TSE", instrument_type="STOCK",
            trade_date=s, open=int(p_2330*10000), high=int((p_2330+1)*10000), low=int((p_2330-1)*10000), close=int(p_2330*10000),
            volume=1000, amount=100000,
            source="shioaji", source_timezone="Asia/Taipei",
            is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
        )
        repo.upsert(bar_30)
        
        # 2317 flat
        bar_17 = MarketBar(
            symbol="2317", exchange="TSE", instrument_type="STOCK",
            trade_date=s, open=500000, high=510000, low=490000, close=500000,
            volume=1000, amount=50000,
            source="shioaji", source_timezone="Asia/Taipei",
            is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
        )
        repo.upsert(bar_17)
        
    # 2. Setup strategy approval manifest
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": "app-v1",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": params_hash
        },
        "permissions": {
            "execution_modes": ["backtest"],
            "risk_increasing_actions": ["open_long", "increase_long"]
        },
        "limits": {
            "currency": "TWD",
            "max_order_value": 30000,
            "max_daily_buy_value": 60000,
            "max_open_positions": 2
        },
        "validity": {
            "valid_from": "2026-06-01T00:00:00+08:00",
            "expires_at": "2026-07-01T00:00:00+08:00"
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
    
    runner = BacktestRunner(
        db_conn=conn,
        calendar=calendar,
        market_repo=repo,
        projection=projection,
        allowed_issuers=["manual-research-review"],
        revoked_approvals=[],
        manifest=manifest,
        strategy_budget=20000,
        slippage_bps=10
    )
    
    # Run backtest (legacy trend_pullback entry strategy via explicit spec)
    entry_spec = EntryStrategySpec(
        definition=StrategyDefinition(
            strategy_id="trend_pullback",
            strategy_version="1.0.0",
            params=params,
            exit_params=None,
            params_hash=params_hash,
            order_budget_twd=params.order_budget_twd
        ),
        strategy=TrendPullbackStrategy(params, ["2330", "2317"])
    )
    result = runner.run(
        start_date=sessions[0],
        end_date=sessions[-1],
        initial_cash=300000,
        universe_symbols=["2330", "2317"],
        entry_spec=entry_spec
    )
    
    assert "run_id" in result
    assert len(result["equity_curve"]) == 6
    assert result["statistics"]["initial_cash"] == 300000
    assert result["statistics"]["trade_count"] == 0 # no trades closed yet (just BUY signal generated on day 6)
