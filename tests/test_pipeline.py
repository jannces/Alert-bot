"""End-to-end pipeline tests with a real validator/repository and stubbed IO."""

from __future__ import annotations

import pytest

from config.settings import Settings
from database.repository import SignalRepository
from screenshots.capture import ScreenshotProvider
from strategy.pipeline import SignalPipeline
from strategy.validation import FinalValidator
from tradingview.webhook_handler import AlertPayload
from tests.fixtures import NOW_MS, short_payload_dict


class ScreenshotStub(ScreenshotProvider):
    def __init__(self, image: bytes | None) -> None:
        self.image = image
        self.calls: list[tuple[str, str]] = []

    async def capture(self, tv_symbol: str, interval: str) -> bytes | None:
        self.calls.append((tv_symbol, interval))
        return self.image


class NotifierStub:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, bytes | None]] = []

    async def send_alert(self, text: str, screenshot: bytes | None) -> bool:
        self.sent.append((text, screenshot))
        return self.ok

    async def aclose(self) -> None:
        return None


def make_pipeline(tmp_path, *, image: bytes | None = b"png", notifier_ok: bool = True,
                  on_failure: str = "send_without_image"):
    settings = Settings()
    settings.screenshots.on_failure = on_failure
    repo = SignalRepository(tmp_path / "pipeline.sqlite3")
    screenshots = ScreenshotStub(image)
    notifier = NotifierStub(notifier_ok)
    pipeline = SignalPipeline(
        settings=settings,
        validator=FinalValidator(settings, now_ms=lambda: NOW_MS),
        repository=repo,
        screenshots=screenshots,
        notifier=notifier,
    )
    return pipeline, repo, screenshots, notifier


def payload() -> AlertPayload:
    return AlertPayload.model_validate(short_payload_dict())


@pytest.mark.asyncio
async def test_valid_setup_sends_one_alert_with_screenshot(tmp_path):
    pipeline, repo, screenshots, notifier = make_pipeline(tmp_path)
    await pipeline._process(payload())

    assert screenshots.calls == [("MEXC:BTCUSDT.P", "15")]
    assert len(notifier.sent) == 1
    text, image = notifier.sent[0]
    assert "TAMAD STRATEGY" in text and image == b"png"
    assert repo.sent_signal_count() == 1


@pytest.mark.asyncio
async def test_duplicate_alert_is_suppressed(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(tmp_path)
    await pipeline._process(payload())
    await pipeline._process(payload())

    assert len(notifier.sent) == 1
    assert repo.sent_signal_count() == 1


@pytest.mark.asyncio
async def test_invalid_setup_is_rejected_and_logged(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(tmp_path)
    bad = short_payload_dict()
    bad["tp2"] = 100.0  # tampered target — recomputation must catch it
    await pipeline._process(AlertPayload.model_validate(bad))

    assert not notifier.sent
    assert repo.rejection_count() == 1
    assert repo.sent_signal_count() == 0


@pytest.mark.asyncio
async def test_screenshot_failure_sends_text_alert_by_default(tmp_path):
    pipeline, _, _, notifier = make_pipeline(tmp_path, image=None)
    await pipeline._process(payload())

    assert len(notifier.sent) == 1
    text, image = notifier.sent[0]
    assert image is None
    assert "⚠️" in text


@pytest.mark.asyncio
async def test_screenshot_failure_skips_alert_in_strict_mode(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(
        tmp_path, image=None, on_failure="skip_alert"
    )
    await pipeline._process(payload())

    assert not notifier.sent
    assert repo.sent_signal_count() == 0
    assert repo.rejection_count() == 1


@pytest.mark.asyncio
async def test_telegram_failure_releases_reservation(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(tmp_path, notifier_ok=False)
    await pipeline._process(payload())
    assert repo.sent_signal_count() == 0

    # A replayed webhook can retry after the failure.
    notifier.ok = True
    await pipeline._process(payload())
    assert repo.sent_signal_count() == 1
