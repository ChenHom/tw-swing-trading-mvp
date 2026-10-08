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
