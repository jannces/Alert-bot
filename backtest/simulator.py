"""Signal detection replay and trade outcome simulation.

Detection re-uses the exact live rules from :mod:`strategy.tamad_strategy` —
the backtest can never drift from what the scanner actually does.

Outcome simulation is deliberately conservative:

- entry is Candle 3's close (as the strategy defines);
- bars after entry are walked forward; within a single bar the STOP is
  assumed to be hit before the target whenever both levels fall inside that
  bar's range (the pessimistic resolution of intrabar ambiguity);
- a trade that touches neither level within the horizon is a "timeout" and
  is excluded from win rates (reported separately).

No fees, funding, or slippage are modeled; win rates are gross. The
breakeven win rate is 33.4% for a 2R target and 25.0% for 3R — real costs
push those thresholds up slightly.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterator, Sequence

from strategy import tamad_strategy as rules
from strategy.models import Candle, Direction, TradeLevels
from strategy.tamad_strategy import ComparisonMode


class Outcome(str, enum.Enum):
    WIN = "win"
    LOSS = "loss"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class Signal:
    """A rule-valid Tamad setup found at ``index`` (Candle 3's position)."""

    index: int
    direction: Direction
    level: float
    levels: TradeLevels


def detect_signals(
    candles: Sequence[Candle],
    tolerance_pct: float,
    mode: ComparisonMode,
    start_index: int = 2,
) -> Iterator[Signal]:
    """Replay the live pattern rules over history, bar by bar."""
    for i in range(max(start_index, 2), len(candles)):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
        direction = rules.matches_prefilter(c1, c2, c3, tolerance_pct)
        if direction is None:
            continue
        level = rules.pattern_level(direction, c1.close, c2.close, mode)
        if not rules.third_candle_respects_level(direction, c3, level):
            continue
        levels = rules.compute_trade_levels(direction, c1, c2, c3)
        if levels.risk <= 0:
            continue
        yield Signal(index=i, direction=direction, level=level, levels=levels)


def simulate_exit(
    candles: Sequence[Candle],
    entry_index: int,
    direction: Direction,
    stop_loss: float,
    target: float,
    horizon_bars: int,
    optimistic: bool = False,
) -> tuple[Outcome, int]:
    """Walk forward from the bar after entry until stop/target/horizon.

    Returns the outcome and the number of bars held. When one bar touches
    both levels, the resolution order is ambiguous without lower-timeframe
    data: the default (conservative) counts it as a LOSS, ``optimistic``
    counts it as a WIN. Running both brackets the true win rate.
    """
    last = min(len(candles) - 1, entry_index + horizon_bars)
    for j in range(entry_index + 1, last + 1):
        bar = candles[j]
        if direction is Direction.SHORT:
            hit_stop = bar.high >= stop_loss
            hit_target = bar.low <= target
        else:
            hit_stop = bar.low <= stop_loss
            hit_target = bar.high >= target
        if hit_stop and hit_target:
            return (Outcome.WIN if optimistic else Outcome.LOSS), j - entry_index
        if hit_stop:
            return Outcome.LOSS, j - entry_index
        if hit_target:
            return Outcome.WIN, j - entry_index
    return Outcome.TIMEOUT, last - entry_index


@dataclass
class VariantResult:
    """Aggregate statistics for one filter combination."""

    name: str
    signals: int = 0
    resolved_2r: int = 0
    wins_2r: int = 0
    resolved_3r: int = 0
    wins_3r: int = 0
    timeouts_2r: int = 0

    @property
    def win_rate_2r(self) -> float | None:
        return self.wins_2r / self.resolved_2r if self.resolved_2r else None

    @property
    def win_rate_3r(self) -> float | None:
        return self.wins_3r / self.resolved_3r if self.resolved_3r else None

    @property
    def expectancy_2r(self) -> float | None:
        """Average R per trade exiting fully at TP2 (win +2R, loss −1R)."""
        wr = self.win_rate_2r
        return None if wr is None else wr * 2.0 - (1.0 - wr)

    @property
    def expectancy_3r(self) -> float | None:
        """Average R per trade exiting fully at TP3 (win +3R, loss −1R)."""
        wr = self.win_rate_3r
        return None if wr is None else wr * 3.0 - (1.0 - wr)
