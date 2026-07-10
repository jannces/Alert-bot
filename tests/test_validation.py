"""Tests for the final strict validation gate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from config.settings import Settings
from strategy.models import SRKind, SRLevel
from strategy.validation import FinalValidator
from tests.fixtures import NOW_MS, TF_MS, make_long_setup, make_short_setup


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def validator(settings: Settings) -> FinalValidator:
    return FinalValidator(settings, now_ms=lambda: NOW_MS)


def failed_names(report) -> set[str]:
    return {check.name for check in report.failed_checks}


class TestValidSetup:
    def test_rule_perfect_short_passes_every_check(self, validator):
        report = validator.validate(make_short_setup())
        assert report.passed, report.summary()

    def test_rule_perfect_long_passes_every_check(self, validator):
        report = validator.validate(make_long_setup())
        assert report.passed, report.summary()

    def test_report_contains_all_mandatory_checks(self, validator):
        report = validator.validate(make_short_setup())
        names = {check.name for check in report.checks}
        assert {
            "candle1_color",
            "candle2_color",
            "candle3_color",
            "equal_close",
            "third_candle_rule",
            "sr_present",
            "sr_side",
            "sr_proximity",
            "candle3_closed",
            "signal_fresh",
            "entry_price",
            "stop_loss",
            "tp2",
            "tp3",
        } <= names


class TestRejections:
    def test_tampered_entry_rejected(self, validator):
        report = validator.validate(make_short_setup(entry=109.4))
        assert not report.passed
        assert "entry_price" in failed_names(report)

    def test_tampered_stop_loss_rejected(self, validator):
        report = validator.validate(make_short_setup(stop_loss=112.0))
        assert "stop_loss" in failed_names(report)

    def test_tampered_targets_rejected(self, validator):
        report = validator.validate(make_short_setup(tp2=101.0, tp3=95.0))
        assert {"tp2", "tp3"} <= failed_names(report)

    def test_unclosed_candle3_rejected(self, settings):
        # "now" is one minute before candle 3's close.
        early = FinalValidator(settings, now_ms=lambda: NOW_MS - TF_MS)
        report = early.validate(make_short_setup())
        assert "candle3_closed" in failed_names(report)

    def test_stale_signal_rejected(self, settings):
        late = FinalValidator(
            settings,
            now_ms=lambda: NOW_MS
            + settings.scanner.max_signal_age_seconds * 1000
            + 60_000,
        )
        report = late.validate(make_short_setup())
        assert "signal_fresh" in failed_names(report)

    def test_missing_sr_rejected_as_middle_of_range(self, validator):
        report = validator.validate(make_short_setup(sr=None))
        assert "sr_present" in failed_names(report)

    def test_sr_on_wrong_side_rejected(self, validator):
        # A swing LOW is not resistance for a SHORT.
        report = validator.validate(
            make_short_setup(sr=SRLevel(kind=SRKind.SWING_LOW, price=110.05))
        )
        assert "sr_side" in failed_names(report)

    def test_sr_too_far_from_pattern_rejected(self, validator):
        report = validator.validate(
            make_short_setup(sr=SRLevel(kind=SRKind.SWING_HIGH, price=113.0))
        )
        assert "sr_proximity" in failed_names(report)

    def test_close_through_resistance_rejected(self, validator):
        setup = make_short_setup()
        c3 = replace(setup.candle3, close=110.5)
        report = validator.validate(
            make_short_setup(candle3=c3, entry=110.5, tp2=105.5, tp3=103.0)
        )
        assert "third_candle_rule" in failed_names(report)

    def test_wrong_candle_color_rejected(self, validator):
        setup = make_short_setup()
        red_c3 = replace(setup.candle3, open=110.9, close=109.5)
        report = validator.validate(make_short_setup(candle3=red_c3))
        assert "candle3_color" in failed_names(report)

    def test_non_consecutive_candles_rejected(self, validator):
        setup = make_short_setup()
        gapped = replace(setup.candle3, open_time_ms=setup.candle3.open_time_ms + TF_MS)
        report = validator.validate(make_short_setup(candle3=gapped))
        assert "candle_order" in failed_names(report)

    def test_level_inconsistency_rejected(self, validator):
        report = validator.validate(make_short_setup(level=110.02))
        assert "level_consistency" in failed_names(report)

    def test_midpoint_mode_changes_the_level(self, settings):
        settings.strategy.equal_close.comparison_mode = "midpoint"
        validator = FinalValidator(settings, now_ms=lambda: NOW_MS)
        # strict level (110.0) no longer matches the midpoint (110.01).
        assert "level_consistency" in failed_names(
            validator.validate(make_short_setup())
        )
        assert validator.validate(make_short_setup(level=110.01)).passed


class TestReportSummary:
    def test_summary_names_failed_rules(self, validator):
        report = validator.validate(make_short_setup(entry=1.0))
        assert not report.passed
        assert "entry_price" in report.summary()

    def test_passed_and_failed_names_split(self, validator):
        report = validator.validate(make_short_setup(tp2=1.0))
        assert "tp2" in report.failed_names
        assert "equal_close" in report.passed_names
