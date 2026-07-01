"""放開持倉數上限（per-account）：unlimited_open_positions=True 時 allocator 的
per-strategy + global max_open_positions 閘解除，且不得改壞 manifest（integrity digest 須照過）。
其餘限額（每日新建倉、每日買入額度）不受影響。"""
import json
import hashlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.contracts.models import (
    MarketBar, StrategyApprovalManifest, StrategyInfo, DailySignalBundle,
    SignalItem, ExecutionContext,
)
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.application.execution.engine import TradeExecutionEngine
from src.trading.allocator import GlobalLimits
from src.cli.common import is_unlimited_positions_account


def _manifest(strategy_id, max_pos):
    d = {
        "schema_version": "1.0", "approval_id": f"app-{strategy_id}", "issuer_id": "manual-research-review",
        "strategy": {"strategy_id": strategy_id, "strategy_version": "1.0.0",
                     "params_canonicalization": "strategy-params-v1", "params_hash": "sha256:h"},
        "permissions": {"execution_modes": ["simulation"], "risk_increasing_actions": ["open_long"]},
        "limits": {"currency": "TWD", "max_order_value": 50000,
                   "max_daily_buy_value": 5_000_000, "max_open_positions": max_pos},
        "validity": {"valid_from": "2026-01-01T00:00:00+08:00", "expires_at": "2099-12-31T23:59:59+08:00"},
        "integrity": {"algorithm": "sha256", "canonicalization": "manifest-v1", "digest": ""},
    }
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
    d["integrity"]["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return StrategyApprovalManifest(**d)


def _bundle(strategy_id, n):
    signals = [SignalItem(signal_id=f"b{i}", symbol=f"235{i:02d}", action="BUY",
                          reference_price=10.0, reason_code="ENTRY", strategy_id=strategy_id)
               for i in range(n)]
    return DailySignalBundle(
        schema_version="1.0", bundle_id=f"bundle-{strategy_id}", run_id="run-1",
        approval_id=f"app-{strategy_id}",
        strategy=StrategyInfo(strategy_id=strategy_id, strategy_version="1.0.0",
                              params_canonicalization="strategy-params-v1", params_hash="sha256:h"),
        signal_date=date(2026, 6, 10), target_execution_date=date(2026, 6, 11),
        market_data_cutoff=date(2026, 6, 10), signals=signals,
    )


def _setup(tmp_path):
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    conn = get_db_connection(db_path)
    ledger = PortfolioLedger(conn)
    projection = PortfolioProjection(conn)
    ledger.deposit("acc-1", "run-1", 5_000_000, "TWD", date(2026, 6, 1))
    projection.rebuild_from_ledger("acc-1")
    repo = MagicMock()
    repo.find.return_value = MarketBar(
        symbol="x", exchange="TSE", instrument_type="STOCK", trade_date=date(2026, 6, 11),
        open=100000, high=101000, low=99000, close=100000, volume=1000, amount=10000,
        source="test", source_fetched_at="now", raw_payload_checksum="chk")
    return conn, projection, repo


_CONTEXT = ExecutionContext(run_id="run-1", run_type="DAILY_SIMULATION",
                            as_of_date=date(2026, 6, 10), execution_date=date(2026, 6, 11),
                            account_id="acc-1")


def _engine(conn, projection, repo, manifests, unlimited):
    # max_new_positions_per_day 設高以隔離「持倉數」閘（本測試專測 max_open_positions）
    return TradeExecutionEngine(
        db_conn=conn, market_repo=repo, projection=projection,
        allowed_issuers=["manual-research-review"], revoked_approvals=[],
        manifests=manifests, strategy_budgets={"trend_breakout": 20000},
        global_limits=GlobalLimits(max_open_positions=8, max_daily_buy_value=5_000_000,
                                   max_new_positions_per_day=100),
        pipeline_order=["trend_breakout"], unlimited_open_positions=unlimited)


def _planned_count(signal_results):
    return sum(1 for v in signal_results.values() if isinstance(v, list) and v)


def test_normal_caps_at_manifest_open_positions(tmp_path):
    conn, projection, repo = _setup(tmp_path)
    manifests = {"trend_breakout": _manifest("trend_breakout", max_pos=5)}
    engine = _engine(conn, projection, repo, manifests, unlimited=False)
    _, _, results, _ = engine.plan_bundles(_CONTEXT, [_bundle("trend_breakout", 12)])
    assert _planned_count(results) == 5  # 卡在 per-strategy max_open_positions=5
    assert any(isinstance(v, str) and "MAX_OPEN_POSITIONS_EXCEEDED" in v for v in results.values())
    conn.close()


def test_unlimited_lifts_gate_and_preserves_manifest(tmp_path):
    conn, projection, repo = _setup(tmp_path)
    manifests = {"trend_breakout": _manifest("trend_breakout", max_pos=5)}
    engine = _engine(conn, projection, repo, manifests, unlimited=True)
    _, _, results, _ = engine.plan_bundles(_CONTEXT, [_bundle("trend_breakout", 12)])

    # 12 檔全開 → 持倉數閘已解除；同時證明 manifest integrity 未壞：
    # 若 model_copy 誤改了 manifest，buy gate 會 INTEGRITY_INVALID 擋光（planned=0）。
    assert _planned_count(results) == 12
    assert not any(isinstance(v, str) and "MAX_OPEN_POSITIONS" in v for v in results.values())
    # manifest 原物件的 limits 未被就地改動
    assert manifests["trend_breakout"].limits.max_open_positions == 5
    conn.close()


def test_is_unlimited_positions_account_helper():
    settings = SimpleNamespace(trading=SimpleNamespace(
        pipeline=SimpleNamespace(unlimited_positions_accounts=["simulation-main"])))
    assert is_unlimited_positions_account(settings, "simulation-main") is True
    assert is_unlimited_positions_account(settings, "國泰") is False
