"""現金閒置根治(2026-07)：per-account 覆寫每日新建倉上限與動態部位大小比例。
未列名的帳號（如國泰）沿用全域值，行為零影響。"""
from src.cli import common


def test_build_global_limits_applies_per_account_override():
    settings = common.get_settings()
    settings.trading.pipeline.max_new_positions_per_day_overrides = {"simulation-main": 6}

    limits_sim = common.build_global_limits(settings, "simulation-main")
    assert limits_sim.max_new_positions_per_day == 6

    # 未列入 override 的帳號 → 回退全域值
    limits_real = common.build_global_limits(settings, "國泰")
    assert limits_real.max_new_positions_per_day == settings.trading.global_limits.max_new_positions_per_day

    # 不給 account_id → 同樣回退全域值（向下相容既有呼叫端）
    limits_none = common.build_global_limits(settings)
    assert limits_none.max_new_positions_per_day == settings.trading.global_limits.max_new_positions_per_day


def test_get_dynamic_order_sizing_fraction():
    settings = common.get_settings()
    settings.trading.pipeline.dynamic_order_sizing_accounts = {"simulation-main": 0.1}

    assert common.get_dynamic_order_sizing_fraction(settings, "simulation-main") == 0.1
    assert common.get_dynamic_order_sizing_fraction(settings, "國泰") is None
    assert common.get_dynamic_order_sizing_fraction(settings, None) is None
