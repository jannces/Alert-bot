"""Tests for support/resistance detection."""

from __future__ import annotations

import pytest

from strategy.models import Candle, SRKind
from strategy.sr_levels import SwingHighLowDetector, create_detector, nearest_level

TF_MS = 900_000


def flat(i: int, *, high: float = 105.0, low: float = 95.0) -> Candle:
    return Candle(i * TF_MS, 100.0, high, low, 101.0)


def series_with_swing(swing_index: int, swing_high: float, count: int) -> list[Candle]:
    candles = [flat(i) for i in range(count)]
    base = candles[swing_index]
    candles[swing_index] = Candle(base.open_time_ms, 100.0, swing_high, 95.0, 101.0)
    return candles


class TestSwingHighLowDetector:
    def test_finds_confirmed_swing_high(self):
        candles = series_with_swing(10, 110.0, 20)
        levels = SwingHighLowDetector(3, 3).find_levels(candles)
        highs = [l for l in levels if l.kind is SRKind.SWING_HIGH]
        assert [l.price for l in highs] == [110.0]

    def test_finds_swing_low(self):
        candles = [flat(i) for i in range(20)]
        candles[8] = Candle(8 * TF_MS, 100.0, 105.0, 90.0, 101.0)
        levels = SwingHighLowDetector(3, 3).find_levels(candles)
        lows = [l for l in levels if l.kind is SRKind.SWING_LOW]
        assert [l.price for l in lows] == [90.0]

    def test_unconfirmed_swing_is_not_a_level(self):
        # Swing at the very end: fewer than right_bars bars follow it.
        candles = series_with_swing(18, 110.0, 20)
        levels = SwingHighLowDetector(3, 3).find_levels(candles)
        assert not any(l.kind is SRKind.SWING_HIGH for l in levels)

    def test_equal_highs_are_not_a_swing(self):
        # Strictly-greater rule: a plateau does not print a swing.
        candles = series_with_swing(10, 110.0, 20)
        candles[11] = Candle(11 * TF_MS, 100.0, 110.0, 95.0, 101.0)
        levels = SwingHighLowDetector(3, 3).find_levels(candles)
        assert not any(l.kind is SRKind.SWING_HIGH for l in levels)

    def test_rejects_invalid_configuration(self):
        with pytest.raises(ValueError):
            SwingHighLowDetector(0, 3)


class TestNearestLevel:
    def test_finds_level_within_proximity(self):
        levels = SwingHighLowDetector(3, 3).find_levels(series_with_swing(10, 110.05, 20))
        found = nearest_level(levels, "high", 110.0, proximity_pct=0.25)
        assert found is not None and found.price == 110.05

    def test_middle_of_range_returns_none(self):
        levels = SwingHighLowDetector(3, 3).find_levels(series_with_swing(10, 120.0, 20))
        assert nearest_level(levels, "high", 110.0, proximity_pct=0.25) is None

    def test_wrong_side_is_ignored(self):
        levels = SwingHighLowDetector(3, 3).find_levels(series_with_swing(10, 110.05, 20))
        assert nearest_level(levels, "low", 110.0, proximity_pct=0.25) is None

    def test_closest_of_multiple_levels_wins(self):
        candles = series_with_swing(6, 110.4, 26)
        base = candles[16]
        candles[16] = Candle(base.open_time_ms, 100.0, 110.1, 95.0, 101.0)
        levels = SwingHighLowDetector(3, 3).find_levels(candles)
        found = nearest_level(levels, "high", 110.0, proximity_pct=0.5)
        assert found is not None and found.price == 110.1


class TestFactory:
    def test_creates_swing_detector(self):
        detector = create_detector("swing_high_low", left_bars=5, right_bars=5)
        assert isinstance(detector, SwingHighLowDetector)

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError):
            create_detector("astrology", left_bars=5, right_bars=5)
