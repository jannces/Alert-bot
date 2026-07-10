"""End-to-end pipeline tests with a real validator/repository and stubbed IO."""

from __future__ import annotations

import pytest

from config.settings import Settings
from database.repository import SignalRepository
from screenshots.capture import RenderedScreenshot
from strategy.pipeline import SignalPipeline
from strategy.validation import FinalValidator
from tests.fixtures import NOW_MS, make_short_setup


class ScreenshotServiceStub:
    def __init__(self, shot: RenderedScreenshot | None) -> None:
        self.shot = shot
        self.calls = 0

    async def render(self, setup) -> RenderedScreenshot | None:
        self.calls += 1
        return self.shot

    async def aclose(self) -> None:
        return None


class NotifierStub:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, bytes | None]] = []

    async def send_alert(self, text: str, screenshot: bytes | None) -> bool:
        self.sent.append((text, screenshot))
        return self.ok

    async def aclose(self) -> None:
        return None


def make_pipeline(
    tmp_path,
    *,
    shot: RenderedScreenshot | None = RenderedScreenshot(b"png", "out/x.png"),
    notifier_ok: bool = True,
    on_failure: str = "send_without_image",
):
    settings = Settings()
    settings.screenshots.on_failure = on_failure
    repo = SignalRepository(tmp_path / "pipeline.sqlite3")
    screenshots = ScreenshotServiceStub(shot)
    notifier = NotifierStub(notifier_ok)
    pipeline = SignalPipeline(
        settings=settings,
        validator=FinalValidator(settings, now_ms=lambda: NOW_MS),
        repository=repo,
        screenshots=screenshots,
        notifier=notifier,
    )
    return pipeline, repo, screenshots, notifier


@pytest.mark.asyncio
async def test_valid_setup_sends_one_alert_with_screenshot(tmp_path):
    pipeline, repo, screenshots, notifier = make_pipeline(tmp_path)
    await pipeline.process(make_short_setup())

    assert screenshots.calls == 1
    assert len(notifier.sent) == 1
    text, image = notifier.sent[0]
    assert "TAMAD STRATEGY" in text and image == b"png"
    assert repo.sent_signal_count() == 1
    row = repo._conn.execute("SELECT screenshot_path FROM signals").fetchone()
    assert row[0] == "out/x.png"


@pytest.mark.asyncio
async def test_duplicate_alert_is_suppressed(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(tmp_path)
    await pipeline.process(make_short_setup())
    await pipeline.process(make_short_setup())

    assert len(notifier.sent) == 1
    assert repo.sent_signal_count() == 1
    assert pipeline.duplicates == 1


@pytest.mark.asyncio
async def test_invalid_setup_is_rejected_and_logged(tmp_path):
    pipeline, repo, screenshots, notifier = make_pipeline(tmp_path)
    await pipeline.process(make_short_setup(sr=None))  # middle of a range

    assert not notifier.sent
    assert screenshots.calls == 0  # rejected before any capture
    assert repo.rejection_count() == 1
    assert repo.sent_signal_count() == 0


@pytest.mark.asyncio
async def test_screenshot_failure_sends_text_alert_by_default(tmp_path):
    pipeline, _, _, notifier = make_pipeline(tmp_path, shot=None)
    await pipeline.process(make_short_setup())

    assert len(notifier.sent) == 1
    text, image = notifier.sent[0]
    assert image is None
    assert "⚠️" in text


@pytest.mark.asyncio
async def test_screenshot_failure_skips_alert_in_strict_mode(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(
        tmp_path, shot=None, on_failure="skip_alert"
    )
    await pipeline.process(make_short_setup())

    assert not notifier.sent
    assert repo.sent_signal_count() == 0
    assert repo.rejection_count() == 1


@pytest.mark.asyncio
async def test_telegram_failure_releases_reservation(tmp_path):
    pipeline, repo, _, notifier = make_pipeline(tmp_path, notifier_ok=False)
    await pipeline.process(make_short_setup())
    assert repo.sent_signal_count() == 0

    # A later retry of the same signal can still deliver it.
    notifier.ok = True
    await pipeline.process(make_short_setup())
    assert repo.sent_signal_count() == 1
