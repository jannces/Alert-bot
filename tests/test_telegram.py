"""Tests for Telegram alert formatting."""

from __future__ import annotations

import pytest

from config.settings import Settings
from strategy.validation import FinalValidator
from telegram.bot import build_alert_message, format_price
from tests.fixtures import NOW_MS, make_long_setup, make_short_setup

CHART_URL = "https://www.tradingview.com/chart/?symbol=MEXC%3ABTCUSDT.P&interval=15"


@pytest.fixture()
def validator() -> FinalValidator:
    return FinalValidator(Settings(), now_ms=lambda: NOW_MS)


class TestFormatPrice:
    def test_large_prices_group_thousands(self):
        assert format_price(118250.0) == "118,250"

    def test_mid_prices_trim_trailing_zeros(self):
        assert format_price(1.5000) == "1.5"

    def test_sub_unit_prices_keep_precision(self):
        assert format_price(0.0004215) == "0.0004215"


class TestAlertMessage:
    def test_short_message_contains_all_required_fields(self, validator):
        setup = make_short_setup()
        report = validator.validate(setup)
        text = build_alert_message(setup, report, CHART_URL, screenshot_ok=True)
        for expected in (
            "TAMAD STRATEGY",
            "BTCUSDT",
            "SHORT",
            "15m",
            "109.5",  # entry
            "113",  # stop loss
            "102.5",  # Take Profit 2R
            "99",  # Take Profit 3R
            "1:2 / 1:3",
            "Detection Time",
        ):
            assert expected in text, f"missing {expected!r}"

    def test_validation_checklist_default_has_six_rules(self, validator):
        # The S/R confirmation is disabled by default, so its checklist row
        # is omitted rather than shown as a failure.
        setup = make_short_setup()
        text = build_alert_message(
            setup, validator.validate(setup), CHART_URL, screenshot_ok=True
        )
        for label in (
            "Candle 1 Color",
            "Candle 2 Color",
            "Candle 3 Rule",
            "Equal Close",
            "Stop Loss",
            "Take Profit",
        ):
            assert f"✅ {label}" in text
        assert "Support / Resistance" not in text
        assert "❌" not in text
        assert "6 / 6 Rules Passed" in text

    def test_validation_checklist_shows_seven_rules_with_sr_enabled(self):
        settings = Settings()
        settings.strategy.support_resistance.enabled = True
        validator = FinalValidator(settings, now_ms=lambda: NOW_MS)
        setup = make_short_setup()
        text = build_alert_message(
            setup, validator.validate(setup), CHART_URL, screenshot_ok=True
        )
        assert "✅ Support / Resistance" in text
        assert "7 / 7 Rules Passed" in text

    def test_tradingview_link_is_included(self, validator):
        setup = make_short_setup()
        text = build_alert_message(
            setup, validator.validate(setup), CHART_URL, screenshot_ok=True
        )
        # HTML parse mode: the link appears as an anchor and as escaped text.
        escaped = CHART_URL.replace("&", "&amp;")
        assert f'<a href="{escaped}">' in text
        assert f"\n{escaped}" in text

    def test_long_message_direction(self, validator):
        setup = make_long_setup()
        text = build_alert_message(
            setup, validator.validate(setup), CHART_URL, screenshot_ok=True
        )
        assert "LONG" in text
        assert "6 / 6 Rules Passed" in text

    def test_missing_screenshot_adds_warning(self, validator):
        setup = make_short_setup()
        report = validator.validate(setup)
        assert "⚠️" in build_alert_message(setup, report, CHART_URL, screenshot_ok=False)
        assert "⚠️" not in build_alert_message(setup, report, CHART_URL, screenshot_ok=True)
