"""B7：mtf_resonance 週 MACD 的滑動窗 EMA 收斂性。

EMA 種子取窗首 n 根 SMA，窗每天滑動 → 同一週的 DIF_w 值隨窗漂移；required_bars 必須
大到讓「窗內計算」與「全歷史計算」的 DIF_w 實質一致，研究結論才能代表 pipeline 行為。
"""
import random
from dataclasses import dataclass
from datetime import date, timedelta

from src.contracts.models import MtfResonanceParams
from src.strategy.mtf_resonance import MtfResonanceStrategy, _macd, _weekly_closes


@dataclass
class _Bar:
    trade_date: date
    close: float


def _synthetic_bars(n: int, seed: int = 42) -> list:
    """確定性隨機漫步日 K（只在平日出 bar，模擬真實週結構）。"""
    rng = random.Random(seed)
    bars = []
    d = date(2018, 1, 1)
    price = 100.0
    while len(bars) < n:
        if d.weekday() < 5:
            price *= 1 + rng.uniform(-0.02, 0.021)
            bars.append(_Bar(trade_date=d, close=price))
        d += timedelta(days=1)
    return bars


def _last_complete_week_dif(bars: list, params: MtfResonanceParams) -> float:
    wk = _weekly_closes(bars)
    dif_w, _ = _macd(wk, params.macd_fast, params.macd_slow, params.macd_signal)
    return dif_w[-2]


def test_weekly_macd_window_converges_to_full_history():
    params = MtfResonanceParams()
    strategy = MtfResonanceStrategy(params, [], "TSE")
    required_bars = (params.macd_slow + params.macd_signal) * 3 * 5  # 與 generate() 同式

    full = _synthetic_bars(1500)
    dif_full = _last_complete_week_dif(full, params)
    dif_window = _last_complete_week_dif(full[-required_bars:], params)

    # 窗內計算與全歷史計算的上一完整週 DIF_w 須實質一致（相對誤差 <1%）
    scale = max(abs(dif_full), 1e-9)
    assert abs(dif_window - dif_full) / scale < 0.01, (
        f"windowed DIF_w={dif_window} vs full={dif_full}：required_bars 不足以讓 EMA 收斂"
    )


def test_old_200_bar_window_documented_divergence():
    """舊視窗（(26+9+5)*5=200 根）的漂移量級記錄：至少在部分市況下 >1%。
    多 seed 取最大偏差——證明放大視窗是必要的，不是吹毛求疵。"""
    params = MtfResonanceParams()
    worst = 0.0
    for seed in range(5):
        full = _synthetic_bars(1500, seed=seed)
        dif_full = _last_complete_week_dif(full, params)
        dif_old = _last_complete_week_dif(full[-200:], params)
        scale = max(abs(dif_full), 1e-9)
        worst = max(worst, abs(dif_old - dif_full) / scale)
    assert worst > 0.01, "若舊窗其實已收斂，B7 修正可回退（此斷言提醒重新評估）"
