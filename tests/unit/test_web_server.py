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
    # C3-1 資金總覽卡（空帳戶 → allocation 空、報酬率「—」）
    assert "淨投入本金" in body
    assert "總權益" in body
    assert "總報酬率" in body
    assert "持倉資產配置" in body
    assert "無資產可顯示" in body
    assert "/static/js/chart.umd.min.js" in body


def test_dashboard_allocation_with_position(tmp_path, monkeypatch):
    """有持倉 + 當日行情 → 渲染圓環 canvas 與 JSON 資料區塊。"""
    from datetime import date
    from src.market_data.repository import SqliteMarketBarRepository
    from src.contracts.models import MarketBar

    db = tmp_path / "web_alloc.db"
    init_db(str(db))
    c = get_db_connection(str(db))
    c.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) "
        "VALUES ('simulation-main', 100000, 'TWD', '2026-06-12')"
    )
    c.execute(
        "INSERT INTO position_lots (lot_id, account_id, symbol, quantity, price, acquired_at, "
        "fill_id, created_at, is_long_term, strategy_id) "
        "VALUES ('lot1','simulation-main','2330',1000,1000000,'2026-06-10','f1','2026-06-10',0,'trend_breakout')"
    )
    c.commit()
    SqliteMarketBarRepository(c).upsert(MarketBar(
        symbol="2330", exchange="TWSE", instrument_type="STOCK", trade_date=date(2026, 6, 12),
        open=1100000, high=1100000, low=1100000, close=1100000, volume=1000, amount=1100000,
        source="TEST", source_fetched_at="2026-06-12T00:00:00+08:00", raw_payload_checksum="x",
    ))
    c.close()

    fake_settings = type("S", (), {"trading": type("T", (), {"database_path": str(db)})()})()
    monkeypatch.setattr(server, "AppSettings", lambda: fake_settings)
    r = TestClient(server.app).get("/?view_date=2026-06-12")
    assert r.status_code == 200
    body = r.text
    assert 'id="allocData"' in body
    assert 'id="allocChart"' in body
    assert "2330" in body
    # 持倉表「名稱」欄：代號 2330 應伴隨中文股名（stock_names 對照）
    assert "台積電" in body
    assert "<th>名稱</th>" in body


def test_dashboard_defaults_to_today(client):
    """無 view_date 時，預設日期應為今天（讓日期欄反映當下）。"""
    from datetime import date
    r = client.get("/")  # 不帶日期
    assert r.status_code == 200
    assert f'name="view_date" value="{date.today().isoformat()}"' in r.text


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


def test_backtests_list_empty(client, monkeypatch):
    monkeypatch.setattr("src.application.services.dashboard.BACKTEST_REPORT_DIR", "/nonexistent/xyz")
    r = client.get("/backtests")
    assert r.status_code == 200
    assert "回測結果" in r.text
    assert "尚無回測結果" in r.text


def test_backtests_list_and_detail(client, monkeypatch, tmp_path):
    from datetime import date
    from src.application.reporting.backtest_report import write_backtest_result

    base = tmp_path / "backtest"
    result = {
        "run_id": "bt-aaa11111",
        "account_id": "backtest:bt-aaa11111",
        "equity_curve": [
            {"date": date(2025, 1, 2), "cash": 300000, "position_value": 0, "equity": 300000},
            {"date": date(2025, 1, 3), "cash": 280000, "position_value": 32345, "equity": 312345},
        ],
        "statistics": {
            "initial_cash": 300000,
            "final_equity": 312345,
            "total_pnl": 12345,
            "total_pnl_bps": 411,
            "max_drawdown": 0.0234,
            "win_rate": 0.6,
            "profit_factor": None,
            "avg_profit": 1000.0,
            "avg_loss": 0.0,
            "trade_count": 5,
        },
        "benchmarks": {}, "return_layers": {}, "robustness": {}, "cost_ratio": {},
        "yearly_breakdown": [], "verdict": {"verdict": "INVALID", "diagnostic_result": None, "reasons": []},
    }
    write_backtest_result(
        result, strategy_id="trend_breakout", start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3), initial_cash=300000, base_dir=str(base),
    )
    monkeypatch.setattr("src.application.services.dashboard.BACKTEST_REPORT_DIR", str(base))

    r = client.get("/backtests")
    assert r.status_code == 200
    assert "trend_breakout" in r.text
    assert "trend_breakout_bt-aaa11111.json" in r.text

    r = client.get("/backtests/trend_breakout_bt-aaa11111.json")
    assert r.status_code == 200
    body = r.text
    assert "trend_breakout" in body
    assert 'id="equityChart"' in body
    assert 'id="equityCurveData"' in body
    assert "&#8734;" in body  # profit_factor=None → ∞
    assert "/static/js/backtest-charts.js" in body


def test_backtest_detail_404_on_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.application.services.dashboard.BACKTEST_REPORT_DIR", str(tmp_path / "backtest"))
    r = client.get("/backtests/does_not_exist.json")
    assert r.status_code == 404


def test_backtest_detail_path_traversal_blocked(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.application.services.dashboard.BACKTEST_REPORT_DIR", str(tmp_path / "backtest"))
    r = client.get("/backtests/..%2f..%2f..%2fetc%2fpasswd")
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


def _seed_bundle(conn, *, bundle_id, signal_date, target_date, action="BUY", symbol="2330",
                 strategy_id="trend_breakout"):
    conn.execute(
        """
        INSERT INTO signal_bundles (bundle_id, run_id, approval_id, strategy_id,
            strategy_version, params_hash, signal_date, target_execution_date,
            market_data_cutoff, created_at)
        VALUES (?, 'r1', 'ap1', ?, 'v1', 'h1', ?, ?, ?, ?)
        """,
        (bundle_id, strategy_id, signal_date, target_date, signal_date, signal_date),
    )
    conn.execute(
        """
        INSERT INTO signal_items (item_id, bundle_id, signal_id, symbol, action,
            reference_price, reason_code, created_at)
        VALUES (?, ?, ?, ?, ?, 5000000, 'ENTRY', ?)
        """,
        (f"it-{bundle_id}", bundle_id, f"sig-{bundle_id}", symbol, action, signal_date),
    )
    conn.commit()


def test_next_execution_decoupled_from_view_date(tmp_path):
    """下次執行取最新 signal_date 批次，與 view_date 無關（解耦）。"""
    from src.application.services import dashboard as dash
    from src.portfolio.projection import PortfolioProjection

    db = tmp_path / "nx.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    # 舊批次（6/10 產生、target 6/11）與最新批次（6/12 產生、target 6/15）。
    _seed_bundle(conn, bundle_id="b-old", signal_date="2026-06-10", target_date="2026-06-11", symbol="1111")
    _seed_bundle(conn, bundle_id="b-new", signal_date="2026-06-12", target_date="2026-06-15", symbol="2330")

    # 即使檢視一個與兩批都不同的日期，下次執行仍應只含最新批次（6/12）。
    data = dash.build_dashboard(conn, PortfolioProjection(conn), "acc", "2026-06-15")
    symbols = {s["symbol"] for s in data["next_execution"]}
    assert symbols == {"2330"}
    assert data["next_execution"][0]["target_date"] == "2026-06-15"
    conn.close()


def test_reconcile_summary_ok_and_mismatch(tmp_path):
    """對帳摘要：一致回 ok+說明；現金不符回中文差異明細。"""
    from src.application.services import dashboard as dash
    from src.portfolio.projection import PortfolioProjection

    db = tmp_path / "rec.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    # 一致帳戶：無流水、餘額 0。
    conn.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('ok-acc', 0, 'TWD', '2026-06-12')"
    )
    # 不一致帳戶：餘額快照 100 但無對應 ledger 流水（合計 0）。
    conn.execute(
        "INSERT INTO cash_balances (account_id, balance, currency, updated_at) VALUES ('bad-acc', 100, 'TWD', '2026-06-12')"
    )
    conn.commit()
    proj = PortfolioProjection(conn)

    ok = dash.build_dashboard(conn, proj, "ok-acc", "2026-06-12")["reconcile"]
    assert ok["ok"] is True and ok["code"] == "RECONCILE_OK"

    bad = dash.build_dashboard(conn, proj, "bad-acc", "2026-06-12")["reconcile"]
    assert bad["ok"] is False and bad["code"] == "CASH_BALANCE_MISMATCH"
    assert "餘額快照" in bad["detail_zh"] and "100" in bad["detail_zh"]
    conn.close()


def test_event_label_localized(tmp_path):
    """執行事件帶中文 event_label；未知代碼退回原碼。"""
    from src.application.services import dashboard as dash
    from src.portfolio.projection import PortfolioProjection

    db = tmp_path / "ev.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    for eid, etype in (("e1", "APPROVAL_INVALID"), ("e2", "SOME_UNKNOWN_CODE")):
        conn.execute(
            """
            INSERT INTO execution_events (event_id, run_id, account_id, event_type,
                strategy_id, symbol, detail, occurred_at, created_at)
            VALUES (?, 'r1', 'ev-acc', ?, 'trend_breakout', '2330', 'bundle x', '2026-06-12', '2026-06-12')
            """,
            (eid, etype),
        )
    conn.commit()
    data = dash.build_dashboard(conn, PortfolioProjection(conn), "ev-acc", "2026-06-12")
    labels = {e["event_type"]: e["event_label"] for e in data["events"]}
    assert labels["APPROVAL_INVALID"] == "授權無效（過期/模式不符）"
    assert labels["SOME_UNKNOWN_CODE"] == "SOME_UNKNOWN_CODE"  # 未知退回原碼
    conn.close()


def test_corporate_actions_surfaced(tmp_path):
    """登錄近期除息事件 → build_dashboard 回傳並標未套用。"""
    from src.application.services import dashboard as dash
    from src.portfolio.projection import PortfolioProjection

    db = tmp_path / "ca.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    # 登錄一筆與檢視日相近的現金股利事件（未套用）
    conn.execute(
        """
        INSERT INTO corporate_actions
        (action_id, symbol, action_type, ex_date, cash_per_share, source, created_at)
        VALUES ('ca1', '00994A', 'CASH_DIVIDEND', '2026-06-18', 15000, 'MANUAL', '2026-06-15')
        """
    )
    conn.commit()
    data = dash.build_dashboard(conn, PortfolioProjection(conn), "ca-acc", "2026-06-15")
    cas = data["corporate_actions"]
    assert len(cas) == 1
    assert cas[0]["symbol"] == "00994A"
    assert cas[0]["applied"] is False
    assert "1.50" in cas[0]["detail_zh"]  # 15000 / 10000 = 1.50 元/股
    conn.close()
