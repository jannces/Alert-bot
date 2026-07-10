"""Final strict validation — the last gate before any Telegram alert.

Every candidate built by the scan engine is re-validated here from the raw
OHLC values of the three candles immediately before the duplicate check.
Nothing computed upstream is trusted: colors, the equal-close rule, the
third-candle rule, the S/R filter, closed-candle confirmation, entry, stop
loss, and both take-profit levels are all recomputed and compared. This is
deliberate defense-in-depth against future bugs anywhere in the engine.

If ANY check fails the setup is rejected, logged as a near-miss rejection,
and no notification is sent.
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
from strategy.tamad_strategy import ComparisonMode

# Relative tolerance when comparing setup fields against values recomputed
# here. This only absorbs float noise — it is NOT a strategy tolerance.
_PRICE_RTOL = 1e-9


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

        # --- candle integrity ------------------------------------------------
        sane = all(c.is_sane() for c in setup.candles)
        add(CheckResult("candle_integrity", sane, "OHLC values are inconsistent"))
        step = setup.timeframe_minutes * 60_000
        consecutive = (
            c2.open_time_ms - c1.open_time_ms == step
            and c3.open_time_ms - c2.open_time_ms == step
        )
        add(
            CheckResult(
                "candle_order",
                consecutive,
                "candles are not consecutive bars of this timeframe",
            )
        )
        if not (sane and consecutive):
            return ValidationReport(tuple(checks))

        # --- rule 1: candle colors -------------------------------------------
        if direction is Direction.SHORT:
            expected = (("candle1_color", c1.is_green, "green"),
                        ("candle2_color", c2.is_red, "red"),
                        ("candle3_color", c3.is_green, "green"))
        else:
            expected = (("candle1_color", c1.is_red, "red"),
                        ("candle2_color", c2.is_green, "green"),
                        ("candle3_color", c3.is_red, "red"))
        for name, ok, want in expected:
            add(
                CheckResult(
                    name,
                    ok,
                    f"{name.replace('_color', '')} is not {want} "
                    "(doji candles are rejected)",
                )
            )

        # --- rule 2: equal closing price ---------------------------------------
        tolerance = cfg.strategy.equal_close.tolerance_percent
        add(
            CheckResult(
                "equal_close",
                rules.equal_close(c1.close, c2.close, tolerance),
                f"candle 1/2 closes differ by more than {tolerance}%",
            )
        )

        # --- rule 3: third candle must respect the level -------------------------
        mode = ComparisonMode(cfg.strategy.equal_close.comparison_mode)
        level = rules.pattern_level(direction, c1.close, c2.close, mode)
        add(
            CheckResult(
                "third_candle_rule",
                rules.third_candle_respects_level(direction, c3, level),
                "candle 3 closed through the level formed by the equal closes",
            )
        )
        add(
            CheckResult(
                "level_consistency",
                _prices_match(setup.level, level),
                f"setup level {setup.level} != recomputed {level}",
            )
        )

        # --- rule 4: meaningful support/resistance --------------------------------
        sr = setup.sr
        add(
            CheckResult(
                "sr_present",
                sr is not None,
                "no meaningful S/R level near the pattern (middle of a range)",
            )
        )
        if sr is not None:
            required_side = "high" if direction is Direction.SHORT else "low"
            add(
                CheckResult(
                    "sr_side",
                    sr.side in (required_side, "any"),
                    f"S/R level {sr.kind.value} is on the wrong side "
                    f"for {direction.value}",
                )
            )
            add(
                CheckResult(
                    "sr_proximity",
                    rules.sr_level_is_meaningful(
                        level,
                        sr.price,
                        cfg.strategy.support_resistance.proximity_percent,
                    ),
                    "pattern did not form at a meaningful S/R level "
                    f"(level {level}, S/R {sr.price})",
                )
            )

        # --- rule 5: candle 3 fully closed, signal fresh ------------------------------
        now_ms = self._now_ms()
        close_ms = c3.close_time_ms(setup.timeframe_minutes)
        add(
            CheckResult(
                "candle3_closed",
                close_ms <= now_ms + cfg.scanner.clock_skew_seconds * 1000,
                "candle 3 has not fully closed yet",
            )
        )
        add(
            CheckResult(
                "signal_fresh",
                now_ms - close_ms <= cfg.scanner.max_signal_age_seconds * 1000,
                f"signal is older than {cfg.scanner.max_signal_age_seconds}s",
            )
        )

        # --- rules 6-8: entry / stop / targets (recomputed cross-check) -------------
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
