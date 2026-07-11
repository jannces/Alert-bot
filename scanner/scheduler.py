"""Candle-close scheduling.

The scanner only ever evaluates newly closed candles, so the natural rhythm
is the timeframe boundary: at every quarter-hour a 15m candle closes, at
every half-hour additionally a 30m candle, at every full hour all three.
The scheduler sleeps until the next boundary (plus a small settle delay so
the exchange has published the final candle), then triggers one sweep for
every timeframe that closed at that boundary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

# on_sweep(timeframes_minutes, boundary_ms) — boundary_ms is the close time
# of the candles to evaluate.
SweepCallback = Callable[[list[int], int], Awaitable[None]]


def next_boundary_ms(now_ms: int, timeframe_minutes: int) -> int:
    """The next candle-close boundary strictly after ``now_ms``."""
    step = timeframe_minutes * 60_000
    return (now_ms // step + 1) * step


def timeframes_closing_at(boundary_ms: int, timeframes: Sequence[int]) -> list[int]:
    """Which of ``timeframes`` have a candle closing exactly at ``boundary_ms``."""
    return sorted(tf for tf in timeframes if boundary_ms % (tf * 60_000) == 0)


class SweepScheduler:
    """Sleeps to each boundary and runs the sweep callback, forever."""

    def __init__(
        self,
        timeframes_minutes: Sequence[int],
        settle_seconds: float,
        on_sweep: SweepCallback,
    ) -> None:
        if not timeframes_minutes:
            raise ValueError("at least one timeframe is required")
        self._timeframes = sorted(timeframes_minutes)
        self._settle = settle_seconds
        self._on_sweep = on_sweep
        self.last_boundary_ms: int | None = None

    async def run(self) -> None:
        logger.info(
            "scheduler started: timeframes=%s settle=%.1fs",
            self._timeframes,
            self._settle,
        )
        while True:
            now_ms = int(time.time() * 1000)
            boundary = min(next_boundary_ms(now_ms, tf) for tf in self._timeframes)
            sleep_s = (boundary - now_ms) / 1000 + self._settle
            logger.debug("sleeping %.1fs until boundary %d", sleep_s, boundary)
            await asyncio.sleep(sleep_s)

            closing = timeframes_closing_at(boundary, self._timeframes)
            self.last_boundary_ms = boundary
            try:
                await self._on_sweep(closing, boundary)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the scheduler must survive anything
                logger.exception("sweep for boundary %d failed", boundary)
