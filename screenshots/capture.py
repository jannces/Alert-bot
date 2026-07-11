"""TradingView chart capture + Python-drawn annotation (D1).

``ScreenshotService`` is the pipeline-facing façade: given a validated
setup it captures the owner's saved TradingView layout with Playwright,
calibrates the pixel↔price mapping, draws the annotations with Pillow
(:mod:`screenshots.annotate`), persists the PNG to disk, and returns both
the bytes (for Telegram) and the file path (for the signal snapshot).

Failures never raise into the alert pipeline: ``render`` returns ``None``
and the pipeline applies the configured ``on_failure`` policy.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from config.settings import ScreenshotSettings
from screenshots import annotate
from screenshots.annotate import ChartCalibration
from screenshots.calibration import calibrate
from strategy.models import TamadSetup

logger = logging.getLogger(__name__)

_DEVICE_SCALE = 2.0


@dataclass(frozen=True, slots=True)
class RenderedScreenshot:
    """Final annotated chart image, ready for Telegram + the database."""

    image: bytes
    file_path: str | None


@dataclass(frozen=True, slots=True)
class RawCapture:
    image: bytes
    calibration: ChartCalibration | None


class ScreenshotProvider(abc.ABC):
    """Captures a raw chart image (plus calibration when possible)."""

    @abc.abstractmethod
    async def capture(self, tv_symbol: str, setup: TamadSetup) -> RawCapture | None: ...

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        return None


class DisabledProvider(ScreenshotProvider):
    async def capture(self, tv_symbol: str, setup: TamadSetup) -> RawCapture | None:
        return None


class PlaywrightProvider(ScreenshotProvider):
    """Headless-Chromium capture of the owner's saved TradingView layout."""

    def __init__(self, settings: ScreenshotSettings) -> None:
        self._cfg = settings.playwright

    async def capture(self, tv_symbol: str, setup: TamadSetup) -> RawCapture | None:
        for attempt in range(1, self._cfg.retries + 2):
            try:
                return await asyncio.wait_for(
                    self._capture_once(tv_symbol, setup),
                    timeout=self._cfg.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - never break the pipeline
                logger.warning(
                    "screenshot attempt %d for %s failed: %s", attempt, tv_symbol, exc
                )
        logger.error("all screenshot attempts failed for %s", tv_symbol)
        return None

    async def _capture_once(self, tv_symbol: str, setup: TamadSetup) -> RawCapture:
        # Imported lazily so the dependency is only needed when this
        # provider is actually configured.
        from playwright.async_api import async_playwright

        separator = "&" if "?" in self._cfg.chart_url else "?"
        url = (
            f"{self._cfg.chart_url}{separator}"
            f"symbol={quote(tv_symbol)}&interval={quote(setup.tv_interval)}"
        )
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                executable_path=self._cfg.executable_path or None,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                storage = self._cfg.storage_state_path
                context = await browser.new_context(
                    viewport={
                        "width": self._cfg.viewport.width,
                        "height": self._cfg.viewport.height,
                    },
                    device_scale_factor=_DEVICE_SCALE,
                    storage_state=storage if storage and Path(storage).exists() else None,
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_selector("canvas", timeout=30_000)
                # Let TradingView stream data and finish painting.
                await page.wait_for_timeout(int(self._cfg.render_wait_seconds * 1000))

                chart = page.locator(".chart-container").first
                pane_box = await chart.bounding_box() if await chart.count() else None

                calibration = None
                if pane_box is not None:
                    calibration = await calibrate(page, pane_box, setup, _DEVICE_SCALE)
                    # Park the cursor so no crosshair pollutes the screenshot.
                    await page.mouse.move(1, 1)
                    await page.wait_for_timeout(150)

                if pane_box is not None:
                    image = await chart.screenshot(type="png")
                else:
                    image = await page.screenshot(type="png")
                return RawCapture(image=image, calibration=calibration)
            finally:
                await browser.close()


class ScreenshotService:
    """Capture → annotate → persist. The pipeline's single entry point."""

    def __init__(self, settings: ScreenshotSettings, tv_prefix: str) -> None:
        self._settings = settings
        self._tv_prefix = tv_prefix
        self._provider: ScreenshotProvider = (
            PlaywrightProvider(settings)
            if settings.provider == "playwright"
            else DisabledProvider()
        )

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def render(self, setup: TamadSetup) -> RenderedScreenshot | None:
        tv_symbol = f"{self._tv_prefix}:{setup.symbol}"
        raw = await self._provider.capture(tv_symbol, setup)
        if raw is None:
            return None

        try:
            image = annotate.render(
                raw.image, setup, raw.calibration, self._settings.annotations
            )
        except Exception:  # noqa: BLE001 - a bad overlay must not lose the chart
            logger.exception("annotation failed for %s; using raw capture", tv_symbol)
            image = raw.image

        return RenderedScreenshot(image=image, file_path=self._persist(setup, image))

    def _persist(self, setup: TamadSetup, image: bytes) -> str | None:
        try:
            out_dir = Path(self._settings.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = setup.detected_at.strftime("%Y%m%d_%H%M%S")
            safe_pair = re.sub(r"[^A-Za-z0-9]+", "", setup.pair)
            name = (
                f"{stamp}_{safe_pair}_{setup.timeframe_minutes}m_"
                f"{setup.direction.value}.png"
            )
            path = out_dir / name
            path.write_bytes(image)
            return str(path)
        except OSError:
            logger.exception("could not persist screenshot for %s", setup.symbol)
            return None
