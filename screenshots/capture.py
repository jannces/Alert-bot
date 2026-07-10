"""TradingView chart screenshot capture.

Two providers are available; both render the actual TradingView chart so the
image in Telegram is the exact chart you would analyze by hand:

``playwright``
    Opens your own TradingView chart layout in headless Chromium and
    screenshots it. Point ``chart_url`` at a saved layout that has the Tamad
    Pine indicator applied — the indicator draws the three highlighted
    candles, the S/R line, entry, stop, TP2 and TP3 directly on the chart, so
    the screenshot contains every required annotation. For private layouts,
    export a Playwright ``storage_state.json`` once after logging in.

``chart_img``
    Uses the chart-img.com TradingView snapshot API (API key required) as a
    lighter-weight alternative when running a browser is not desirable.

Failures never raise into the alert pipeline: ``capture`` returns ``None``
and the pipeline applies the configured ``on_failure`` policy.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

import httpx

from config.settings import ScreenshotSettings

logger = logging.getLogger(__name__)


class ScreenshotProvider(abc.ABC):
    """Captures a chart image for a symbol/interval pair."""

    @abc.abstractmethod
    async def capture(self, tv_symbol: str, interval: str) -> bytes | None:
        """Return PNG bytes, or None when capture fails."""

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        return None


class DisabledProvider(ScreenshotProvider):
    async def capture(self, tv_symbol: str, interval: str) -> bytes | None:
        return None


class PlaywrightProvider(ScreenshotProvider):
    """Headless-Chromium screenshot of a real TradingView chart layout."""

    def __init__(self, settings: ScreenshotSettings) -> None:
        self._cfg = settings.playwright

    async def capture(self, tv_symbol: str, interval: str) -> bytes | None:
        for attempt in range(1, self._cfg.retries + 2):
            try:
                return await asyncio.wait_for(
                    self._capture_once(tv_symbol, interval),
                    timeout=self._cfg.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - never break the pipeline
                logger.warning(
                    "screenshot attempt %d for %s %s failed: %s",
                    attempt,
                    tv_symbol,
                    interval,
                    exc,
                )
        logger.error("all screenshot attempts failed for %s %s", tv_symbol, interval)
        return None

    async def _capture_once(self, tv_symbol: str, interval: str) -> bytes:
        # Imported lazily so the dependency is only needed when this
        # provider is actually configured.
        from playwright.async_api import async_playwright

        separator = "&" if "?" in self._cfg.chart_url else "?"
        url = (
            f"{self._cfg.chart_url}{separator}"
            f"symbol={quote(tv_symbol)}&interval={quote(interval)}"
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
                    device_scale_factor=2,
                    storage_state=storage if storage and Path(storage).exists() else None,
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_selector("canvas", timeout=30_000)
                # Give TradingView time to stream data and paint the chart
                # (including the Pine indicator's pattern drawings).
                await page.wait_for_timeout(int(self._cfg.render_wait_seconds * 1000))
                chart = page.locator(".chart-container").first
                if await chart.count():
                    return await chart.screenshot(type="png")
                return await page.screenshot(type="png")
            finally:
                await browser.close()


class ChartImgProvider(ScreenshotProvider):
    """Screenshot via the chart-img.com TradingView snapshot API."""

    def __init__(self, settings: ScreenshotSettings) -> None:
        self._cfg = settings.chart_img
        if not self._cfg.api_key:
            raise ValueError("chart_img provider selected but api_key is empty")
        self._client = httpx.AsyncClient(timeout=self._cfg.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def capture(self, tv_symbol: str, interval: str) -> bytes | None:
        payload = {
            "symbol": tv_symbol,
            "interval": f"{interval}m" if interval.isdigit() else interval,
            "width": self._cfg.width,
            "height": self._cfg.height,
        }
        try:
            response = await self._client.post(
                self._cfg.base_url,
                json=payload,
                headers={"x-api-key": self._cfg.api_key},
            )
            if response.status_code == 200:
                return response.content
            logger.error(
                "chart-img returned HTTP %s: %s",
                response.status_code,
                response.text[:300],
            )
        except httpx.HTTPError as exc:
            logger.error("chart-img request failed: %s", exc)
        return None


def create_provider(settings: ScreenshotSettings) -> ScreenshotProvider:
    if settings.provider == "playwright":
        return PlaywrightProvider(settings)
    if settings.provider == "chart_img":
        return ChartImgProvider(settings)
    return DisabledProvider()
