"""Asynchronous signal pipeline: validate → dedup → screenshot → notify.

One background worker drains a queue of accepted webhook payloads so the
webhook endpoint always responds within TradingView's delivery timeout. The
worker is defensive end to end: any unexpected error is logged and the loop
keeps running — a single bad alert can never take the scanner down.
"""

from __future__ import annotations

import asyncio
import logging

from config.settings import Settings
from database.repository import SignalRepository
from screenshots.capture import ScreenshotProvider
from strategy.models import TamadSetup
from strategy.validation import FinalValidator
from telegram.bot import TelegramNotifier, build_alert_message
from tradingview.webhook_handler import AlertPayload

logger = logging.getLogger(__name__)


class SignalPipeline:
    """Owns the alert queue and the full processing lifecycle of a signal."""

    def __init__(
        self,
        settings: Settings,
        validator: FinalValidator,
        repository: SignalRepository,
        screenshots: ScreenshotProvider,
        notifier: TelegramNotifier,
    ) -> None:
        self._settings = settings
        self._validator = validator
        self._repository = repository
        self._screenshots = screenshots
        self._notifier = notifier
        self._queue: asyncio.Queue[AlertPayload] = asyncio.Queue(maxsize=1000)
        self._worker: asyncio.Task | None = None
        self._processed = 0
        self._sent = 0
        self._rejected = 0

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name="signal-pipeline")
        logger.info("signal pipeline started")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self._screenshots.aclose()
        await self._notifier.aclose()
        self._repository.close()
        logger.info("signal pipeline stopped")

    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "processed": self._processed,
            "sent": self._sent,
            "rejected": self._rejected,
        }

    # -- ingestion --------------------------------------------------------------

    async def enqueue(self, payload: AlertPayload) -> None:
        await self._queue.put(payload)

    def record_malformed(self, data: object, error: str) -> None:
        """Log a structurally invalid webhook payload for debugging."""
        raw = data if isinstance(data, dict) else {"raw": str(data)[:2000]}
        raw = {k: v for k, v in raw.items() if k != "secret"}
        self._repository.record_rejection(
            exchange=str(raw.get("exchange") or "") or None,
            symbol=str(raw.get("symbol") or "") or None,
            timeframe_minutes=None,
            direction=str(raw.get("direction") or "") or None,
            reasons=f"malformed payload: {error[:1000]}",
            payload=raw,
        )

    # -- processing ---------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._process(payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must survive anything
                logger.exception(
                    "unexpected error processing %s %s alert",
                    payload.symbol,
                    payload.timeframe,
                )
            finally:
                self._processed += 1
                self._queue.task_done()

    async def _process(self, payload: AlertPayload) -> None:
        setup = payload.to_setup()

        # Final strict validation: every mandatory rule, recomputed from the
        # raw candle data. Any failure rejects the setup.
        report = self._validator.validate(setup)
        if not report.passed:
            self._rejected += 1
            reasons = report.summary()
            logger.info(
                "REJECTED %s %s %s: %s",
                setup.direction.value,
                setup.symbol,
                setup.timeframe_label,
                reasons,
            )
            self._repository.record_rejection(
                exchange=setup.exchange,
                symbol=setup.symbol,
                timeframe_minutes=setup.timeframe_minutes,
                direction=setup.direction.value,
                reasons=reasons,
                payload=setup.raw_payload,
            )
            return

        # Duplicate guard: the key is claimed atomically before sending, so a
        # setup can be alerted at most once — ever, across restarts.
        if not self._repository.reserve_signal(setup):
            logger.info(
                "duplicate suppressed: %s %s %s (candle %d)",
                setup.direction.value,
                setup.symbol,
                setup.timeframe_label,
                setup.candle3.open_time_ms,
            )
            return

        await self._send(setup)

    async def _send(self, setup: TamadSetup) -> None:
        tv_symbol = f"{self._settings.exchange.tv_prefix}:{setup.symbol}"
        screenshot = await self._screenshots.capture(tv_symbol, setup.tv_interval)

        if screenshot is None:
            if self._settings.screenshots.on_failure == "skip_alert":
                logger.error(
                    "screenshot failed and on_failure=skip_alert: "
                    "NOT alerting %s %s %s",
                    setup.direction.value,
                    setup.symbol,
                    setup.timeframe_label,
                )
                self._repository.release_signal(setup.dedup_key)
                self._repository.record_rejection(
                    exchange=setup.exchange,
                    symbol=setup.symbol,
                    timeframe_minutes=setup.timeframe_minutes,
                    direction=setup.direction.value,
                    reasons="screenshot capture failed (on_failure=skip_alert)",
                    payload=setup.raw_payload,
                )
                return
            logger.warning(
                "screenshot failed for %s; sending text-only alert", setup.symbol
            )

        message = build_alert_message(
            setup,
            market=self._settings.exchange.market,
            screenshot_ok=screenshot is not None,
        )
        sent = await self._notifier.send_alert(message, screenshot)
        if sent:
            self._sent += 1
            self._repository.mark_sent(
                setup.dedup_key, screenshot_attached=screenshot is not None
            )
            logger.info(
                "ALERT SENT: %s %s %s entry=%s sl=%s tp2=%s tp3=%s",
                setup.direction.value,
                setup.symbol,
                setup.timeframe_label,
                setup.entry,
                setup.stop_loss,
                setup.tp2,
                setup.tp3,
            )
        else:
            # Telegram is down: release the reservation so the operator can
            # replay the webhook without the dedup guard swallowing it.
            self._repository.release_signal(setup.dedup_key)
            logger.error(
                "Telegram delivery failed for %s %s %s; reservation released",
                setup.direction.value,
                setup.symbol,
                setup.timeframe_label,
            )
