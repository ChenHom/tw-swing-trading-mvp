import pytest
import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock
from src.contracts.models import (
    MarketBar, TrendPullbackParams, StrategyApprovalManifest, DailySignalBundle, SignalItem, StrategyInfo
)
from src.calendar.calendar import ExchangeCalendarsTradingCalendar, TradingCalendar
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.projection import PortfolioProjection
from src.application.runners.simulation import DailySimulationRunner
from src.portfolio.ledger import PortfolioLedger

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_sim.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    yield conn
    conn.close()

@pytest.fixture
def dummy_manifest():
    return StrategyApprovalManifest(
        schema_version="1.0",
        approval_id="app-sim-test",
        issuer_id="manual-research-review",
        strategy=StrategyInfo(
            strategy_id="trend_pullback",
            strategy_version="1.0.0",
            params_canonicalization="strategy-params-v1",
            params_hash="hash-123"
        ),
        permissions={
            "execution_modes": ["simulation"],
            "risk_increasing_actions": ["open_long"]
        },
        limits={
            "currency": "TWD",
            "max_order_value": 50000,
            "max_daily_buy_value": 200000,
            "max_open_positions": 5
        },
        validity={
            "valid_from": "2026-06-01T00:00:00+08:00",
            "expires_at": "2026-06-30T00:00:00+08:00"
        },
        integrity={
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": "sha256:4d60232230beff537b0b23023023023023023023023023023023023023023023" # Dummy format
        }
    )

def test_get_preflight_status(temp_db, dummy_manifest):
    calendar = ExchangeCalendarsTradingCalendar()
    mock_provider = MagicMock()
    repo = SqliteMarketBarRepository(temp_db)
    projection = PortfolioProjection(temp_db)
    
    # 1. Missing manifest
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifest=None
    )
    assert runner.get_preflight_status(date(2026, 6, 10)) == "MISSING"
    
    # 2. Revoked manifest
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=["app-sim-test"],
        manifest=dummy_manifest
    )
    assert runner.get_preflight_status(date(2026, 6, 10)) == "REVOKED"
    
    # 3. Naive check / success
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifest=dummy_manifest, expiry_warning_sessions=3
    )
    # Mock validation because digest in dummy manifest is mock
    with MagicMock() as mock_validator:
        # We can bypass check by letting mock_validator do nothing
        pass
        
    # Let's adjust digest check using patch
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.approval.validator.ManifestValidator.validate", lambda self, m, t, mode: None)
        
        # Test active status (expiry is 2026-06-30)
        assert runner.get_preflight_status(date(2026, 6, 10)) == "ACTIVE"
        
        # Test expiring soon status
        # June 30 is expiry, June 29 is 1 session away
        assert runner.get_preflight_status(date(2026, 6, 29)) == "EXPIRING_SOON"
        
        # Test expired status
        assert runner.get_preflight_status(date(2026, 7, 1)) == "EXPIRED"
        
        # Test not yet valid
        assert runner.get_preflight_status(date(2026, 5, 31)) == "NOT_YET_VALID"

def test_run_daily_skipped_non_trading_day(temp_db):
    calendar = ExchangeCalendarsTradingCalendar()
    mock_provider = MagicMock()
    repo = SqliteMarketBarRepository(temp_db)
    projection = PortfolioProjection(temp_db)
    
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=[], revoked_approvals=[]
    )
    
    # 2026-06-07 is Sunday (non-trading day)
    status = runner.run_daily(
        run_date=date(2026, 6, 7),
        account_id="sim-test",
        strategy_id="trend_pullback",
        strategy_params=TrendPullbackParams(order_budget_twd=30000),
        universe_symbols=["2330"]
    )
    assert status == "SKIPPED"
    
    # Verify daily_runs row exists
    cursor = temp_db.cursor()
    cursor.execute("SELECT status, last_error_code FROM daily_runs WHERE run_date = '2026-06-07'")
    row = cursor.fetchone()
    assert row is not None
    assert row["status"] == "SKIPPED"
    assert row["last_error_code"] == "NON_TRADING_DAY"

def test_run_daily_waiting_market_data(temp_db):
    calendar = ExchangeCalendarsTradingCalendar()
    # Mock provider returns no kbars
    mock_provider = MagicMock()
    mock_provider.fetch_kbars.return_value = []
    
    repo = SqliteMarketBarRepository(temp_db)
    projection = PortfolioProjection(temp_db)
    
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=[], revoked_approvals=[]
    )
    
    # 2026-06-10 is Wednesday (trading day)
    status = runner.run_daily(
        run_date=date(2026, 6, 10),
        account_id="sim-test",
        strategy_id="trend_pullback",
        strategy_params=TrendPullbackParams(order_budget_twd=30000),
        universe_symbols=["2330"]
    )
    assert status == "WAITING"
    
    # Verify sub status
    cursor = temp_db.cursor()
    cursor.execute(
        "SELECT status, market_sync_status, last_error_code FROM daily_runs WHERE run_date = '2026-06-10'"
    )
    row = cursor.fetchone()
    assert row["status"] == "WAITING"
    assert row["market_sync_status"] == "PENDING"
    assert row["last_error_code"] == "WAITING_MARKET_DATA"

def test_run_daily_idempotency_already_completed(temp_db):
    calendar = ExchangeCalendarsTradingCalendar()
    mock_provider = MagicMock()
    repo = SqliteMarketBarRepository(temp_db)
    projection = PortfolioProjection(temp_db)
    
    # Directly insert completed run
    cursor = temp_db.cursor()
    cursor.execute(
        """
        INSERT INTO daily_runs (
            run_id, run_date, account_id, strategy_id, status, market_sync_status,
            execution_status, signal_generation_status, report_status, started_at, completed_at
        ) VALUES ('sim-20260610', '2026-06-10', 'sim-test', 'trend_pullback', 'COMPLETED', 'COMPLETED', 'COMPLETED', 'COMPLETED', 'COMPLETED', 'now', 'now')
        """
    )
    temp_db.commit()
    
    runner = DailySimulationRunner(
        db_conn=temp_db, calendar=calendar, market_provider=mock_provider,
        market_repo=repo, projection=projection,
        allowed_issuers=[], revoked_approvals=[]
    )
    
    status = runner.run_daily(
        run_date=date(2026, 6, 10),
        account_id="sim-test",
        strategy_id="trend_pullback",
        strategy_params=TrendPullbackParams(order_budget_twd=30000),
        universe_symbols=["2330"]
    )
    
    # Provider shouldn't be called because status is already COMPLETED
    assert status == "COMPLETED"
    mock_provider.fetch_kbars.assert_not_called()


# ── Integration: engine must NOT crash when long-term lot blocks a SELL ───────

def test_engine_skips_sell_blocked_by_long_term_position():
    """
    Regression test for the bug where simulation run-daily crashed with
    SELL_WITHOUT_POSITION when the strategy issued a SELL for a symbol
    held as a long-term (is_long_term=1) position.

    Expected behaviour: engine skips the SELL fill gracefully and returns
    COMPLETED; the long-term lot remains untouched.
    """
    import uuid
    from src.application.execution.engine import TradeExecutionEngine
    from src.contracts.models import (
        DailySignalBundle, SignalItem, StrategyInfo,
        TrendPullbackParams,
    )
    from src.portfolio.db import init_db, get_db_connection
    from src.portfolio.projection import PortfolioProjection
    from src.application.execution.engine import ExecutionContext

    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        init_db(db_path)
        conn = get_db_connection(db_path)
        projection = PortfolioProjection(conn)

        # Set up account with cash
        conn.execute(
            "INSERT INTO cash_balances (account_id, balance, currency, updated_at) "
            "VALUES ('test-acct', 1000000, 'TWD', datetime('now'))"
        )
        conn.commit()

        # Record a long-term BUY for 2330 (simulating manual record-fill --long-term)
        lt_fill = {
            "fill_id": "fill-lt-buy-1",
            "account_id": "test-acct",
            "run_id": "manual-20260611",
            "order_id": "ord-lt-1",
            "execution_key": "lt-buy-2330-1",
            "symbol": "2330",
            "side": "BUY",
            "quantity": 66,
            "price": 19752300,  # 1975.23
            "filled_at": "2026-06-10T09:00:00",
            "is_long_term": 1,
            "source": "MANUAL_IMPORT",
        }
        projection.apply_fill_transaction(lt_fill)

        # Build a SELL signal bundle for 2330 (as strategy would generate)
        sell_signal = SignalItem(
            item_id="item-sell-1",
            signal_id="sig-sell-1",
            symbol="2330",
            action="SELL",
            reference_price=22550000,
            reason_code="TAKE_PROFIT_EXIT",
        )
        bundle = DailySignalBundle(
            schema_version="1.0",
            bundle_id="bundle-test-lt",
            run_id="run-lt-test",
            approval_id="app-lt-test",
            strategy=StrategyInfo(
                strategy_id="trend_pullback",
                strategy_version="1.0.0",
                params_canonicalization="strategy-params-v1",
                params_hash="hash-lt-test",
            ),
            signal_date=date(2026, 6, 10),
            target_execution_date=date(2026, 6, 11),
            market_data_cutoff=date(2026, 6, 10),
            signals=[sell_signal],
        )

        # Mock market data returning today's close for 2330
        mock_repo = MagicMock()
        mock_repo.find.return_value = MarketBar(
            symbol="2330", exchange="TSE", instrument_type="STOCK",
            trade_date=date(2026, 6, 11),
            open=22550000, high=22550000, low=22550000, close=22550000,
            volume=1000, amount=10000000,
            source="shioaji", source_fetched_at="now", raw_payload_checksum="chk",
        )

        lt_manifest = StrategyApprovalManifest(
            schema_version="1.0",
            approval_id="app-lt-test",
            issuer_id="manual-research-review",
            strategy=StrategyInfo(
                strategy_id="trend_pullback",
                strategy_version="1.0.0",
                params_canonicalization="strategy-params-v1",
                params_hash="hash-lt-test",
            ),
            permissions={
                "execution_modes": ["simulation"],
                "risk_increasing_actions": ["open_long"],
            },
            limits={
                "currency": "TWD",
                "max_order_value": 500000,
                "max_daily_buy_value": 500000,
                "max_open_positions": 10,
            },
            validity={
                "valid_from": "2026-01-01T00:00:00+08:00",
                "expires_at": "2099-12-31T23:59:59+08:00",
            },
            integrity={
                "algorithm": "sha256",
                "canonicalization": "manifest-v1",
                "digest": "sha256:f19e6152422fbd2e3431edd4ba4a48c367cd7e8ca2fc4f55845878c49550e185",
            },
        )

        engine = TradeExecutionEngine(
            db_conn=conn,
            market_repo=mock_repo,
            projection=projection,
            allowed_issuers=["manual-research-review"],
            revoked_approvals=[],
            manifest=lt_manifest,
            strategy_budget=500000,
        )

        context = ExecutionContext(
            account_id="test-acct",
            run_id="run-lt-test",
            run_type="DAILY_SIMULATION",
            as_of_date=date(2026, 6, 10),
            execution_date=date(2026, 6, 11),
        )

        # This must NOT raise — engine should skip the SELL and return COMPLETED
        result = engine.execute_bundle(context, bundle)

        assert result["status"] == "COMPLETED", f"Expected COMPLETED, got {result}"
        # SELL was skipped: fills list should be empty
        assert result["fills"] == [], f"Expected no fills applied, got {result['fills']}"

        # Long-term lot must remain intact
        lot = conn.execute(
            "SELECT quantity, is_long_term FROM position_lots "
            "WHERE symbol='2330' AND account_id='test-acct'"
        ).fetchone()
        assert lot is not None, "Long-term lot should still exist"
        assert lot["quantity"] == 66
        assert lot["is_long_term"] == 1

        conn.close()
    finally:
        os.unlink(db_path)
