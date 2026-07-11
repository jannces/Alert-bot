"""The scan engine: turns MEXC candle data into Tamad candidates.

For every (symbol, timeframe) at a candle-close boundary the engine:

1. fetches the recent candle history (rate-limited, bounded concurrency),
2. discards the forming candle and verifies the just-closed bar is present,
3. skips bars that were already evaluated (persistent scan state),
4. applies the pattern pre-filter (candle colors + equal close),
5. for candidates: derives the pattern level, searches for a meaningful
   S/R level, computes entry/stop/targets, and hands the complete
   :class:`TamadSetup` to the pipeline (which runs the final strict
   validation, duplicate check, screenshot, and Telegram delivery).

Python is the only component that decides whether a trade setup is valid.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from config.settings import Settings
from database.repository import SignalRepository
from mexc.client import MexcClient, MexcError, api_symbol_to_tv
from strategy import tamad_strategy as rules
from strategy.models import TamadSetup
from strategy.pipeline import SignalPipeline
from strategy.sr_levels import SRDetector, nearest_level
from strategy.tamad_strategy import ComparisonMode

logger = logging.getLogger(__name__)


class ScanEngine:
    """Sweeps every active MEXC USDT perpetual on each closed candle."""

    def __init__(
        self,
        settings: Settings,
        client: MexcClient,
        repository: SignalRepository,
        sr_detector: SRDetector,
        pipeline: SignalPipeline,
    ) -> None:
        self._settings = settings
        self._client = client
        self._repository = repository
        self._sr_detector = sr_detector
        self._pipeline = pipeline
        self._symbols: list[str] = []
        self._symbols_fetched_at = 0.0
        self._active_timeframes: set[int] = set()
        self.last_sweep: dict[int, str] = {}  # tf minutes -> iso timestamp

    async def sweep(self, timeframes_minutes: list[int], boundary_ms: int) -> None:
        """Scan all symbols for every timeframe that closed at the boundary."""
        symbols = await self._active_symbols()
        if not symbols:
            logger.warning("no symbols to scan — skipping sweep")
            return
        # Shortest timeframe first: its freshness budget is the tightest.
        for tf in sorted(timeframes_minutes):
            if tf in self._active_timeframes:
                # A previous sweep of this timeframe is still draining (rate
                # limits); skipping keeps the backlog bounded. The missed bar
                # stays unprocessed and is logged, never silently mis-scanned.
                logger.warning(
                    "sweep %dm at boundary %d skipped — previous sweep still running",
                    tf,
                    boundary_ms,
                )
                continue
            self._active_timeframes.add(tf)
            started = time.monotonic()
            try:
                await self._sweep_timeframe(tf, boundary_ms, symbols)
            finally:
                self._active_timeframes.discard(tf)
            self.last_sweep[tf] = datetime.now(timezone.utc).isoformat()
            logger.info(
                "sweep %dm done: %d symbols in %.1fs",
                tf,
                len(symbols),
                time.monotonic() - started,
            )

    async def _sweep_timeframe(
        self, tf_minutes: int, boundary_ms: int, symbols: list[str]
    ) -> None:
        last_processed = self._repository.load_scan_state(tf_minutes)
        semaphore = asyncio.Semaphore(self._settings.mexc.max_concurrency)

        async def scan(api_symbol: str) -> None:
            async with semaphore:
                try:
                    await self._scan_symbol(
                        api_symbol,
                        tf_minutes,
                        boundary_ms,
                        last_processed.get(api_symbol),
                    )
                except MexcError as exc:
                    logger.warning("scan %s %dm failed: %s", api_symbol, tf_minutes, exc)
                except Exception:  # noqa: BLE001 - one symbol must never kill a sweep
                    logger.exception("unexpected error scanning %s %dm", api_symbol, tf_minutes)

        await asyncio.gather(*(scan(s) for s in symbols))

    async def _scan_symbol(
        self,
        api_symbol: str,
        tf_minutes: int,
        boundary_ms: int,
        last_processed_ms: int | None,
    ) -> None:
        step = tf_minutes * 60_000
        candle3_open_ms = boundary_ms - step

        # Evaluate each bar at most once — also across restarts.
        if last_processed_ms is not None and last_processed_ms >= candle3_open_ms:
            return

        candles = await self._client.fetch_klines(
            api_symbol, tf_minutes, self._settings.scanner.history_bars
        )
        # Only fully closed candles: drop the forming bar (and anything odd
        # beyond the boundary).
        closed = [c for c in candles if c.open_time_ms <= candle3_open_ms]
        if not closed or closed[-1].open_time_ms != candle3_open_ms:
            logger.debug(
                "%s %dm: just-closed bar not yet published — skipping this bar",
                api_symbol,
                tf_minutes,
            )
            return

        # Mark the bar processed regardless of outcome — "evaluate once".
        self._repository.set_scan_state(api_symbol, tf_minutes, candle3_open_ms)

        if len(closed) < 3:
            return
        c1, c2, c3 = closed[-3], closed[-2], closed[-1]

        strategy_cfg = self._settings.strategy
        equal_cfg = strategy_cfg.equal_close
        near_cfg = strategy_cfg.near_miss
        # Candidacy uses the widest configured bound so near-misses are seen.
        prefilter_tolerance = (
            max(equal_cfg.tolerance_percent, near_cfg.tolerance_percent)
            if near_cfg.enabled
            else equal_cfg.tolerance_percent
        )
        direction = rules.matches_prefilter(c1, c2, c3, prefilter_tolerance)
        if direction is None:
            return  # no pattern — not persisted (near-miss logging boundary)

        level = rules.pattern_level(
            direction,
            c1.close,
            c2.close,
            ComparisonMode(equal_cfg.comparison_mode),
        )
        graded = rules.grade_pattern(
            direction,
            c1,
            c2,
            c3,
            level,
            tolerance_pct=equal_cfg.tolerance_percent,
            near_miss_enabled=near_cfg.enabled,
            near_tolerance_pct=near_cfg.tolerance_percent,
            near_overshoot_pct=near_cfg.overshoot_percent,
        )
        if graded is None:
            return  # beyond even the near-miss allowances
        grade, notes = graded
        sr = None
        if strategy_cfg.support_resistance.enabled:
            side = "high" if direction.value == "SHORT" else "low"
            sr = nearest_level(
                self._sr_detector.find_levels(closed),
                side,
                level,
                strategy_cfg.support_resistance.proximity_percent,
            )
        levels = rules.compute_trade_levels(direction, c1, c2, c3)

        setup = TamadSetup(
            exchange=self._settings.exchange.name,
            symbol=api_symbol_to_tv(api_symbol),
            timeframe_minutes=tf_minutes,
            direction=direction,
            candle1=c1,
            candle2=c2,
            candle3=c3,
            level=level,
            sr=sr,
            entry=levels.entry,
            stop_loss=levels.stop_loss,
            risk=levels.risk,
            tp2=levels.tp2,
            tp3=levels.tp3,
            detected_at=datetime.now(timezone.utc),
            grade=grade,
            notes=notes,
        )
        logger.info(
            "candidate (%s): %s %s %dm (level=%s sr=%s)",
            grade.value,
            direction.value,
            setup.symbol,
            tf_minutes,
            level,
            f"{sr.kind.value}@{sr.price}" if sr else "none",
        )
        await self._pipeline.process(setup)

    async def _active_symbols(self) -> list[str]:
        """Discover active USDT perps, cached and refreshed periodically."""
        age = time.monotonic() - self._symbols_fetched_at
        if self._symbols and age < self._settings.scanner.symbol_refresh_seconds:
            return self._symbols
        try:
            symbols = await self._client.fetch_usdt_perp_symbols()
        except MexcError as exc:
            logger.error("symbol discovery failed (%s); keeping previous list", exc)
            return self._symbols

        cfg = self._settings.scanner.symbols
        if cfg.mode == "whitelist":
            symbols = [
                s
                for s in symbols
                if cfg.allows(api_symbol_to_tv(s))
                or cfg.allows(api_symbol_to_tv(s).removesuffix(".P"))
            ]
        self._symbols = symbols
        self._symbols_fetched_at = time.monotonic()
        return self._symbols
