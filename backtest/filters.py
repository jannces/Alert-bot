"""Candidate confirmation filters, evaluated at signal time only.

Every filter sees a :class:`FilterContext` whose ``candles`` end at Candle 3
(the just-closed bar) — nothing after it — so no filter can look into the
future. A filter returning ``False`` rejects the candidate; when a filter
cannot be computed (e.g. not enough history for an EMA), it also rejects,
matching the project's "when in doubt, reject" rule.

These implementations are shared verbatim between the backtester and (once
a filter is proven on data) the live scan engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from backtest.indicators import ema, rsi
from strategy.models import Candle, Direction, TradeLevels
from strategy.sr_levels import SwingHighLowDetector, nearest_level


@dataclass(frozen=True, slots=True)
class FilterContext:
    """Everything a confirmation may inspect at detection time."""

    candles: Sequence[Candle]  # full history, ending at Candle 3 (index -1)
    direction: Direction
    level: float  # equal-close pattern level
    levels: TradeLevels

    @property
    def c1(self) -> Candle:
        return self.candles[-3]

    @property
    def c2(self) -> Candle:
        return self.candles[-2]

    @property
    def c3(self) -> Candle:
        return self.candles[-1]


ConfirmationFilter = Callable[[FilterContext], bool]


def wick_sweep(ctx: FilterContext) -> bool:
    """Candle 3's wick must actually pierce the level, not just approach it.

    The pattern then reads as a liquidity sweep: the level was broken
    intrabar, breakout traders were trapped, and the close rejected back.
    """
    if ctx.direction is Direction.SHORT:
        return ctx.c3.high > ctx.level
    return ctx.c3.low < ctx.level


def make_ema_trend(period: int = 200) -> ConfirmationFilter:
    """Trade only with the prevailing trend: SHORT below the EMA, LONG above."""

    def ema_trend(ctx: FilterContext) -> bool:
        value = ema([c.close for c in ctx.candles], period)
        if value is None:
            return False
        if ctx.direction is Direction.SHORT:
            return ctx.c3.close < value
        return ctx.c3.close > value

    return ema_trend


def make_volume_surge(multiple: float = 1.5, lookback: int = 20) -> ConfirmationFilter:
    """Candle 3's volume must exceed ``multiple`` × the recent average."""

    def volume_surge(ctx: FilterContext) -> bool:
        if len(ctx.candles) < lookback + 1:
            return False
        window = ctx.candles[-(lookback + 1) : -1]
        average = sum(c.volume for c in window) / lookback
        if average <= 0:
            return False  # no volume data — cannot confirm
        return ctx.c3.volume > multiple * average

    return volume_surge


def make_rsi_extreme(
    period: int = 14, short_min: float = 60.0, long_max: float = 40.0
) -> ConfirmationFilter:
    """The rejection must occur from a stretched condition."""

    def rsi_extreme(ctx: FilterContext) -> bool:
        value = rsi([c.close for c in ctx.candles], period)
        if value is None:
            return False
        if ctx.direction is Direction.SHORT:
            return value >= short_min
        return value <= long_max

    return rsi_extreme


def make_min_range(min_avg_range_pct: float = 0.1) -> ConfirmationFilter:
    """The three pattern candles must show real movement.

    Filters out near-dead instruments (e.g. tokenized stocks off-hours)
    whose "equal closes" are coincidences of a stalled price, not levels.
    """

    def min_range(ctx: FilterContext) -> bool:
        ranges = [
            (c.high - c.low) / c.close * 100.0 for c in (ctx.c1, ctx.c2, ctx.c3)
        ]
        return sum(ranges) / 3.0 >= min_avg_range_pct

    return min_range


def make_sr_filter(
    left_bars: int = 20, right_bars: int = 20, proximity_pct: float = 0.25
) -> ConfirmationFilter:
    """The original swing high/low S/R confirmation, for re-testing on data."""
    detector = SwingHighLowDetector(left_bars, right_bars)

    def sr_filter(ctx: FilterContext) -> bool:
        side = "high" if ctx.direction is Direction.SHORT else "low"
        levels = detector.find_levels(ctx.candles)
        return nearest_level(levels, side, ctx.level, proximity_pct) is not None

    return sr_filter
