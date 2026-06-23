"""Canonical bar 合併不變式測試（P0-T3）：重疊輸入被拒、切點異常被偵出。"""
import pytest
from datetime import date, datetime, timezone

from src.contracts.models import MarketBar
from src.market_data.canonical_merge import (
    CanonicalMergeError, SourceSegment, merge_canonical_bars,
)


def _bar(symbol: str, trade_date: date, close: int, source: str) -> MarketBar:
    return MarketBar(
        symbol=symbol, exchange="TSE", instrument_type="STOCK",
        trade_date=trade_date,
        open=close, high=close, low=close, close=close,
        volume=1000, amount=close * 1000,
        source=source,
        source_fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_payload_checksum="x",
        price_basis="raw", adjustment_factor=1.0,
    )


def test_non_overlapping_segments_merge_in_date_order():
    seg_a = SourceSegment("finmind", [_bar("2330", date(2022, 1, 3), 6310000, "finmind")])
    seg_b = SourceSegment("twse", [_bar("2330", date(2022, 1, 4), 6320000, "twse")])
    merged = merge_canonical_bars([seg_a, seg_b])
    assert [b.trade_date for b in merged] == [date(2022, 1, 3), date(2022, 1, 4)]


def test_overlapping_segments_from_different_sources_rejected():
    seg_a = SourceSegment("finmind", [_bar("2330", date(2022, 1, 3), 6310000, "finmind")])
    seg_b = SourceSegment("twse", [_bar("2330", date(2022, 1, 3), 6310000, "twse")])
    with pytest.raises(CanonicalMergeError, match="日期重疊"):
        merge_canonical_bars([seg_a, seg_b])


def test_abnormal_seam_return_rejected():
    seg_a = SourceSegment("finmind", [_bar("2330", date(2022, 1, 3), 1000000, "finmind")])
    seg_b = SourceSegment("twse", [_bar("2330", date(2022, 1, 4), 2000000, "twse")])  # +100%
    with pytest.raises(CanonicalMergeError, match="異常報酬"):
        merge_canonical_bars([seg_a, seg_b])


def test_abnormal_seam_return_allowed_with_known_action_date():
    seg_a = SourceSegment("finmind", [_bar("2330", date(2022, 1, 3), 1000000, "finmind")])
    seg_b = SourceSegment("twse", [_bar("2330", date(2022, 1, 4), 2000000, "twse")])
    merged = merge_canonical_bars([seg_a, seg_b], known_action_dates=frozenset({date(2022, 1, 4)}))
    assert len(merged) == 2


def test_mixed_symbol_rejected():
    seg_a = SourceSegment("finmind", [_bar("2330", date(2022, 1, 3), 6310000, "finmind")])
    seg_b = SourceSegment("finmind", [_bar("2317", date(2022, 1, 4), 1000000, "finmind")])
    with pytest.raises(CanonicalMergeError):
        merge_canonical_bars([seg_a, seg_b])
