"""P2-T4：參數高原/鄰近穩健性掃描——用合成 landscape 斷言（非真的跑 backtest）。"""
from src.application.runners.parameter_plateau import parameter_plateau_scan


def test_plateau_when_base_and_all_neighbors_pass():
    # lookback=20 為峰值，但 18/19/21/22 鄰近一片也夠高 → 真高原
    landscape = {18: 0.8, 19: 0.85, 20: 0.9, 21: 0.85, 22: 0.8}
    grid = {"lookback": [16, 18, 19, 20, 21, 22, 24]}
    base_params = {"lookback": 20}

    result = parameter_plateau_scan(
        base_params, grid, evaluate_fn=lambda p: landscape[p["lookback"]], passes_fn=lambda s: s >= 0.7,
    )

    assert result["base_score"] == 0.9
    assert result["base_passes"] is True
    assert result["neighbor_pass_ratio"] == 1.0
    assert result["is_plateau"] is True


def test_not_a_plateau_when_a_neighbor_fails_despite_high_base_score():
    # lookback=20 單點極高，但緊鄰 19/21 直接垮 → 孤立尖峰，非高原
    landscape = {19: 0.1, 20: 0.95, 21: 0.05}
    grid = {"lookback": [19, 20, 21]}
    base_params = {"lookback": 20}

    result = parameter_plateau_scan(
        base_params, grid, evaluate_fn=lambda p: landscape[p["lookback"]], passes_fn=lambda s: s >= 0.7,
    )

    assert result["base_passes"] is True
    assert result["neighbor_pass_ratio"] == 0.0
    assert result["is_plateau"] is False


def test_not_a_plateau_when_base_itself_fails():
    landscape = {19: 0.9, 20: 0.5, 21: 0.9}
    grid = {"lookback": [19, 20, 21]}
    base_params = {"lookback": 20}

    result = parameter_plateau_scan(
        base_params, grid, evaluate_fn=lambda p: landscape[p["lookback"]], passes_fn=lambda s: s >= 0.7,
    )

    assert result["base_passes"] is False
    assert result["is_plateau"] is False  # 鄰居再好也救不回 base 本身不合格


def test_boundary_value_only_has_one_side_neighbor():
    landscape = {16: 0.8, 18: 0.9}
    grid = {"lookback": [16, 18, 20]}  # base=16 在網格最左端，只有右鄰居
    base_params = {"lookback": 16}

    result = parameter_plateau_scan(
        base_params, grid, evaluate_fn=lambda p: landscape[p["lookback"]], passes_fn=lambda s: s >= 0.7,
    )

    assert len(result["neighbors"]) == 1
    assert result["neighbors"][0]["value"] == 18


def test_base_value_not_in_grid_skips_that_dimension():
    grid = {"lookback": [16, 18, 20]}
    base_params = {"lookback": 999}  # 不在掃描網格內

    result = parameter_plateau_scan(
        base_params, grid, evaluate_fn=lambda p: 1.0, passes_fn=lambda s: s >= 0.7,
    )

    assert result["neighbors"] == []
    assert result["neighbor_pass_ratio"] is None
    assert result["is_plateau"] is False  # 無鄰居可驗證，不可宣稱高原


def test_multi_param_grid_scans_each_dimension_independently():
    # 兩個參數各自鄰近格，互不混合（one-at-a-time，非全網格組合）
    grid = {"lookback": [18, 20, 22], "volume_multiple_pct": [140, 150, 160]}
    base_params = {"lookback": 20, "volume_multiple_pct": 150}

    def evaluate_fn(p):
        # base 與所有鄰居都合格的簡單一致函式
        return 1.0

    result = parameter_plateau_scan(base_params, grid, evaluate_fn, passes_fn=lambda s: s >= 0.5)

    assert len(result["neighbors"]) == 4  # 兩參數各 2 個鄰居
    assert {n["param"] for n in result["neighbors"]} == {"lookback", "volume_multiple_pct"}
    assert result["is_plateau"] is True
