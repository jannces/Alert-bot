"""Support/resistance detection — configurable, never guessed.

Detectors implement a single small interface so additional methods
(fractals, daily/weekly high-low, pivot points) can plug in later without
touching the scanner or pipeline:

    detector.find_levels(candles) -> list[SRLevel]

Only ``swing_high_low`` is implemented today, matching the configuration:

    support_resistance:
      method: swing_high_low
      left_bars: 20
      right_bars: 20
      proximity_percent: 0.25
"""

from __future__ import annotations

from typing import Protocol, Sequence

from strategy.models import Candle, SRKind, SRLevel
from strategy.tamad_strategy import sr_level_is_meaningful


class SRDetector(Protocol):
    """Interface every support/resistance detector implements."""

    def find_levels(self, candles: Sequence[Candle]) -> list[SRLevel]:
        """Return all confirmed S/R levels in the supplied candle history."""
        ...


class SwingHighLowDetector:
    """Confirmed swing highs/lows.

    A swing high is a bar whose high is strictly greater than the highs of
    the ``left_bars`` bars before it AND the ``right_bars`` bars after it
    (mirror for swing lows). Requiring bars on the right means a swing is
    only *confirmed* ``right_bars`` bars after it prints — deliberately
    non-repainting: a level either exists or it doesn't.
    """

    def __init__(self, left_bars: int, right_bars: int) -> None:
        if left_bars < 1 or right_bars < 1:
            raise ValueError("left_bars and right_bars must be >= 1")
        self._left = left_bars
        self._right = right_bars

    def find_levels(self, candles: Sequence[Candle]) -> list[SRLevel]:
        levels: list[SRLevel] = []
        n = len(candles)
        for i in range(self._left, n - self._right):
            high = candles[i].high
            low = candles[i].low
            window = list(candles[i - self._left : i]) + list(
                candles[i + 1 : i + 1 + self._right]
            )
            if all(high > c.high for c in window):
                levels.append(SRLevel(kind=SRKind.SWING_HIGH, price=high))
            if all(low < c.low for c in window):
                levels.append(SRLevel(kind=SRKind.SWING_LOW, price=low))
        return levels


def nearest_level(
    levels: Sequence[SRLevel],
    side: str,
    reference_price: float,
    proximity_pct: float,
) -> SRLevel | None:
    """The closest level on ``side`` ("high"/"low") within the proximity.

    Returns ``None`` when no meaningful level is near the reference price —
    the pattern is in the middle of a range and must be rejected.
    """
    best: SRLevel | None = None
    for level in levels:
        if level.side not in (side, "any"):
            continue
        if not sr_level_is_meaningful(reference_price, level.price, proximity_pct):
            continue
        if best is None or abs(level.price - reference_price) < abs(
            best.price - reference_price
        ):
            best = level
    return best


def create_detector(method: str, *, left_bars: int, right_bars: int) -> SRDetector:
    """Factory keyed by the ``support_resistance.method`` config value."""
    if method == "swing_high_low":
        return SwingHighLowDetector(left_bars=left_bars, right_bars=right_bars)
    raise ValueError(
        f"unsupported support_resistance.method {method!r} "
        "(available: swing_high_low)"
    )
