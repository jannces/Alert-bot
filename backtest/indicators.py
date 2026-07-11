"""Minimal indicator math for confirmation filters.

Deliberately dependency-free and simple; these are used for *filtering
candidates* in the backtest and (if proven) in the live engine — never for
stop-loss or target calculation, which the strategy forbids.
"""

from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average of the full series, standard SMA seeding.

    Returns ``None`` when there is not enough data — callers must treat that
    as "cannot confirm" and reject.
    """
    if period < 1 or len(values) < period:
        return None
    seed = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    current = seed
    for value in values[period:]:
        current = (value - current) * alpha + current
    return current


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI of the last value in ``closes``.

    Returns ``None`` when there is not enough data.
    """
    if period < 1 or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
