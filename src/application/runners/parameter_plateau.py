"""參數高原/鄰近穩健性掃描（P2-T4）。

不取單點最佳——要求 base 參數的「鄰居」整片也合格，否則只是踩中一個剛好好看的孤立點
（防 Challenger 用「單點最佳」偷渡）。鄰居定義為每個參數獨立 ±1 格（one-at-a-time），
非全網格組合：維度一多，全組合數量隨指數成長，多數研究場景跑不起也不需要——
one-at-a-time 已足以揪出「緊鄰一格就垮」的脆弱單點。

與 evaluate_fn/passes_fn 解耦，不綁定 BacktestRunner——呼叫端自行決定 evaluate_fn 要不要
真的跑一次 backtest，本工具只管「掃鄰居 + 判合格 + 算比例」。
"""
from typing import Callable


def parameter_plateau_scan(
    base_params: dict, param_grid: dict[str, list], evaluate_fn: Callable[[dict], float],
    passes_fn: Callable[[float], bool],
) -> dict:
    base_score = evaluate_fn(base_params)
    neighbors = []
    for key, values in param_grid.items():
        if base_params.get(key) not in values:
            continue  # base 不在掃描網格內，該維度不可比，跳過
        idx = values.index(base_params[key])
        for neighbor_idx in (idx - 1, idx + 1):
            if 0 <= neighbor_idx < len(values):
                neighbor_params = {**base_params, key: values[neighbor_idx]}
                score = evaluate_fn(neighbor_params)
                neighbors.append({
                    "param": key, "value": values[neighbor_idx], "score": score, "passes": passes_fn(score),
                })

    pass_count = sum(1 for n in neighbors if n["passes"])
    return {
        "base_score": base_score,
        "base_passes": passes_fn(base_score),
        "neighbors": neighbors,
        "neighbor_pass_ratio": (pass_count / len(neighbors)) if neighbors else None,
        "is_plateau": bool(neighbors) and passes_fn(base_score) and all(n["passes"] for n in neighbors),
    }
