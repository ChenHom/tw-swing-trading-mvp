from datetime import date

from src.application.services import dashboard
from src.portfolio.db import get_db_connection, init_db
from src.portfolio.ledger import PortfolioLedger
from src.portfolio.projection import PortfolioProjection
from src.web import server


def test_dashboard_route_accepts_trade_date_as_iso_date():
    parameters = server.app.openapi()["paths"]["/"]["get"]["parameters"]
    trade_date = next((item for item in parameters if item["name"] == "trade_date"), None)

    assert trade_date is not None
    assert trade_date["schema"]["anyOf"][0] == {"type": "string", "format": "date"}


def test_dashboard_attaches_trade_history_for_independent_date(tmp_path):
    db = tmp_path / "dashboard-history.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    PortfolioLedger(conn).deposit("a", "run-1", 100000, "TWD", date(2026, 6, 1))
    projection = PortfolioProjection(conn)
    projection.rebuild_from_ledger("a")

    data = dashboard.build_dashboard(
        conn,
        projection,
        "a",
        date(2026, 6, 20),
        trade_date=date(2026, 6, 10),
    )

    assert data["date"] == "2026-06-20"
    assert data["completed_trade_history"]["selected_date"] == "2026-06-10"
    assert data["completed_trade_history"]["trades"] == []
    conn.close()


def _render_completed_trade_dashboard(tmp_path, unknown_net=False):
    db = tmp_path / "completed-trade-web.db"
    init_db(str(db))
    conn = get_db_connection(str(db))
    ledger = PortfolioLedger(conn)
    ledger.deposit("simulation-main", "run-1", 100000, "TWD", date(2026, 6, 1))
    projection = PortfolioProjection(conn)
    projection.rebuild_from_ledger("simulation-main")
    projection.apply_fill_transaction({
        "fill_id": "web-buy", "account_id": "simulation-main", "run_id": "run-1",
        "order_id": "order-buy", "execution_key": "key-buy", "symbol": "2330",
        "side": "BUY", "quantity": 100, "price": 1000000,
        "filled_at": "2026-06-01T09:00:00+08:00", "strategy_id": "trend_breakout",
    })
    projection.apply_fill_transaction({
        "fill_id": "web-sell", "account_id": "simulation-main", "run_id": "run-1",
        "order_id": "order-sell", "execution_key": "key-sell", "symbol": "2330",
        "side": "SELL", "quantity": 100, "price": 1100000,
        "filled_at": "2026-06-10T09:00:00+08:00", "strategy_id": "trend_breakout",
    })
    if unknown_net:
        conn.execute("UPDATE fifo_matches SET net_realized_pnl = NULL")
        conn.commit()
    data = dashboard.build_dashboard(
        conn,
        projection,
        "simulation-main",
        date(2026, 6, 20),
        trade_date=date(2026, 6, 10),
    )
    cap = dashboard.build_capital_overview(
        conn, projection, "simulation-main", date(2026, 6, 20), market_repo=None
    )
    body = server.templates.get_template("dashboard.html").render(
        d=data,
        cap=cap,
        accounts=["simulation-main"],
        view_date="2026-06-20",
        today="2026-06-20",
    )
    conn.close()
    return body


def test_completed_trade_card_renders_in_second_standalone_tab(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path)

    assert '<details name="completed-trade-row"' in body
    assert "web-buy" in body and "web-sell" in body
    assert body.count('class="tab-btn') == 7
    assert (
        body.index("資金總覽")
        < body.index("交易紀錄")
        < body.index("持倉部位")
        < body.index("策略別損益")
    )
    capital_start = body.index('id="tab-capital"')
    trades_start = body.index('id="tab-trades"')
    positions_start = body.index('id="tab-positions"')
    assert "completed-trades-card" not in body[capital_start:trades_start]
    assert "completed-trades-card" in body[trades_start:positions_start]


def test_completed_trade_card_preserves_account_and_view_date_in_controls(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path)

    assert 'name="account" value="simulation-main"' in body
    assert 'name="view_date" value="2026-06-20"' in body
    assert 'name="trade_date"' in body
    assert 'action="/trading/#tab-trades"' in body
    assert "window.location.hash" in body
    assert "history.replaceState" in body
    template = (server.BASE_DIR / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "trade_date={{ h.older_date }}#tab-trades" in template
    assert "trade_date={{ h.newer_date }}#tab-trades" in template


def test_completed_trade_unknown_net_is_not_rendered_as_gross(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path, unknown_net=True)

    assert "舊資料無法回算完整費稅" in body
    assert "部分交易淨損益不可計算" in body


def test_completed_trade_styles_keep_taiwan_pnl_colors_and_accessible_motion():
    css = (server.BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")

    assert ".completed-trades-card .pos { color: #e53e3e; }" in css
    assert ".completed-trades-card .neg { color: #38a169; }" in css
    assert ".completed-trades-disclosure[open] .completed-trades-toggle" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_site_is_branded_as_swing_trading(tmp_path):
    body = _render_completed_trade_dashboard(tmp_path)

    assert "<title>台股波段交易儀表板</title>" in body
    assert '<span class="brand-text-desktop">台股波段交易</span>' in body
    assert "tw-day-trading" not in body
