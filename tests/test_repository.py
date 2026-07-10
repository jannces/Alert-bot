"""Tests for the SQLite repository, especially the duplicate-alert guard."""

from __future__ import annotations

from database.repository import SignalRepository
from tests.fixtures import make_short_setup


def make_repo(tmp_path) -> SignalRepository:
    return SignalRepository(tmp_path / "test.sqlite3")


class TestDuplicateGuard:
    def test_first_reservation_succeeds(self, tmp_path):
        repo = make_repo(tmp_path)
        assert repo.reserve_signal(make_short_setup()) is True

    def test_same_setup_is_a_duplicate(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()
        assert repo.reserve_signal(setup) is True
        assert repo.reserve_signal(setup) is False

    def test_duplicate_guard_survives_reopen(self, tmp_path):
        setup = make_short_setup()
        repo = make_repo(tmp_path)
        assert repo.reserve_signal(setup) is True
        repo.close()

        reopened = make_repo(tmp_path)
        assert reopened.reserve_signal(setup) is False

    def test_different_direction_is_not_a_duplicate(self, tmp_path):
        from strategy.models import Direction

        repo = make_repo(tmp_path)
        assert repo.reserve_signal(make_short_setup()) is True
        assert repo.reserve_signal(make_short_setup(direction=Direction.LONG)) is True

    def test_release_allows_retry_only_when_unsent(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()

        assert repo.reserve_signal(setup) is True
        repo.release_signal(setup.dedup_key)
        assert repo.reserve_signal(setup) is True  # released → claimable again

        repo.mark_sent(setup.dedup_key, screenshot_attached=True)
        repo.release_signal(setup.dedup_key)  # must NOT delete a sent signal
        assert repo.reserve_signal(setup) is False


class TestLogging:
    def test_sent_count_tracks_marked_signals(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()
        repo.reserve_signal(setup)
        assert repo.sent_signal_count() == 0
        repo.mark_sent(setup.dedup_key, screenshot_attached=False)
        assert repo.sent_signal_count() == 1

    def test_rejections_are_recorded(self, tmp_path):
        repo = make_repo(tmp_path)
        repo.record_rejection(
            exchange="MEXC",
            symbol="BTCUSDT.P",
            timeframe_minutes=15,
            direction="SHORT",
            reasons="third_candle_rule: closed above resistance",
            payload={"example": True},
        )
        assert repo.rejection_count() == 1
