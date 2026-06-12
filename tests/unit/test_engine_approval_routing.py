"""引擎授權路由：BUY 按策略路由驗證、SELL 永不受授權閘門阻擋（§2.7）。"""
import json
import hashlib
import pytest
from datetime import date
from unittest.mock import MagicMock
from src.contracts.models import (
    MarketBar, StrategyApprovalManifest, StrategyInfo, DailySignalBundle, SignalItem, ExecutionContext
)
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.application.execution.engine import TradeExecutionEngine
from src.trading.allocator import GlobalLimits


def make_manifest(strategy_id, approval_id, params_hash="sha256:h", expires="2099-12-31T23:59:59+08:00"):
    manifest_dict = {
        "schema_version": "1.0",
        "approval_id": approval_id,
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
            "expires_at": expires
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


def make_bundle(strategy_id, signals, approval_id="app-x", params_hash="sha256:h", exit_bundle=False):
    suffix = "-exit" if exit_bundle else ""
    return DailySignalBundle(
        schema_version="1.0",
        bundle_id=f"bundle-20260610-{strategy_id}{suffix}",
        run_id="run-1",
        approval_id=approval_id,
        strategy=StrategyInfo(
            strategy_id=strategy_id, strategy_version="1.0.0",
            params_canonicalization="strategy-params-v1", params_hash=params_hash
        ),
        signal_date=date(2026, 6, 10),
        target_execution_date=date(2026, 6, 11),
        market_data_cutoff=date(2026, 6, 10),
        signals=signals
    )


@pytest.fixture
def setup(tmp_path):
    db_file = tmp_path / "test_routing.db"
    init_db(str(db_file))
    conn = get_db_connection(str(db_file))
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    ledger.deposit("acc-1", "run-1", 500000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger("acc-1")

    mock_repo = MagicMock()
    mock_repo.find.return_value = MarketBar(
        symbol="2330", exchange="TSE", instrument_type="STOCK",
        trade_date=date(2026, 6, 11),
        open=1000000, high=1010000, low=990000, close=1000000,
        volume=1000, amount=10000,
        source="test", source_fetched_at="now", raw_payload_checksum="chk"
    )
    yield conn, projection, mock_repo
    conn.close()


CONTEXT = ExecutionContext(
    run_id="run-1", run_type="DAILY_SIMULATION",
    as_of_date=date(2026, 6, 10), execution_date=date(2026, 6, 11),
    account_id="acc-1"
)


def _engine(conn, projection, repo, manifests):
    return TradeExecutionEngine(
        db_conn=conn, market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifests=manifests,
        strategy_budgets={"trend_breakout": 20000, "pullback_rebound": 20000},
        global_limits=GlobalLimits(),
        pipeline_order=["trend_breakout", "pullback_rebound"]
    )


def test_buy_blocked_without_manifest_and_event_recorded(setup):
    conn, projection, repo = setup
    bundle = make_bundle("trend_breakout", [
        SignalItem(signal_id="s1", symbol="2330", action="BUY", reference_price=100.0,
                   reason_code="ENTRY", strategy_id="trend_breakout")
    ])
    engine = _engine(conn, projection, repo, manifests={})
    result = engine.execute_bundles(CONTEXT, [bundle])

    assert result["status"] == "COMPLETED"
    assert result["fills"] == []
    assert "APPROVAL_NOT_FOUND" in result["signal_results"]["s1"]

    cursor = conn.cursor()
    cursor.execute("SELECT event_type, strategy_id FROM execution_events")
    events = cursor.fetchall()
    assert any(e["event_type"] == "APPROVAL_NOT_FOUND" and e["strategy_id"] == "trend_breakout" for e in events)


def test_sell_executes_even_with_expired_manifest(setup):
    """授權過期期間，risk_exit 的 SELL 必須照常執行（停損不可失效）。"""
    conn, projection, repo = setup
    projection.apply_fill_transaction({
        "fill_id": "f-buy", "account_id": "acc-1", "run_id": "run-0",
        "order_id": "o1", "execution_key": "k1", "symbol": "2330",
        "side": "BUY", "quantity": 100, "price": 1100000,
        "filled_at": "2026-06-08T09:00:00+08:00", "strategy_id": "trend_breakout"
    })

    expired = make_manifest("trend_breakout", "app-expired", expires="2026-06-01T00:00:00+08:00")
    exit_bundle = make_bundle("trend_breakout", [
        SignalItem(signal_id="s-sell", symbol="2330", action="SELL", reference_price=100.0,
                   reason_code="FIXED_STOP_EXIT", strategy_id="trend_breakout", signal_source="RISK_EXIT")
    ], approval_id="risk-exit", exit_bundle=True)

    engine = _engine(conn, projection, repo, manifests={"trend_breakout": expired})
    result = engine.execute_bundles(CONTEXT, [exit_bundle])

    assert result["status"] == "COMPLETED"
    assert len(result["fills"]) == 1
    assert result["fills"][0]["side"] == "SELL"

    cursor = conn.cursor()
    cursor.execute("SELECT SUM(quantity) as q FROM position_lots WHERE symbol='2330'")
    assert (cursor.fetchone()["q"] or 0) == 0


def test_buy_blocked_by_params_hash_mismatch_but_other_strategy_unaffected(setup):
    conn, projection, repo = setup
    m_breakout = make_manifest("trend_breakout", "app-bk", params_hash="sha256:other")  # hash 不符
    m_pullback = make_manifest("pullback_rebound", "app-pb", params_hash="sha256:h")

    b1 = make_bundle("trend_breakout", [
        SignalItem(signal_id="s1", symbol="2330", action="BUY", reference_price=100.0,
                   reason_code="ENTRY", strategy_id="trend_breakout")
    ], approval_id="app-bk")
    b2 = make_bundle("pullback_rebound", [
        SignalItem(signal_id="s2", symbol="2330", action="BUY", reference_price=100.0,
                   reason_code="ENTRY", strategy_id="pullback_rebound")
    ], approval_id="app-pb")

    engine = _engine(conn, projection, repo, manifests={"trend_breakout": m_breakout, "pullback_rebound": m_pullback})
    result = engine.execute_bundles(CONTEXT, [b1, b2])

    assert "PARAMS_HASH_MISMATCH" in result["signal_results"]["s1"]
    assert isinstance(result["signal_results"]["s2"], list)  # pullback 正常成交
    assert len(result["fills"]) >= 1
