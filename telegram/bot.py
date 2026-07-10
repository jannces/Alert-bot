"""Telegram notifications via the official Bot API.

The Bot API is called directly over HTTPS (no third-party bot framework) so
this package can keep the ``telegram/`` name required by the project layout
without colliding with the ``python-telegram-bot`` import namespace.

This module only ever SENDS notifications. It never receives commands and it
never talks to any exchange.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import timezone

import httpx

from config.settings import TelegramSettings
from strategy.models import Direction, TamadSetup

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"


def format_price(value: float) -> str:
    """Format a crypto price without scientific notation or noise zeros."""
    if value >= 1000:
        text = f"{value:,.2f}"
    elif value >= 1:
        text = f"{value:.4f}"
    else:
        text = f"{value:.10f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def build_alert_message(setup: TamadSetup, *, market: str, screenshot_ok: bool) -> str:
    """Render the Telegram alert exactly as specified, in HTML parse mode."""
    if setup.direction is Direction.SHORT:
        level_name = "Resistance"
        color1, color2, color3 = "Green", "Red", "Green"
    else:
        level_name = "Support"
        color1, color2, color3 = "Red", "Green", "Red"

    detected = setup.detected_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    pair = html.escape(setup.pair)
    sr_type = html.escape(setup.sr_type.replace("_", " "))

    lines = [
        "🚨 <b>TAMAD STRATEGY</b>",
        "",
        f"<b>Exchange:</b> MEXC ({html.escape(market)})",
        f"<b>Pair:</b> {pair}",
        f"<b>Direction:</b> {setup.direction.value}",
        f"<b>Timeframe:</b> {setup.timeframe_label}",
        "",
        f"<b>Entry:</b> {format_price(setup.entry)}",
        f"<b>Stop Loss:</b> {format_price(setup.stop_loss)}",
        f"<b>TP2 (2R):</b> {format_price(setup.tp2)}",
        f"<b>TP3 (3R):</b> {format_price(setup.tp3)}",
        "<b>Risk Reward:</b> 1:2 / 1:3",
        "",
        f"<b>Time Detected:</b> {detected}",
        "",
        "<b>Reason:</b>",
        f"✅ {color1} Candle 1",
        f"✅ {color2} Candle 2",
        "✅ Equal Closing Price",
        f"✅ Third Candle respected {level_name}",
        f"✅ Pattern formed at {level_name} ({sr_type})",
        "✅ Candle 3 Closed",
    ]
    if not screenshot_ok:
        lines += ["", "⚠️ Chart screenshot unavailable — verify on TradingView."]
    return "\n".join(lines)


class TelegramNotifier:
    """Sends alert messages (with chart screenshots) to a single chat."""

    def __init__(self, settings: TelegramSettings) -> None:
        if not settings.bot_token or not settings.chat_id:
            raise ValueError(
                "Telegram bot_token and chat_id must be configured "
                "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"
            )
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_alert(self, text: str, screenshot: bytes | None) -> bool:
        """Send the alert, attaching the screenshot when available.

        Returns True on success. Failures are retried with exponential
        backoff and logged; they never raise into the caller.
        """
        if screenshot is not None:
            sent = await self._call(
                "sendPhoto",
                data={
                    "chat_id": self._settings.chat_id,
                    "caption": text,
                    "parse_mode": "HTML",
                },
                files={"photo": ("chart.png", screenshot, "image/png")},
            )
            if sent:
                return True
            logger.error("sendPhoto failed after retries; falling back to text-only")
        return await self._call(
            "sendMessage",
            data={
                "chat_id": self._settings.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    async def _call(self, method: str, *, data: dict, files: dict | None = None) -> bool:
        url = f"{_API_BASE}/bot{self._settings.bot_token}/{method}"
        for attempt in range(1, self._settings.retries + 1):
            try:
                response = await self._client.post(url, data=data, files=files)
                body = response.json()
                if response.status_code == 200 and body.get("ok"):
                    return True
                # Honor Telegram rate limiting.
                retry_after = (body.get("parameters") or {}).get("retry_after")
                logger.warning(
                    "Telegram %s attempt %d failed: HTTP %s %s",
                    method,
                    attempt,
                    response.status_code,
                    body.get("description", ""),
                )
                delay = float(retry_after) if retry_after else 2.0**attempt
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Telegram %s attempt %d error: %s", method, attempt, exc)
                delay = 2.0**attempt
            if attempt < self._settings.retries:
                await asyncio.sleep(delay)
        logger.error("Telegram %s failed after %d attempts", method, self._settings.retries)
        return False
