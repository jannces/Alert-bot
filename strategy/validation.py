"""Final strict validation — the last gate before any Telegram alert.

Every setup received from TradingView is re-validated here from the raw OHLC
values of the three candles. Nothing computed upstream (in Pine Script or in
the webhook payload) is trusted: colors, the equal-close rule, the
third-candle rule, the S/R filter, closed-candle confirmation, entry, stop
loss, and both take-profit levels are all recomputed and compared.

If ANY check fails the setup is rejected and no notification is sent.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from config.settings import Settings
from strategy import tamad_strategy as rules
from strategy.models import (
    CheckResult,
    Direction,
    TamadSetup,
    ValidationReport,
)
from strategy.tamad_strategy import LevelBasis

# Relative tolerance when comparing prices reported by TradingView against
# values recomputed here. This only absorbs float/serialization noise — it is
# NOT a strategy tolerance.
_PRICE_RTOL = 1e-6


def _prices_match(reported: float, recomputed: float) -> bool:
    return math.isclose(reported, recomputed, rel_tol=_PRICE_RTOL, abs_tol=1e-12)


class FinalValidator:
    """Runs the complete mandatory rule set against a :class:`TamadSetup`."""

    def __init__(
        self, settings: Settings, *, now_ms: Callable[[], int] | None = None
    ) -> None:
        self._settings = settings
        # Injectable clock for tests.
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def validate(self, setup: TamadSetup) -> ValidationReport:
        checks: list[CheckResult] = []
        add = checks.append
        cfg = self._settings
        c1, c2, c3 = setup.candles
        direction = setup.direction

        # --- scope: symbol / timeframe -----------------------------------
        add(
            CheckResult(
                "timeframe_allowed",
                setup.timeframe_minutes in cfg.scanner.timeframe_minutes,
                f"timeframe {setup.timeframe_minutes}m not in configured timeframes",
            )
        )
        add(
            CheckResult(
                "symbol_allowed",
                cfg.scanner.symbols.allows(setup.symbol),
                f"symbol {setup.symbol} not in whitelist",
            )
        )
        add(
            CheckResult(
                "usdt_perpetual",
                setup.symbol.endswith(cfg.exchange.symbol_suffix),
                f"symbol {setup.symbol} is not a {cfg.exchange.symbol_suffix} contract",
            )
        )

        # --- candle integrity ---------------------------------------------
        sane = all(c.is_sane() for c in setup.candles)
        add(CheckResult("candle_integrity", sane, "OHLC values are inconsistent"))
        ordered = c1.open_time_ms < c2.open_time_ms < c3.open_time_ms
        add(CheckResult("candle_order", ordered, "candles are not consecutive in time"))
        if not (sane and ordered):
            return ValidationReport(tuple(checks))

        # --- rule 1: candle colors ------------------------------------------
        add(
            CheckResult(
                "candle_colors",
                rules.candle_colors_valid(direction, c1, c2, c3),
                f"colors do not match a {direction.value} pattern "
                "(doji candles are rejected)",
            )
        )

        # --- rule 2: equal closing price ------------------------------------
        tolerance = cfg.strategy.equal_close_tolerance_pct
        add(
            CheckResult(
                "equal_close",
                rules.equal_close(c1.close, c2.close, tolerance),
                f"candle 1/2 closes differ by more than {tolerance}%",
            )
        )

        # --- rule 3: third candle must respect the level --------------------
        basis = LevelBasis(cfg.strategy.level_basis)
        level = rules.pattern_level(direction, c1.close, c2.close, basis)
        add(
            CheckResult(
                "third_candle_rule",
                rules.third_candle_respects_level(direction, c3, level),
                "candle 3 closed through the level formed by the equal closes",
            )
        )
        add(
            CheckResult(
                "level_matches",
                _prices_match(setup.level, level),
                f"reported level {setup.level} != recomputed {level}",
            )
        )

        # --- rule 4: meaningful support/resistance ---------------------------
        add(
            CheckResult(
                "sr_type_allowed",
                setup.sr_type in cfg.strategy.allowed_sr_types,
                f"S/R type {setup.sr_type!r} is not an accepted level type",
            )
        )
        side_ok = True
        if direction is Direction.SHORT and setup.sr_type.endswith("_low"):
            side_ok = False
        if direction is Direction.LONG and setup.sr_type.endswith("_high"):
            side_ok = False
        add(
            CheckResult(
                "sr_side",
                side_ok,
                f"S/R type {setup.sr_type!r} is on the wrong side for {direction.value}",
            )
        )
        add(
            CheckResult(
                "sr_proximity",
                rules.sr_level_is_meaningful(
                    level, setup.sr_level, cfg.strategy.sr_proximity_pct
                ),
                "pattern did not form at a meaningful S/R level "
                f"(level {level}, S/R {setup.sr_level})",
            )
        )

        # --- rule 5: candle 3 fully closed -----------------------------------
        now_ms = self._now_ms()
        close_ms = c3.close_time_ms(setup.timeframe_minutes)
        closed = close_ms <= now_ms + cfg.app.clock_skew_seconds * 1000
        add(
            CheckResult(
                "candle3_closed",
                closed,
                "candle 3 has not fully closed yet",
            )
        )
        fresh = now_ms - close_ms <= cfg.app.max_alert_age_seconds * 1000
        add(
            CheckResult(
                "signal_fresh",
                fresh,
                f"signal is older than {cfg.app.max_alert_age_seconds}s",
            )
        )

        # --- rules 6-8: entry / stop / targets --------------------------------
        levels = rules.compute_trade_levels(direction, c1, c2, c3)
        add(
            CheckResult(
                "entry_price",
                _prices_match(setup.entry, levels.entry),
                f"entry {setup.entry} != candle 3 close {levels.entry}",
            )
        )
        add(
            CheckResult(
                "stop_loss",
                _prices_match(setup.stop_loss, levels.stop_loss),
                f"stop {setup.stop_loss} != 3-candle extreme wick {levels.stop_loss}",
            )
        )
        add(CheckResult("risk_positive", levels.risk > 0, "risk is not positive"))
        add(
            CheckResult(
                "tp2",
                _prices_match(setup.tp2, levels.tp2),
                f"TP2 {setup.tp2} != recomputed 2R target {levels.tp2}",
            )
        )
        add(
            CheckResult(
                "tp3",
                _prices_match(setup.tp3, levels.tp3),
                f"TP3 {setup.tp3} != recomputed 3R target {levels.tp3}",
            )
        )

        return ValidationReport(tuple(checks))
