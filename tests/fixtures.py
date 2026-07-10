"""Shared test data: rule-perfect Tamad setups used across the test suite."""

from __future__ import annotations

from datetime import datetime, timezone

from strategy.models import Candle, Direction, TamadSetup

TF_MINUTES = 15
TF_MS = TF_MINUTES * 60_000
T0 = 1_760_000_000_000  # arbitrary bar-open time (ms)

# Candle 3 close time, and a "now" a few seconds after it.
CANDLE3_CLOSE_MS = T0 + 3 * TF_MS
NOW_MS = CANDLE3_CLOSE_MS + 5_000


def short_candles() -> tuple[Candle, Candle, Candle]:
    """A rule-perfect SHORT pattern.

    c1 green closes 110.00, c2 red closes 110.02 (0.018% apart → equal),
    resistance (strict) = 110.00, c3 green wicks to 112 but closes 109.50.
    Stop = highest wick 113. Risk = 3.5 → TP2 102.5, TP3 99.0.
    """
    c1 = Candle(T0, 100.0, 111.0, 99.0, 110.0)
    c2 = Candle(T0 + TF_MS, 112.0, 113.0, 109.5, 110.02)
    c3 = Candle(T0 + 2 * TF_MS, 105.0, 112.0, 104.9, 109.5)
    return c1, c2, c3


def long_candles() -> tuple[Candle, Candle, Candle]:
    """A rule-perfect LONG pattern (mirror of the SHORT one)."""
    c1 = Candle(T0, 120.0, 121.0, 109.0, 110.0)
    c2 = Candle(T0 + TF_MS, 108.0, 110.5, 107.0, 110.02)
    c3 = Candle(T0 + 2 * TF_MS, 115.0, 115.1, 108.0, 110.5)
    return c1, c2, c3


def make_short_setup(**overrides) -> TamadSetup:
    c1, c2, c3 = short_candles()
    fields = dict(
        exchange="MEXC",
        symbol="BTCUSDT.P",
        timeframe_minutes=TF_MINUTES,
        direction=Direction.SHORT,
        candle1=c1,
        candle2=c2,
        candle3=c3,
        level=110.0,
        sr_type="swing_high",
        sr_level=110.05,
        entry=109.5,
        stop_loss=113.0,
        tp2=102.5,
        tp3=99.0,
        detected_at=datetime.fromtimestamp(CANDLE3_CLOSE_MS / 1000, tz=timezone.utc),
        raw_payload={},
    )
    fields.update(overrides)
    return TamadSetup(**fields)


def short_payload_dict(secret: str = "test-secret") -> dict:
    """A valid webhook payload matching :func:`make_short_setup`."""
    c1, c2, c3 = short_candles()
    return {
        "secret": secret,
        "strategy": "TAMAD",
        "version": 1,
        "exchange": "MEXC",
        "symbol": "BTCUSDT.P",
        "timeframe": "15",
        "direction": "SHORT",
        "level": 110.0,
        "sr_type": "swing_high",
        "sr_level": 110.05,
        "candles": [
            {"t": c.open_time_ms, "o": c.open, "h": c.high, "l": c.low, "c": c.close}
            for c in (c1, c2, c3)
        ],
        "entry": 109.5,
        "sl": 113.0,
        "tp2": 102.5,
        "tp3": 99.0,
        "detected_at_ms": CANDLE3_CLOSE_MS,
    }
