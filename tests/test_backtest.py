"""Tests for the backtest machinery: detection replay, simulation, filters."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backtest import filters as f
from backtest.indicators import ema, rsi
from backtest.simulator import (
    Outcome,
    detect_signals,
    simulate_exit,
)
from strategy.models import Candle, Direction
from strategy.tamad_strategy import ComparisonMode, compute_trade_levels
from tests.fixtures import short_candles

TF_MS = 900_000


def flat(i: int, *, volume: float = 100.0) -> Candle:
    return Candle(i * TF_MS, 100.0, 105.0, 95.0, 101.0, volume)


def series_with_short_pattern(prefix: int = 10) -> list[Candle]:
    """`prefix` flat candles followed by the fixture SHORT pattern (retimed)."""
    candles = [flat(i) for i in range(prefix)]
    for offset, c in enumerate(short_candles()):
        candles.append(replace(c, open_time_ms=(prefix + offset) * TF_MS))
    return candles


class TestDetectSignals:
    def test_finds_the_embedded_pattern(self):
        candles = series_with_short_pattern()
        signals = list(detect_signals(candles, 0.1, ComparisonMode.STRICT))
        assert len(signals) == 1
        signal = signals[0]
        assert signal.direction is Direction.SHORT
        assert signal.index == len(candles) - 1
        assert signal.levels.entry == 109.5
        assert signal.levels.stop_loss == 113.0

    def test_flat_series_has_no_signals(self):
        # Consecutive identical candles fail the color sequence.
        assert not list(detect_signals([flat(i) for i in range(50)], 0.1, ComparisonMode.STRICT))

    def test_start_index_excludes_warmup_signals(self):
        candles = series_with_short_pattern(prefix=10)
        assert not list(
            detect_signals(candles, 0.1, ComparisonMode.STRICT, start_index=len(candles))
        )


class TestSimulateExit:
    """Entry 109.5, SL 113.0, TP2 102.5 (the fixture SHORT trade)."""

    def _series(self, *bars: tuple[float, float]) -> list[Candle]:
        """Entry bar followed by (high, low) forward bars."""
        candles = series_with_short_pattern()
        i = len(candles)
        for high, low in bars:
            mid = (high + low) / 2
            candles.append(Candle(i * TF_MS, mid, high, low, mid))
            i += 1
        return candles

    def test_target_hit_first_is_a_win(self):
        candles = self._series((110.0, 106.0), (107.0, 102.0))
        outcome, bars = simulate_exit(candles, len(candles) - 3, Direction.SHORT, 113.0, 102.5, 100)
        assert outcome is Outcome.WIN and bars == 2

    def test_stop_hit_first_is_a_loss(self):
        candles = self._series((113.5, 108.0), (107.0, 101.0))
        outcome, bars = simulate_exit(candles, len(candles) - 3, Direction.SHORT, 113.0, 102.5, 100)
        assert outcome is Outcome.LOSS and bars == 1

    def test_same_bar_ambiguity_is_a_loss(self):
        # One giant bar spans both stop and target → conservative LOSS.
        candles = self._series((114.0, 101.0))
        outcome, _ = simulate_exit(candles, len(candles) - 2, Direction.SHORT, 113.0, 102.5, 100)
        assert outcome is Outcome.LOSS

    def test_same_bar_ambiguity_optimistic_is_a_win(self):
        candles = self._series((114.0, 101.0))
        outcome, _ = simulate_exit(
            candles, len(candles) - 2, Direction.SHORT, 113.0, 102.5, 100, optimistic=True
        )
        assert outcome is Outcome.WIN

    def test_neither_level_within_horizon_is_a_timeout(self):
        candles = self._series((110.0, 106.0), (111.0, 105.0), (110.5, 106.5))
        outcome, _ = simulate_exit(candles, len(candles) - 4, Direction.SHORT, 113.0, 102.5, 100)
        assert outcome is Outcome.TIMEOUT

    def test_long_direction_mirrors(self):
        candles = self._series((112.0, 107.5), (118.0, 111.0))
        outcome, _ = simulate_exit(candles, len(candles) - 3, Direction.LONG, 107.0, 117.5, 100)
        assert outcome is Outcome.WIN


def make_context(candles: list[Candle], direction=Direction.SHORT, level=110.0):
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    return f.FilterContext(
        candles=candles,
        direction=direction,
        level=level,
        levels=compute_trade_levels(direction, c1, c2, c3),
    )


class TestFilters:
    def test_wick_sweep_requires_the_level_to_be_pierced(self):
        candles = series_with_short_pattern()
        ctx = make_context(candles)  # fixture c3 high 112 > level 110 → swept
        assert f.wick_sweep(ctx)

        no_sweep = candles[:-1] + [replace(candles[-1], high=109.9)]
        assert not f.wick_sweep(make_context(no_sweep))

    def test_ema_trend_rejects_counter_trend_and_short_history(self):
        rising = [replace(flat(i), close=100.0 + i * 0.5) for i in range(250)]
        falling = [replace(flat(i), close=250.0 - i * 0.5) for i in range(250)]
        short_ctx_up = make_context(rising, Direction.SHORT)
        short_ctx_down = make_context(falling, Direction.SHORT)
        check = f.make_ema_trend(200)
        assert not check(short_ctx_up)  # SHORT above EMA in an uptrend → reject
        assert check(short_ctx_down)  # SHORT below EMA in a downtrend → confirm
        assert not check(make_context(rising[:50], Direction.SHORT))  # too little history

    def test_volume_surge(self):
        candles = [flat(i, volume=100.0) for i in range(30)]
        surged = candles[:-1] + [replace(candles[-1], volume=200.0)]
        quiet = candles[:-1] + [replace(candles[-1], volume=120.0)]
        check = f.make_volume_surge(1.5, 20)
        assert check(make_context(surged))
        assert not check(make_context(quiet))
        no_volume = [flat(i, volume=0.0) for i in range(30)]
        assert not check(make_context(no_volume))  # missing data → reject

    def test_rsi_extreme(self):
        rising = [replace(flat(i), close=100.0 + i) for i in range(40)]
        falling = [replace(flat(i), close=200.0 - i) for i in range(40)]
        check = f.make_rsi_extreme(14, 60.0, 40.0)
        assert check(make_context(rising, Direction.SHORT))  # overbought SHORT ok
        assert not check(make_context(falling, Direction.SHORT))
        assert check(make_context(falling, Direction.LONG))
        assert not check(make_context(rising, Direction.LONG))

    def test_min_range_rejects_dead_candles(self):
        lively = series_with_short_pattern()
        assert f.make_min_range(0.10)(make_context(lively))
        dead = [
            Candle(i * TF_MS, 100.0, 100.02, 99.99, 100.01) for i in range(10)
        ]
        assert not f.make_min_range(0.10)(make_context(dead))

    def test_sr_filter_uses_swing_levels(self):
        candles = series_with_short_pattern(prefix=30)
        # Plant a confirmed swing high near the pattern level (110).
        candles[10] = replace(candles[10], high=110.1)
        check = f.make_sr_filter(3, 3, 0.25)
        assert check(make_context(candles))
        assert not check(make_context(series_with_short_pattern(prefix=30)))


class TestIndicators:
    def test_ema_of_constant_series_is_the_constant(self):
        assert ema([5.0] * 50, 20) == pytest.approx(5.0)

    def test_ema_insufficient_data_is_none(self):
        assert ema([1.0, 2.0], 20) is None

    def test_rsi_bounds(self):
        assert rsi([float(i) for i in range(1, 40)], 14) == pytest.approx(100.0)
        falling = rsi([float(40 - i) for i in range(40)], 14)
        assert falling is not None and falling < 1.0

    def test_rsi_insufficient_data_is_none(self):
        assert rsi([1.0] * 10, 14) is None
