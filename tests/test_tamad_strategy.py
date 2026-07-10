"""Unit tests for the pure Tamad Strategy rules."""

from __future__ import annotations

from dataclasses import replace

from strategy.models import Candle, Direction
from strategy.tamad_strategy import (
    LevelBasis,
    candle_colors_valid,
    compute_trade_levels,
    detect_pattern,
    equal_close,
    pattern_level,
    sr_level_is_meaningful,
    third_candle_respects_level,
)
from tests.fixtures import long_candles, short_candles

TOL = 0.05


class TestEqualClose:
    def test_identical_closes_pass(self):
        assert equal_close(100.0, 100.0, TOL)

    def test_within_tolerance_passes(self):
        assert equal_close(100.0, 100.049, TOL)

    def test_beyond_tolerance_fails(self):
        assert not equal_close(100.0, 100.051, TOL)

    def test_zero_tolerance_requires_exact_match(self):
        assert not equal_close(100.0, 100.0000001, 0.0)
        assert equal_close(100.0, 100.0, 0.0)

    def test_non_positive_prices_fail(self):
        assert not equal_close(0.0, 100.0, TOL)
        assert not equal_close(100.0, -1.0, TOL)


class TestPatternLevel:
    def test_short_strict_uses_lower_close(self):
        assert pattern_level(Direction.SHORT, 110.0, 110.02, LevelBasis.STRICT) == 110.0

    def test_long_strict_uses_higher_close(self):
        assert pattern_level(Direction.LONG, 110.0, 110.02, LevelBasis.STRICT) == 110.02

    def test_outer_is_mirror_of_strict(self):
        assert pattern_level(Direction.SHORT, 110.0, 110.02, LevelBasis.OUTER) == 110.02
        assert pattern_level(Direction.LONG, 110.0, 110.02, LevelBasis.OUTER) == 110.0

    def test_avg_is_midpoint(self):
        assert pattern_level(Direction.SHORT, 100.0, 102.0, LevelBasis.AVG) == 101.0


class TestCandleColors:
    def test_short_needs_green_red_green(self):
        c1, c2, c3 = short_candles()
        assert candle_colors_valid(Direction.SHORT, c1, c2, c3)
        assert not candle_colors_valid(Direction.LONG, c1, c2, c3)

    def test_long_needs_red_green_red(self):
        c1, c2, c3 = long_candles()
        assert candle_colors_valid(Direction.LONG, c1, c2, c3)
        assert not candle_colors_valid(Direction.SHORT, c1, c2, c3)

    def test_doji_is_rejected(self):
        c1, c2, c3 = short_candles()
        doji = replace(c3, close=c3.open)
        assert not candle_colors_valid(Direction.SHORT, c1, c2, doji)
        assert not candle_colors_valid(Direction.LONG, c1, c2, doji)


class TestThirdCandleRule:
    def test_short_close_at_resistance_is_allowed(self):
        _, _, c3 = short_candles()
        assert third_candle_respects_level(Direction.SHORT, replace(c3, close=110.0), 110.0)

    def test_short_close_above_resistance_rejected(self):
        _, _, c3 = short_candles()
        assert not third_candle_respects_level(
            Direction.SHORT, replace(c3, close=110.01, high=112.0), 110.0
        )

    def test_short_wick_above_resistance_is_allowed(self):
        _, _, c3 = short_candles()
        assert c3.high > 110.0  # the fixture wick pierces the level
        assert third_candle_respects_level(Direction.SHORT, c3, 110.0)

    def test_long_close_below_support_rejected(self):
        assert not third_candle_respects_level(
            Direction.LONG, Candle(0, 111.0, 111.5, 108.0, 109.9), 110.0
        )


class TestTradeLevels:
    def test_short_levels(self):
        c1, c2, c3 = short_candles()
        levels = compute_trade_levels(Direction.SHORT, c1, c2, c3)
        assert levels.entry == 109.5  # candle 3 close
        assert levels.stop_loss == 113.0  # highest wick of the three candles
        assert levels.risk == 3.5
        assert levels.tp2 == 102.5  # entry - 2R
        assert levels.tp3 == 99.0  # entry - 3R

    def test_long_levels(self):
        c1, c2, c3 = long_candles()
        levels = compute_trade_levels(Direction.LONG, c1, c2, c3)
        assert levels.entry == 110.5
        assert levels.stop_loss == 107.0  # lowest wick of the three candles
        assert levels.risk == 3.5
        assert levels.tp2 == 117.5
        assert levels.tp3 == 121.0


class TestDetectPattern:
    def test_valid_short_detected(self):
        result = detect_pattern(short_candles(), TOL)
        assert result is not None
        direction, level, levels = result
        assert direction is Direction.SHORT
        assert level == 110.0
        assert levels.stop_loss == 113.0

    def test_valid_long_detected(self):
        result = detect_pattern(long_candles(), TOL)
        assert result is not None
        direction, level, _ = result
        assert direction is Direction.LONG
        assert level == 110.02

    def test_unequal_closes_rejected(self):
        c1, c2, c3 = short_candles()
        c2 = replace(c2, close=111.0)  # ~0.9% away — far beyond tolerance
        assert detect_pattern((c1, c2, c3), TOL) is None

    def test_close_through_level_rejected(self):
        c1, c2, c3 = short_candles()
        c3 = replace(c3, close=110.5, high=112.0)  # closes above resistance
        assert detect_pattern((c1, c2, c3), TOL) is None

    def test_wrong_colors_rejected(self):
        c1, c2, c3 = short_candles()
        c3 = replace(c3, open=110.9, close=109.5)  # red third candle on a SHORT
        assert detect_pattern((c1, c2, c3), TOL) is None

    def test_insane_ohlc_rejected(self):
        c1, c2, c3 = short_candles()
        c3 = replace(c3, high=1.0)  # high below the body
        assert detect_pattern((c1, c2, c3), TOL) is None

    def test_fewer_than_three_candles_rejected(self):
        c1, c2, _ = short_candles()
        assert detect_pattern((c1, c2), TOL) is None


class TestSRProximity:
    def test_level_at_sr_passes(self):
        assert sr_level_is_meaningful(110.0, 110.05, 0.15)

    def test_middle_of_range_fails(self):
        assert not sr_level_is_meaningful(110.0, 112.0, 0.15)

    def test_invalid_levels_fail(self):
        assert not sr_level_is_meaningful(0.0, 110.0, 0.15)
        assert not sr_level_is_meaningful(110.0, 0.0, 0.15)
