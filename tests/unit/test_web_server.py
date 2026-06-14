"""唯讀 Web 儀表板冒煙測試（FastAPI TestClient）。"""
import os
import pytest

# 測試環境用空 root_path，避免子路徑前綴干擾斷言；須在 import server 前設定。
os.environ["TRADING_WEB_ROOT_PATH"] = ""

from fastapi.testclient import TestClient

from src.portfolio.db import init_db, get_db_connection
from src.web import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "web.db"
    init_db(str(db))
    # 種一個有現金的帳戶，讓帳戶下拉與儀表板有資料
    c = get_db_connection(str(db))
    # 一致帳戶：無 cash_ledger ⇒ ledger 總額 0，balance 亦 0 ⇒ reconcile 通過
    c.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('simulation-main', 0, 'TWD', '2026-06-12')"
    )
    c.commit()
    c.close()

    fake_settings = type("S", (), {"trading": type("T", (), {"database_path": str(db)})()})()
    monkeypatch.setattr(server, "AppSettings", lambda: fake_settings)
    return TestClient(server.app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_dashboard_renders(client):
    r = client.get("/?view_date=2026-06-12")
    assert r.status_code == 200
    body = r.text
    assert "tw-day-trading" in body
    assert "可用現金" in body
    assert "RISK_EXIT" in body
    assert "simulation-main" in body
    # 空帳戶對帳應顯示通過
    assert "通過" in body


def test_dashboard_defaults_to_latest_run_date(client, monkeypatch, tmp_path):
    """無 view_date 時，預設日期應落在最近一個有 daily_run 的日期，而非今天。"""
    # 取得 client 用的 DB 路徑並種一筆 06-12 的 run
    db_path = server.AppSettings().trading.database_path
    from src.portfolio.db import get_db_connection
    c = get_db_connection(db_path)
    c.execute(
        """
        INSERT INTO daily_runs (run_id, run_date, account_id, strategy_id, status,
            market_sync_status, execution_status, signal_generation_status,
            report_status, started_at, completed_at, last_error_code)
        VALUES ('sim-x','2026-06-12','simulation-main','MULTI','COMPLETED',
            'COMPLETED','COMPLETED','COMPLETED','COMPLETED','2026-06-12','2026-06-12',NULL)
        """
    )
    c.commit(); c.close()

    r = client.get("/")  # 不帶日期
    assert r.status_code == 200
    assert 'name="view_date" value="2026-06-12"' in r.text


def test_reports_list_empty(client, monkeypatch):
    # 指向一個不存在的報告目錄 → 空清單
    monkeypatch.setattr("src.application.services.dashboard.REPORT_DIR", "/nonexistent/xyz")
    r = client.get("/reports")
    assert r.status_code == 200
    assert "歷史每日報告" in r.text


def test_report_detail_404_on_missing(client):
    r = client.get("/reports/does_not_exist.txt")
    assert r.status_code == 404


def test_report_detail_path_traversal_blocked(client):
    # 嘗試目錄穿越應被擋（read_report 只取檔名）
    r = client.get("/reports/..%2f..%2f..%2fetc%2fpasswd")
    assert r.status_code == 404


def test_dashboard_monitored_requires_exit_block(tmp_path):
    """監控判定須與 risk_exit 一致：歸入無 exit 區塊的策略不得計入監控。"""
    from src.application.services import dashboard as dash
    from src.portfolio.projection import PortfolioProjection

    db = tmp_path / "mon.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    projection = PortfolioProjection(conn)
    # 兩筆策略持倉：一支具 exit 區塊（trend_breakout）、一支無（fake_noexit）。
    for sid in ("trend_breakout", "fake_noexit"):
        projection.apply_fill_transaction({
            "fill_id": f"f-{sid}", "account_id": "acc-mon", "run_id": "r1",
            "order_id": f"o-{sid}", "execution_key": f"k-{sid}",
            "symbol": "2317" if sid == "trend_breakout" else "2330",
            "side": "BUY", "quantity": 100, "price": 500000,
            "filled_at": "2026-06-12T09:00:00+08:00", "is_long_term": 0,
            "source": "MANUAL_IMPORT", "strategy_id": sid,
        })

    # 只有 trend_breakout 屬具 exit 區塊集合。
    data = dash.build_dashboard(conn, projection, "acc-mon", "2026-06-12",
                               exit_strategy_ids={"trend_breakout"})
    by_sid = {p["strategy_id"]: p for p in data["positions"]}
    assert by_sid["trend_breakout"]["monitored"] is True
    assert by_sid["fake_noexit"]["monitored"] is False
    assert data["monitored_count"] == 1
    conn.close()
