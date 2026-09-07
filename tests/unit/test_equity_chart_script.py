from src.web import server


def test_equity_chart_script_supports_daily_pnl_mixed_chart_and_backtest_fallback():
    script = (server.BASE_DIR / "static" / "js" / "backtest-charts.js").read_text(
        encoding="utf-8"
    )

    assert "daily_pnl" in script
    assert "type: 'bar'" in script
    assert "yPnl" in script
    assert "#e53e3e" in script
    assert "#38a169" in script
    assert "position_value" in script
    assert "cash" in script


def test_dashboard_equity_chart_accessible_label_mentions_daily_pnl():
    template = (server.BASE_DIR / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'aria-label="歷史總權益與每日損益圖"' in template
