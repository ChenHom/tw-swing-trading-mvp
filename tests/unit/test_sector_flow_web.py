"""族群資金頁籤：API 與頁籤結構。"""
import json

from fastapi.testclient import TestClient

from src.web import server
from tests.unit.test_completed_trade_web import _render_completed_trade_dashboard


def test_sector_tab_sits_right_of_capital_tab(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path)

    assert body.index('data-tab="capital"') < body.index('data-tab="sector"') < body.index('data-tab="trades"')
    assert "switchTab('sector', this)" in body
    assert '<span class="tab-text-desktop">族群資金</span>' in body
    assert '<span class="tab-text-mobile">族群</span>' in body
    assert 'id="tab-sector"' in body
    assert "/static/js/sector_flow.js" in body


def test_sector_flow_api_returns_file_as_is(tmp_path, monkeypatch):
    path = tmp_path / "dashboard.json"
    payload = {"dates": ["2026-10-02"], "rows": [], "status": "ok", "windows": [20]}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(server, "SECTOR_FLOW_PATH", path)

    res = TestClient(server.app).get("/api/sector-flow")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == payload


def test_sector_flow_api_404_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SECTOR_FLOW_PATH", tmp_path / "missing.json")

    res = TestClient(server.app).get("/api/sector-flow")

    assert res.status_code == 404
    assert "尚無族群資金資料" in res.json()["error"]


def test_large_holder_section_sits_between_rank_and_detail(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path)

    assert body.index('<table id="sf-rank">') < body.index('id="sf-lh"') < body.index('id="sf-detail"')
    assert "大戶持股（週）" in body
