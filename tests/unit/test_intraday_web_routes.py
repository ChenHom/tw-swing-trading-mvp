"""Integration smoke for newly added intraday routes; no trading DB access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.application.services.intraday_dashboard import write_snapshot, make_snapshot
from src.web import server

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 9, 15, tzinfo=TZ)


def test_routes_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("INTRADAY_DASHBOARD_ENABLED", raising=False)
    monkeypatch.setattr(server, "INTRADAY_SNAPSHOT_PATH", tmp_path/"not-exists.json")
    with TestClient(server.app) as client:
        page = client.get("/intraday")
        assert page.status_code == 200
        assert "DISABLED" in page.text
        assert "不是買進或賣出訊號" in page.text
        assert "place_order" not in page.text
        assert client.get("/api/intraday/status").json()["status"] == "DISABLED"
        assert client.get("/api/intraday/symbols").json()["symbols"] == []


def test_api_rejects_invalid_symbols_and_lot_type(monkeypatch, tmp_path):
    monkeypatch.setenv("INTRADAY_DASHBOARD_ENABLED", "1")
    monkeypatch.setattr(server, "INTRADAY_SNAPSHOT_PATH", tmp_path/"not-exists.json")
    with TestClient(server.app) as client:
        assert client.get("/api/intraday/2327/snapshot?lot_type=INVALID").status_code == 422
        assert client.get("/api/intraday/../../../x/snapshot").status_code in (400, 404)
        assert client.get("/api/intraday/2327/snapshot?lot_type=BOARD").status_code == 404
        assert client.get("/healthz").status_code == 200


def test_snapshot_json_api_does_not_require_broker_or_database(monkeypatch, tmp_path):
    monkeypatch.setenv("INTRADAY_DASHBOARD_ENABLED", "1")
    path = tmp_path/"dashboard.json"
    monkeypatch.setattr(server, "INTRADAY_SNAPSHOT_PATH", path)
    now = datetime.now(timezone.utc)
    snapshot = make_snapshot(
        collector_health={"state": "CLOSED"},
        generated_at=now.isoformat(),
    )
    write_snapshot(path, snapshot)
    with TestClient(server.app) as client:
        assert client.get("/api/intraday/status").json()["status"] == "CLOSED"
        assert client.get("/intraday").status_code == 200
        assert client.get("/api/intraday/symbols").json()["symbols"] == []


def test_existing_reports_page_and_navigation_still_render_without_broker(monkeypatch):
    """Unskipped legacy-page smoke: PR4 cannot break the existing base template."""
    monkeypatch.setattr(server.dash, "list_reports", lambda: [])
    with TestClient(server.app) as client:
        page = client.get("/reports")
        assert page.status_code == 200
        assert "盤中觀察" in page.text
        assert "/trading/intraday" in page.text
        assert "歷史" in page.text
        assert client.get("/healthz").status_code == 200


def test_historical_quote_requires_second_opt_in_and_stays_reference_only(monkeypatch, tmp_path):
    from tests.unit.test_intraday_dashboard import tick, book
    from datetime import datetime, timezone

    monkeypatch.setenv("INTRADAY_DASHBOARD_ENABLED", "1")
    monkeypatch.delenv("INTRADAY_SHOW_HISTORICAL", raising=False)
    path = tmp_path / "preview.json"
    monkeypatch.setattr(server, "INTRADAY_SNAPSHOT_PATH", path)
    health = {
        "state": "CLOSED", "session_id": "snapshot-session",
        "collection_status": "RAW_CAPTURED",
        "transport_verification_status": "UNVERIFIED",
        "data_quality_status": "UNVERIFIED",
        "market_completeness_status": "UNVERIFIED",
        "connection_verified": False, "trading_session_verified": False,
    }
    write_snapshot(path, make_snapshot(
        ticks=[tick()], books=[book()], collector_health=health,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ))
    with TestClient(server.app) as client:
        hidden = client.get("/api/intraday/2327/snapshot?lot_type=BOARD")
        assert hidden.status_code == 200
        assert hidden.json()["symbol"]["tick"] is None
        assert hidden.json()["symbol"]["reference_tick"] is None
        monkeypatch.setenv("INTRADAY_SHOW_HISTORICAL", "1")
        reference = client.get("/api/intraday/2327/snapshot?lot_type=BOARD")
        assert reference.status_code == 200
        row = reference.json()["symbol"]
        assert row["tick"] is None and row["book"] is None
        assert row["reference_tick"]["price_x10000"] == 6375000
        assert row["reference_book"]["bid_volumes_shares"][0] == 3000
        assert row["reference_label"] == "HISTORICAL_UNVERIFIED_NOT_LIVE"
        page = client.get("/intraday")
        assert page.status_code == 200
        assert "歷史參考價" in page.text
        assert "不是買進或賣出訊號" in page.text
        assert client.get("/api/intraday/status").json()["display_mode"] == "HISTORICAL_REFERENCE"
