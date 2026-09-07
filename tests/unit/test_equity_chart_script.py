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
    assert "每日損益（紅賺／綠賠）" in script
    assert "position_value" in script
    assert "cash" in script

    mixed_branch = script.split("if (hasDailyPnl) {", 1)[1].split("} else {", 1)[0]
    assert "type: 'line', label: '現金'" in mixed_branch
    assert "type: 'line', label: '持倉市值'" in mixed_branch
    assert "data: rows.map(function (r) { return r.cash; })" in mixed_branch
    assert "data: rows.map(function (r) { return r.position_value; })" in mixed_branch
    assert mixed_branch.count("yAxisID: 'y'") == 3


def test_dashboard_equity_chart_accessible_label_mentions_daily_pnl():
    template = (server.BASE_DIR / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'aria-label="歷史總權益與每日損益圖"' in template
    assert "歷史權益與每日損益" in template
