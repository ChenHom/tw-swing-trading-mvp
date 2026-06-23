import pytest
import json
import hashlib
from datetime import date, datetime
from src.contracts.models import (
    StrategyApprovalManifest, DailySignalBundle, ExecutionContext, MarketBar, TrendPullbackParams
)
from src.portfolio.db import init_db, get_db_connection
from src.market_data.repository import SqliteMarketBarRepository
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.strategy.canonicalizer import StrategyParameterCanonicalizer
from src.application.execution.engine import TradeExecutionEngine

@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "test_execution.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    
    repo = SqliteMarketBarRepository(conn)
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    
    yield conn, repo, ledger, projection
    conn.close()

def test_trade_execution_engine_e2e(test_setup):
    conn, repo, ledger, projection = test_setup
    
    # 1. Insert daily market bar for 2330 on 2026-06-11 (open = 100.0)
    bar = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1010000, low=990000, close=1000000,
        volume=100, amount=10000,
        source="shioaji", source_timezone="Asia/Taipei",
        is_complete=1, source_fetched_at="now", raw_payload_checksum="chk"
    )
    repo.upsert(bar)
    
    # 2. Deposit cash
    ledger.deposit("acc-main", "run-main", 300000, "TWD", date(2026, 6, 10))
    projection.rebuild_from_ledger("acc-main")
    
    # 3. Create strategy params & compute hash
    params = TrendPullbackParams(order_budget_twd=25000)
    params_hash = StrategyParameterCanonicalizer.compute_hash(params)
    
    # 4. Create active manifest
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": "approval-v1",
        "issuer_id": "manual-research-review",
        "strategy": {
            "strategy_id": "trend_pullback",
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
            "max_order_value": 30000,
            "max_daily_buy_value": 50000,
            "max_open_positions": 2
        },
        "validity": {
            "valid_from": "2026-06-10T00:00:00+08:00",
            "expires_at": "2026-07-10T00:00:00+08:00"
        },
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "manifest-v1",
            "digest": ""
        }
    }
    # compute manifest digest
    canonical_str = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    manifest_dict["integrity"]["digest"] = f"sha256:{digest}"
    manifest = StrategyApprovalManifest(**manifest_dict)
    
    # 5. Create signal bundle: BUY 2330
    bundle_dict = {
        "schema_version": "1.0",
        "bundle_id": "bundle-20260610",
        "run_id": "daily:2026-06-10",
        "approval_id": "approval-v1",
        "strategy": {
            "strategy_id": "trend_pullback",
            "strategy_version": "1.0.0",
            "params_canonicalization": "strategy-params-v1",
            "params_hash": params_hash
        },
        "signal_date": "2026-06-10",
        "target_execution_date": "2026-06-11",
        "market_data_cutoff": "2026-06-10",
        "signals": [
            {
                "signal_id": "bundle-20260610:2330:buy",
                "symbol": "2330",
                "action": "BUY",
                "reference_price": 100.0,
                "reason_code": "ENTRY"
            }
        ]
    }
    bundle = DailySignalBundle(**bundle_dict)
    
    # 6. Setup execution context
    context = ExecutionContext(
        run_id="daily:2026-06-10",
        run_type="DAILY_SIMULATION",
        as_of_date=date(2026, 6, 10),
        execution_date=date(2026, 6, 11),
        account_id="acc-main"
    )
    
    # 7. Initialize TradeExecutionEngine and run
    # Set up config files allowlist and revoked files
    allowlist = ["manual-research-review"]
    revoked = []
    
    engine = TradeExecutionEngine(
        db_conn=conn,
        market_repo=repo,
        projection=projection,
        allowed_issuers=allowlist,
        revoked_approvals=revoked,
        manifests={"trend_pullback": manifest},
        strategy_budgets={"trend_pullback": 25000},
        slippage_bps=10
    )
    
    # Execute the bundle
    result = engine.execute_bundle(context, bundle)
    
    # Assert successful execution
    assert result["status"] == "COMPLETED"
    
    # Verify cash balance, positions, and reconcile
    # Strategy budget 25000 / 100.0 = 250 shares（全數零股，< 1000 股一張）。
    # 零股折損（FakeBroker 3x 滑價，P0-T7）：100.0 x (1 + 30bps) = 100.3
    # Cost = 250 * 100.3 = 25075 TWD; Fee = max(20, round(25075 * 0.001425)) = 36 TWD
    # Final cash = 300,000 - (25,075 + 36) = 274,889
    balance = projection.get_cash_balance("acc-main")
    assert balance == 274889

    lots = projection.get_position_lots("acc-main", "2330")
    assert len(lots) == 1
    assert lots[0]["quantity"] == 250
    assert lots[0]["price"] == 1003000  # 100.3 x 10000（零股折損後）
    
    reconcile_res = projection.reconcile("acc-main")
    assert reconcile_res["status"] == "RECONCILE_OK"
