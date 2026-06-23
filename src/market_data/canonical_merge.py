"""Canonical bar 合併：(symbol, trade_date, price_basis) 唯一不變式的寫入前防線。

不同來源可分段（如 FinMind 主歷史 + TWSE 補洞），但日期區間不得重疊；切點需通過
價格連續性檢查（無對應公司行動可解釋的異常報酬即拒絕）才允許合併寫入 research.db。
"""
from dataclasses import dataclass
from datetime import date

from src.contracts.models import MarketBar


class CanonicalMergeError(ValueError):
    pass


@dataclass
class SourceSegment:
    source_name: str
    bars: list  # list[MarketBar]，同一 (symbol, price_basis)，已按 trade_date 排序


def merge_canonical_bars(
    segments: list,
    abnormal_return_threshold: float = 0.20,
    known_action_dates: frozenset = frozenset(),
) -> list:
    """合併多個來源分段為單一 canonical 序列。任一檢查失敗即拋 CanonicalMergeError，不靜默丟資料。"""
    if not segments:
        return []

    symbols = {b.symbol for seg in segments for b in seg.bars}
    bases = {b.price_basis for seg in segments for b in seg.bars}
    if len(symbols) > 1 or len(bases) > 1:
        raise CanonicalMergeError("merge_canonical_bars 僅接受單一 (symbol, price_basis) 的分段")

    nonempty = [seg for seg in segments if seg.bars]
    nonempty.sort(key=lambda seg: seg.bars[0].trade_date)

    # 1. 重疊檢查：不同來源的日期區間不得相交
    for i in range(len(nonempty)):
        for j in range(i + 1, len(nonempty)):
            a, b = nonempty[i], nonempty[j]
            if a.source_name == b.source_name:
                continue
            a_start, a_end = a.bars[0].trade_date, a.bars[-1].trade_date
            b_start, b_end = b.bars[0].trade_date, b.bars[-1].trade_date
            if a_start <= b_end and b_start <= a_end:
                raise CanonicalMergeError(
                    f"來源 {a.source_name}[{a_start}~{a_end}] 與 "
                    f"{b.source_name}[{b_start}~{b_end}] 日期重疊"
                )

    # 2. 切點連續性檢查（鄰接分段交界）
    merged: list = []
    for seg in nonempty:
        if merged:
            _check_seam(merged[-1], seg.bars[0], abnormal_return_threshold, known_action_dates)
        merged.extend(seg.bars)

    return merged


def _check_seam(
    prev_bar: MarketBar, next_bar: MarketBar,
    threshold: float, known_action_dates: frozenset
) -> None:
    if prev_bar.close <= 0:
        return
    ret = (next_bar.close - prev_bar.close) / prev_bar.close
    if abs(ret) > threshold and next_bar.trade_date not in known_action_dates:
        raise CanonicalMergeError(
            f"切點異常報酬 {ret:.1%}（{prev_bar.trade_date} -> {next_bar.trade_date}），"
            f"且無對應公司行動可解釋——疑為來源切換造成的價格不連續"
        )
