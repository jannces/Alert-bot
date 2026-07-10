"""Pure implementation of the Tamad Strategy pattern rules.

The Tamad Strategy is a strict three-candle rejection pattern that must form
at a meaningful support or resistance level:

SHORT
    Candle 1: green (bullish)
    Candle 2: red (bearish), closing equal to Candle 1's close (within a
              configurable tolerance). The equal closes form the resistance.
    Candle 3: green and fully closed. Its wick may trade above the
              resistance, but its *close* must never be above it.
    Stop loss: the highest high of the three candles.

LONG
    Exact mirror image (red / green / red, equal closes form the support,
    Candle 3 must not close below it, stop is the lowest low).

Entry is always the close of Candle 3. TP2/TP3 are 2R and 3R from entry.
No indicators, ATR, or percentages are ever used for the stop.

This module is deliberately side-effect free so the exact same code path is
used by the final pre-alert validation and by the unit tests.
"""

from __future__ import annotations

import enum
from typing import Sequence

from strategy.models import Candle, Direction, TradeLevels


class LevelBasis(str, enum.Enum):
    """How the pattern level is derived from the two (near-)equal closes.

    The two closes only match within a tolerance, so a rule is needed to pick
    the exact level Candle 3 is compared against:

    - ``STRICT``: the level Candle 3 is *least* allowed to break — the lower
      of the two closes for a SHORT, the higher for a LONG. This is the
      default: when in doubt, reject.
    - ``OUTER``: the more permissive of the two closes.
    - ``AVG``: the midpoint of the two closes.
    """

    STRICT = "strict"
    OUTER = "outer"
    AVG = "avg"


def equal_close(close1: float, close2: float, tolerance_pct: float) -> bool:
    """True when the two closes match within ``tolerance_pct`` percent.

    The tolerance is measured relative to Candle 1's close, mirroring the
    Pine Script implementation exactly.
    """
    if close1 <= 0 or close2 <= 0:
        return False
    return abs(close1 - close2) <= close1 * tolerance_pct / 100.0


def pattern_level(
    direction: Direction, close1: float, close2: float, basis: LevelBasis
) -> float:
    """The support/resistance level implied by the two equal closes."""
    if basis is LevelBasis.AVG:
        return (close1 + close2) / 2.0
    if direction is Direction.SHORT:
        # Resistance. STRICT = lower close (hardest to satisfy).
        return min(close1, close2) if basis is LevelBasis.STRICT else max(close1, close2)
    # Support. STRICT = higher close (hardest to satisfy).
    return max(close1, close2) if basis is LevelBasis.STRICT else min(close1, close2)


def candle_colors_valid(direction: Direction, c1: Candle, c2: Candle, c3: Candle) -> bool:
    """Check the mandatory candle color sequence. Dojis always fail."""
    if direction is Direction.SHORT:
        return c1.is_green and c2.is_red and c3.is_green
    return c1.is_red and c2.is_green and c3.is_red


def third_candle_respects_level(direction: Direction, c3: Candle, level: float) -> bool:
    """Candle 3's wick may pierce the level but its close must respect it."""
    if direction is Direction.SHORT:
        return c3.close <= level
    return c3.close >= level


def compute_trade_levels(
    direction: Direction, c1: Candle, c2: Candle, c3: Candle
) -> TradeLevels:
    """Derive entry / stop / targets strictly from the three pattern candles.

    Entry is Candle 3's close. The stop is the extreme wick of the three
    candles — never ATR, percentages, or indicators.
    """
    entry = c3.close
    if direction is Direction.SHORT:
        stop_loss = max(c1.high, c2.high, c3.high)
        risk = stop_loss - entry
        tp2 = entry - 2.0 * risk
        tp3 = entry - 3.0 * risk
    else:
        stop_loss = min(c1.low, c2.low, c3.low)
        risk = entry - stop_loss
        tp2 = entry + 2.0 * risk
        tp3 = entry + 3.0 * risk
    return TradeLevels(entry=entry, stop_loss=stop_loss, risk=risk, tp2=tp2, tp3=tp3)


def detect_pattern(
    candles: Sequence[Candle],
    tolerance_pct: float,
    basis: LevelBasis = LevelBasis.STRICT,
) -> tuple[Direction, float, TradeLevels] | None:
    """Detect a Tamad pattern on the last three candles of ``candles``.

    All candles supplied must already be fully closed — this function never
    sees, and must never be given, a forming candle.

    Returns ``(direction, level, trade_levels)`` for a valid pattern, or
    ``None`` when no rule-perfect pattern exists. The meaningful-S/R filter
    is applied separately (it needs broader market context than three bars).
    """
    if len(candles) < 3:
        return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    if not (c1.is_sane() and c2.is_sane() and c3.is_sane()):
        return None
    if not equal_close(c1.close, c2.close, tolerance_pct):
        return None

    for direction in (Direction.SHORT, Direction.LONG):
        if not candle_colors_valid(direction, c1, c2, c3):
            continue
        level = pattern_level(direction, c1.close, c2.close, basis)
        if not third_candle_respects_level(direction, c3, level):
            return None  # colors matched but the third-candle rule failed
        levels = compute_trade_levels(direction, c1, c2, c3)
        if levels.risk <= 0:
            return None
        return direction, level, levels
    return None


def sr_level_is_meaningful(
    pattern_lvl: float, sr_level: float, proximity_pct: float
) -> bool:
    """True when the reported S/R level sits close enough to the pattern.

    ``proximity_pct`` is the maximum distance between the pattern level and
    the structural S/R level, as a percentage of the pattern level.
    """
    if pattern_lvl <= 0 or sr_level <= 0:
        return False
    return abs(sr_level - pattern_lvl) <= pattern_lvl * proximity_pct / 100.0
