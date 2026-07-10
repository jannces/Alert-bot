"""Tests for the final strict validation gate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from config.settings import Settings
from strategy.validation import FinalValidator
from tests.fixtures import NOW_MS, TF_MS, make_short_setup


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

    def test_report_contains_all_mandatory_checks(self, validator):
        report = validator.validate(make_short_setup())
        names = {check.name for check in report.checks}
        assert {
            "candle_colors",
            "equal_close",
            "third_candle_rule",
            "sr_proximity",
            "candle3_closed",
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
        assert not report.passed
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
            now_ms=lambda: NOW_MS + settings.app.max_alert_age_seconds * 1000 + 60_000,
        )
        report = late.validate(make_short_setup())
        assert "signal_fresh" in failed_names(report)

    def test_unknown_sr_type_rejected(self, validator):
        report = validator.validate(make_short_setup(sr_type="round_number"))
        assert "sr_type_allowed" in failed_names(report)

    def test_sr_on_wrong_side_rejected(self, validator):
        # A swing LOW is not resistance for a SHORT.
        report = validator.validate(make_short_setup(sr_type="swing_low"))
        assert "sr_side" in failed_names(report)

    def test_sr_too_far_from_pattern_rejected(self, validator):
        report = validator.validate(make_short_setup(sr_level=113.0))
        assert "sr_proximity" in failed_names(report)

    def test_close_through_resistance_rejected(self, validator):
        setup = make_short_setup()
        c3 = replace(setup.candle3, close=110.5)
        report = validator.validate(
            make_short_setup(candle3=c3, entry=110.5, tp2=105.5, tp3=103.0)
        )
        assert "third_candle_rule" in failed_names(report)

    def test_wrong_timeframe_rejected(self, validator):
        report = validator.validate(make_short_setup(timeframe_minutes=5))
        assert "timeframe_allowed" in failed_names(report)

    def test_non_usdt_perp_symbol_rejected(self, validator):
        report = validator.validate(make_short_setup(symbol="BTCUSD"))
        assert "usdt_perpetual" in failed_names(report)

    def test_symbol_outside_whitelist_rejected(self, settings):
        settings.scanner.symbols.mode = "whitelist"
        settings.scanner.symbols.whitelist = ["ETHUSDT.P"]
        validator = FinalValidator(settings, now_ms=lambda: NOW_MS)
        report = validator.validate(make_short_setup())
        assert "symbol_allowed" in failed_names(report)

    def test_out_of_order_candles_rejected(self, validator):
        setup = make_short_setup()
        swapped = make_short_setup(candle1=setup.candle2, candle2=setup.candle1)
        report = validator.validate(swapped)
        assert "candle_order" in failed_names(report)

    def test_reported_level_mismatch_rejected(self, validator):
        report = validator.validate(make_short_setup(level=110.02))
        assert "level_matches" in failed_names(report)


class TestReportSummary:
    def test_summary_names_failed_rules(self, validator):
        report = validator.validate(make_short_setup(entry=1.0))
        assert not report.passed
        assert "entry_price" in report.summary()
