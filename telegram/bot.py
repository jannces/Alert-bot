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
from strategy.models import TamadSetup, ValidationReport

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"

# The seven headline rules shown in the alert checklist, mapped to the
# validator's granular check names (a display grouping, not a relaxation:
# an alert only exists when EVERY validator check passed).
_CHECKLIST: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Candle 1 Color", ("candle1_color",)),
    ("Candle 2 Color", ("candle2_color",)),
    ("Candle 3 Rule", ("candle3_color", "third_candle_rule", "candle3_closed")),
    ("Equal Close", ("equal_close",)),
    ("Support / Resistance", ("sr_present", "sr_side", "sr_proximity")),
    ("Stop Loss", ("stop_loss", "risk_positive")),
    ("Take Profit", ("tp2", "tp3")),
)


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


def build_alert_message(
    setup: TamadSetup,
    report: ValidationReport,
    chart_url: str,
    *,
    screenshot_ok: bool,
) -> str:
    """Render the Telegram alert (HTML parse mode)."""
    detected = setup.detected_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    passed = set(report.passed_names)
    all_names = {check.name for check in report.checks}
    checklist = []
    total = 0
    ok_count = 0
    for label, check_names in _CHECKLIST:
        # Rows whose rules were not evaluated at all (e.g. the S/R filter is
        # disabled in config) are omitted rather than shown as failures.
        relevant = [name for name in check_names if name in all_names]
        if not relevant:
            continue
        ok = all(name in passed for name in relevant)
        total += 1
        ok_count += ok
        checklist.append(f"{'✅' if ok else '❌'} {label}")

    lines = [
        "🚨 <b>TAMAD STRATEGY</b>",
        "",
        f"<b>Pair:</b> {html.escape(setup.pair)}",
        f"<b>Direction:</b> {setup.direction.value}",
        f"<b>Timeframe:</b> {setup.timeframe_label}",
        "",
        f"<b>Entry:</b> {format_price(setup.entry)}",
        f"<b>Stop Loss:</b> {format_price(setup.stop_loss)}",
        f"<b>Take Profit 2R:</b> {format_price(setup.tp2)}",
        f"<b>Take Profit 3R:</b> {format_price(setup.tp3)}",
        "<b>Risk Reward:</b> 1:2 / 1:3",
        "",
        f"<b>Detection Time:</b> {detected}",
        "",
        "<b>Validation</b>",
        *checklist,
        "",
        "<b>Confidence</b>",
        f"{ok_count} / {total} Rules Passed",
        "",
        f"📊 <a href=\"{html.escape(chart_url, quote=True)}\">Open live chart on TradingView</a>",
        html.escape(chart_url),
    ]
    if not screenshot_ok:
        lines += ["", "⚠️ Chart screenshot unavailable — open the live chart above."]
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
