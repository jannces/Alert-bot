"""Tests for the SQLite repository: dedup guard, snapshots, scan state."""

from __future__ import annotations

import json

from database.repository import SignalRepository
from strategy.models import Direction
from tests.fixtures import make_short_setup, passing_report


def make_repo(tmp_path) -> SignalRepository:
    return SignalRepository(tmp_path / "test.sqlite3")


class TestDuplicateGuard:
    def test_first_reservation_succeeds(self, tmp_path):
        repo = make_repo(tmp_path)
        assert repo.reserve_signal(make_short_setup(), passing_report()) is True

    def test_same_setup_is_a_duplicate(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()
        assert repo.reserve_signal(setup, passing_report()) is True
        assert repo.reserve_signal(setup, passing_report()) is False

    def test_duplicate_guard_survives_reopen(self, tmp_path):
        setup = make_short_setup()
        repo = make_repo(tmp_path)
        assert repo.reserve_signal(setup, passing_report()) is True
        repo.close()

        reopened = make_repo(tmp_path)
        assert reopened.reserve_signal(setup, passing_report()) is False

    def test_different_direction_is_not_a_duplicate(self, tmp_path):
        repo = make_repo(tmp_path)
        assert repo.reserve_signal(make_short_setup(), passing_report()) is True
        assert (
            repo.reserve_signal(
                make_short_setup(direction=Direction.LONG), passing_report()
            )
            is True
        )

    def test_release_allows_retry_only_when_unsent(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()

        assert repo.reserve_signal(setup, passing_report()) is True
        repo.release_signal(setup.dedup_key)
        assert repo.reserve_signal(setup, passing_report()) is True  # claimable again

        repo.mark_sent(setup.dedup_key, screenshot_path="x.png", screenshot_attached=True)
        repo.release_signal(setup.dedup_key)  # must NOT delete a sent signal
        assert repo.reserve_signal(setup, passing_report()) is False


class TestSignalSnapshot:
    def test_full_snapshot_is_stored(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()
        repo.reserve_signal(setup, passing_report())
        repo.mark_sent(
            setup.dedup_key,
            screenshot_path="screenshots/out/test.png",
            screenshot_attached=True,
        )

        row = repo._conn.execute(
            "SELECT pair, timeframe_minutes, direction, "
            "c1_close, c2_close, c3_close, c3_open_time_ms, "
            "entry, stop_loss, risk, tp2, tp3, sr_kind, sr_price, "
            "screenshot_path, validation_json FROM signals"
        ).fetchone()
        assert row[0:3] == ("BTCUSDT", 15, "SHORT")
        assert row[3:6] == (110.0, 110.02, 109.5)  # OHLC snapshot (closes)
        assert row[6] == setup.candle3.open_time_ms
        assert row[7:12] == (109.5, 113.0, 3.5, 102.5, 99.0)
        assert row[12:14] == ("swing_high", 110.05)
        assert row[14] == "screenshots/out/test.png"
        assert json.loads(row[15])  # serialized validation report

    def test_sent_count_tracks_marked_signals(self, tmp_path):
        repo = make_repo(tmp_path)
        setup = make_short_setup()
        repo.reserve_signal(setup, passing_report())
        assert repo.sent_signal_count() == 0
        repo.mark_sent(setup.dedup_key, screenshot_path=None, screenshot_attached=False)
        assert repo.sent_signal_count() == 1


class TestRejections:
    def test_near_miss_rejection_stores_rules_and_candles(self, tmp_path):
        from config.settings import Settings
        from strategy.validation import FinalValidator
        from tests.fixtures import NOW_MS

        repo = make_repo(tmp_path)
        setup = make_short_setup(sr=None)  # middle of a range → near-miss
        report = FinalValidator(Settings(), now_ms=lambda: NOW_MS).validate(setup)
        assert not report.passed
        repo.record_rejection(setup, report)

        row = repo._conn.execute(
            "SELECT pair, timeframe_minutes, direction, passed_rules, "
            "failed_rules, failure_reason, candles_json FROM rejections"
        ).fetchone()
        assert row[0:3] == ("BTCUSDT", 15, "SHORT")
        assert "equal_close" in json.loads(row[3])
        assert json.loads(row[4]) == ["sr_present"]
        assert "sr_present" in row[5]
        candles = json.loads(row[6])
        assert len(candles) == 3 and candles[2]["c"] == 109.5
        assert repo.rejection_count() == 1


class TestScanState:
    def test_round_trip(self, tmp_path):
        repo = make_repo(tmp_path)
        assert repo.load_scan_state(15) == {}
        repo.set_scan_state("BTC_USDT", 15, 1000)
        repo.set_scan_state("BTC_USDT", 15, 2000)  # upsert
        repo.set_scan_state("ETH_USDT", 60, 3000)
        assert repo.load_scan_state(15) == {"BTC_USDT": 2000}
        assert repo.load_scan_state(60) == {"ETH_USDT": 3000}

    def test_survives_reopen(self, tmp_path):
        repo = make_repo(tmp_path)
        repo.set_scan_state("BTC_USDT", 15, 1000)
        repo.close()
        assert make_repo(tmp_path).load_scan_state(15) == {"BTC_USDT": 1000}
