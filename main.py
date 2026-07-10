"""Tamad Strategy scanner — entrypoint.

Wires the components together and runs the 24/7 scan loop:

    MEXC public futures API  →  async Python scanner (candle-close boundaries)
    →  Tamad strategy + S/R validation  →  risk calculation  →  duplicate
    check  →  TradingView screenshot (Playwright)  →  Python-drawn overlay
    →  Telegram notification  →  SQLite logging

Python is the only component that decides whether a setup is valid;
TradingView is strictly a charting and screenshot service.

This process NEVER trades. It holds no exchange credentials and its only
outputs are Telegram messages, screenshots, log entries, and database rows.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from config.logging_setup import setup_logging
from config.settings import Settings, load_settings
from database.repository import SignalRepository
from mexc.client import MexcClient
from scanner.engine import ScanEngine
from scanner.scheduler import SweepScheduler
from screenshots.capture import ScreenshotService
from strategy.pipeline import SignalPipeline
from strategy.sr_levels import create_detector
from strategy.validation import FinalValidator
from telegram.bot import TelegramNotifier

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> FastAPI:
    """Compose the scanner and return the ASGI app with a managed lifespan."""
    client = MexcClient(settings.mexc)
    repository = SignalRepository(settings.database.path)
    pipeline = SignalPipeline(
        settings=settings,
        validator=FinalValidator(settings),
        repository=repository,
        screenshots=ScreenshotService(settings.screenshots, settings.exchange.tv_prefix),
        notifier=TelegramNotifier(settings.telegram),
    )
    sr_cfg = settings.strategy.support_resistance
    engine = ScanEngine(
        settings=settings,
        client=client,
        repository=repository,
        sr_detector=create_detector(
            sr_cfg.method, left_bars=sr_cfg.left_bars, right_bars=sr_cfg.right_bars
        ),
        pipeline=pipeline,
    )
    scheduler = SweepScheduler(
        settings.scanner.timeframe_minutes,
        settings.scanner.boundary_settle_seconds,
        engine.sweep,
    )

    app = FastAPI(
        title="Tamad Strategy Scanner", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "last_sweep": engine.last_sweep,
            **pipeline.stats(),
        }

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(scheduler.run(), name="sweep-scheduler")
        logger.info(
            "Tamad scanner ready — timeframes=%s tolerance=%s%% mode=%s sr=%s "
            "screenshots=%s/%s",
            settings.scanner.timeframes,
            settings.strategy.equal_close.tolerance_percent,
            settings.strategy.equal_close.comparison_mode,
            settings.strategy.support_resistance.method,
            settings.screenshots.provider,
            settings.screenshots.annotations,
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.aclose()
            await pipeline.aclose()
            logger.info("Tamad scanner stopped")

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Tamad Strategy scanner")
    parser.add_argument(
        "--config", default="config/config.yaml", help="path to config YAML"
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    setup_logging(settings.logging)

    app = build_application(settings)
    uvicorn.run(app, host=settings.app.host, port=settings.app.port, log_config=None)


if __name__ == "__main__":
    main()
