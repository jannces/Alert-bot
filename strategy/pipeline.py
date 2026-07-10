"""Per-signal pipeline: validate → dedup → screenshot → annotate → notify →
persist.

The scan engine hands over fully built candidates; this pipeline runs the
final strict validation as the last gate, guards against duplicates, renders
the annotated TradingView screenshot, delivers the Telegram alert, and
records the complete signal snapshot (or the near-miss rejection).

The pipeline is defensive end to end: any unexpected error is logged and
contained — a single bad signal can never take the scanner down.
"""

from __future__ import annotations

import asyncio
import logging

from config.settings import Settings
from database.repository import SignalRepository
from screenshots.capture import ScreenshotService
from strategy.models import TamadSetup
from strategy.validation import FinalValidator
from telegram.bot import TelegramNotifier, build_alert_message
from tradingview.links import chart_link

logger = logging.getLogger(__name__)


class SignalPipeline:
    """Owns the full processing lifecycle of one detected candidate."""

    def __init__(
        self,
        settings: Settings,
        validator: FinalValidator,
        repository: SignalRepository,
        screenshots: ScreenshotService,
        notifier: TelegramNotifier,
    ) -> None:
        self._settings = settings
        self._validator = validator
        self._repository = repository
        self._screenshots = screenshots
        self._notifier = notifier
        # Browser captures are serialized: one Chromium at a time.
        self._capture_lock = asyncio.Lock()
        self.processed = 0
        self.sent = 0
        self.rejected = 0
        self.duplicates = 0

    async def aclose(self) -> None:
        await self._screenshots.aclose()
        await self._notifier.aclose()
        self._repository.close()

    def stats(self) -> dict:
        return {
            "processed": self.processed,
            "sent": self.sent,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
        }

    async def process(self, setup: TamadSetup) -> None:
        """Run one candidate through the complete pipeline. Never raises."""
        self.processed += 1
        try:
            await self._process(setup)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one signal must never kill the scanner
            logger.exception(
                "unexpected error processing %s %s %s",
                setup.direction.value,
                setup.symbol,
                setup.timeframe_label,
            )

    async def _process(self, setup: TamadSetup) -> None:
        # Final strict validation: every mandatory rule, recomputed from the
        # raw candle data. Any failure rejects the setup.
        report = self._validator.validate(setup)
        if not report.passed:
            self.rejected += 1
            logger.info(
                "REJECTED %s %s %s: %s",
                setup.direction.value,
                setup.pair,
                setup.timeframe_label,
                report.summary(),
            )
            self._repository.record_rejection(setup, report)
            return

        # Duplicate guard: the key is claimed atomically before sending, so a
        # setup can be alerted at most once — ever, across restarts.
        if not self._repository.reserve_signal(setup, report):
            self.duplicates += 1
            logger.info(
                "duplicate suppressed: %s %s %s (candle %d)",
                setup.direction.value,
                setup.pair,
                setup.timeframe_label,
                setup.candle3.open_time_ms,
            )
            return

        async with self._capture_lock:
            shot = await self._screenshots.render(setup)

        if shot is None and self._settings.screenshots.on_failure == "skip_alert":
            logger.error(
                "screenshot failed and on_failure=skip_alert: NOT alerting %s %s %s",
                setup.direction.value,
                setup.pair,
                setup.timeframe_label,
            )
            self._repository.release_signal(setup.dedup_key)
            self._repository.record_rejection(setup, report)
            return
        if shot is None:
            logger.warning(
                "screenshot failed for %s; sending text-only alert", setup.pair
            )

        message = build_alert_message(
            setup,
            report,
            chart_link(setup, self._settings.exchange.tv_prefix),
            screenshot_ok=shot is not None,
        )
        delivered = await self._notifier.send_alert(
            message, shot.image if shot else None
        )
        if delivered:
            self.sent += 1
            self._repository.mark_sent(
                setup.dedup_key,
                screenshot_path=shot.file_path if shot else None,
                screenshot_attached=shot is not None,
            )
            logger.info(
                "ALERT SENT: %s %s %s entry=%s sl=%s tp2=%s tp3=%s",
                setup.direction.value,
                setup.pair,
                setup.timeframe_label,
                setup.entry,
                setup.stop_loss,
                setup.tp2,
                setup.tp3,
            )
        else:
            # Telegram is down: release the reservation so the signal is not
            # silently swallowed (a later re-detection of the same bar could
            # still deliver it within the freshness window).
            self._repository.release_signal(setup.dedup_key)
            logger.error(
                "Telegram delivery failed for %s %s %s; reservation released",
                setup.direction.value,
                setup.pair,
                setup.timeframe_label,
            )
