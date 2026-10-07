"""build_pipeline 的 per-account 進場策略 override（R-T4b Track 1）。

PIT 裁決後治理用法：REJECTED 策略從真實帳號（國泰）退役、僅留影子觀察；
RESEARCH_PASS 的 trend_breakout 兩邊都跑。SELL/risk_exit 不受 override 影響（exit
是全策略載入，既有持倉照常出場）。"""
from src.cli import common


def _ids(specs):
    return [s.definition.strategy_id for s in specs]


def test_account_override_selects_subset():
    settings = common.get_settings()
    settings.trading.pipeline.account_overrides = {"國泰": ["trend_breakout"]}

    # 有 override 的帳號 → 只跑 override 清單
    entry_real, exit_real = common.build_pipeline(settings, ["2330"], "國泰")
    assert _ids(entry_real) == ["trend_breakout"]
    # exit_definitions 不受 override 影響（既有持倉仍須出場）
    assert "pullback_rebound" in exit_real

    # 未列入 override 的帳號 → 回退全域 entry_strategies
    entry_sim, _ = common.build_pipeline(settings, ["2330"], "simulation-main")
    assert _ids(entry_sim) == settings.trading.pipeline.entry_strategies

    # 不給 account_id → 同樣回退全域
    entry_none, _ = common.build_pipeline(settings, ["2330"])
    assert _ids(entry_none) == settings.trading.pipeline.entry_strategies


def test_account_override_empty_list_retires_all_entries():
    """帳號進場策略全數退役：空清單不得回退到全域 entry_strategies；exit 照常全載入。"""
    settings = common.get_settings()
    settings.trading.pipeline.account_overrides = {"國泰": []}
    entry_real, exit_real = common.build_pipeline(settings, ["2330"], "國泰")
    assert entry_real == []
    assert {"trend_breakout", "pullback_rebound"} <= set(exit_real)


def test_real_config_retires_pullback_entries_for_every_account():
    # pullback_rebound 已 PIT REJECTED，前向觀察也確認（2026-10-07）：真實設定下任何帳號都不得再產生新進場，
    # 但出場定義仍要載入，既有 pullback 持倉才能照常由 risk_exit 出場。
    settings = common.get_settings()
    for account in ("國泰", "simulation-main"):
        entry, exits = common.build_pipeline(settings, ["2330"], account)
        assert "pullback_rebound" not in _ids(entry)
        assert "pullback_rebound" in exits


def test_real_config_cathay_has_no_entries_and_sim_keeps_trend_breakout():
    # 2026-10-07：trend_breakout 現行程式碼下 REJECTED → 國泰全數退役；simulation-main 留 trend_breakout 前向觀察。
    settings = common.get_settings()
    cathay, exits = common.build_pipeline(settings, ["2330"], "國泰")
    assert cathay == []
    assert {"trend_breakout", "pullback_rebound"} <= set(exits)
    sim, _ = common.build_pipeline(settings, ["2330"], "simulation-main")
    assert _ids(sim) == ["trend_breakout"]
