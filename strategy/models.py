"""Core domain models for the Tamad Strategy scanner.

Everything in this module is a plain, immutable value object so the strategy
and validation layers stay pure and easy to test.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


class Direction(str, enum.Enum):
    """Trade direction of a detected setup."""

    LONG = "LONG"
    SHORT = "SHORT"


class SRType(str, enum.Enum):
    """Kind of support/resistance level the pattern formed at."""

    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    FRACTAL_HIGH = "fractal_high"
    FRACTAL_LOW = "fractal_low"
    PREV_DAY_HIGH = "prev_day_high"
    PREV_DAY_LOW = "prev_day_low"
    PREV_WEEK_HIGH = "prev_week_high"
    PREV_WEEK_LOW = "prev_week_low"
    PIVOT = "pivot"


@dataclass(frozen=True, slots=True)
class Candle:
    """A single fully-formed OHLC candle.

    ``open_time_ms`` is the bar open time in milliseconds since epoch (the
    same convention TradingView's ``time`` built-in uses).
    """

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        return self.close == self.open

    @property
    def open_time(self) -> datetime:
        return datetime.fromtimestamp(self.open_time_ms / 1000, tz=timezone.utc)

    def is_sane(self) -> bool:
        """Basic OHLC integrity: positive prices, high/low envelope the body."""
        return (
            min(self.open, self.high, self.low, self.close) > 0
            and self.high >= max(self.open, self.close)
            and self.low <= min(self.open, self.close)
        )

    def close_time_ms(self, timeframe_minutes: int) -> int:
        return self.open_time_ms + timeframe_minutes * 60_000


@dataclass(frozen=True, slots=True)
class TradeLevels:
    """Entry / stop / targets derived strictly from the three pattern candles."""

    entry: float
    stop_loss: float
    risk: float
    tp2: float
    tp3: float


@dataclass(frozen=True, slots=True)
class TamadSetup:
    """A fully described Tamad setup as reported by TradingView.

    All numeric fields are re-validated by :mod:`strategy.validation` before
    any notification is sent; nothing here is trusted as-is.
    """

    exchange: str
    symbol: str  # TradingView ticker, e.g. "BTCUSDT.P"
    timeframe_minutes: int
    direction: Direction
    candle1: Candle
    candle2: Candle
    candle3: Candle
    level: float  # support/resistance implied by the equal closes
    sr_type: str  # reported S/R classification (see SRType)
    sr_level: float  # the meaningful S/R level the pattern formed at
    entry: float
    stop_loss: float
    tp2: float
    tp3: float
    detected_at: datetime
    raw_payload: dict = field(default_factory=dict)

    @property
    def candles(self) -> tuple[Candle, Candle, Candle]:
        return (self.candle1, self.candle2, self.candle3)

    @property
    def pair(self) -> str:
        """Human-facing pair name, e.g. ``BTCUSDT`` for ``BTCUSDT.P``."""
        return self.symbol.removesuffix(".P")

    @property
    def timeframe_label(self) -> str:
        minutes = self.timeframe_minutes
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours}h"
        return f"{minutes}m"

    @property
    def tv_interval(self) -> str:
        """TradingView interval string ("15", "30", "60"...)."""
        return str(self.timeframe_minutes)

    @property
    def dedup_key(self) -> str:
        """Uniquely identifies one setup: same bar + symbol + tf + direction."""
        return (
            f"{self.exchange}:{self.symbol}:{self.timeframe_minutes}:"
            f"{self.direction.value}:{self.candle3.open_time_ms}"
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one validation rule."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregated outcome of the final strict validation."""

    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def summary(self) -> str:
        if self.passed:
            return "all checks passed"
        return "; ".join(f"{c.name}: {c.detail or 'failed'}" for c in self.failed_checks)
