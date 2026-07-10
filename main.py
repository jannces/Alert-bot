"""Tamad Strategy scanner — entrypoint.

Wires the components together and serves the TradingView webhook 24/7:

    TradingView (Pine v6 pattern detection, per symbol/timeframe alerts)
        → POST /webhook/tradingview
        → strict re-validation of every Tamad rule
        → duplicate guard (SQLite)
        → TradingView chart screenshot
        → Telegram notification

This process NEVER trades. It holds no exchange credentials and its only
outputs are Telegram messages and log entries.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from config.logging_setup import setup_logging
from config.settings import Settings, load_settings
from database.repository import SignalRepository
from screenshots.capture import create_provider
from strategy.pipeline import SignalPipeline
from strategy.validation import FinalValidator
from telegram.bot import TelegramNotifier
from tradingview.webhook_handler import create_app

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> FastAPI:
    """Compose the pipeline and return the ASGI app with a managed lifespan."""
    pipeline = SignalPipeline(
        settings=settings,
        validator=FinalValidator(settings),
        repository=SignalRepository(settings.database.path),
        screenshots=create_provider(settings.screenshots),
        notifier=TelegramNotifier(settings.telegram),
    )
    app = create_app(settings, pipeline)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await pipeline.start()
        logger.info(
            "Tamad scanner ready — timeframes=%s, tolerance=%s%%, provider=%s",
            settings.scanner.timeframes,
            settings.strategy.equal_close_tolerance_pct,
            settings.screenshots.provider,
        )
        try:
            yield
        finally:
            await pipeline.stop()

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

    if not settings.app.webhook_secret:
        raise SystemExit(
            "WEBHOOK_SECRET is not set — refusing to start with an "
            "unauthenticated webhook endpoint."
        )

    app = build_application(settings)
    uvicorn.run(app, host=settings.app.host, port=settings.app.port, log_config=None)


if __name__ == "__main__":
    main()
