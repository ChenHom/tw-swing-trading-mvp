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
