"""Tests for Telegram message formatting."""

from __future__ import annotations

from strategy.models import Direction
from telegram.bot import build_alert_message, format_price
from tests.fixtures import long_candles, make_short_setup


class TestFormatPrice:
    def test_large_prices_group_thousands(self):
        assert format_price(118250.0) == "118,250"

    def test_mid_prices_trim_trailing_zeros(self):
        assert format_price(1.5000) == "1.5"

    def test_sub_unit_prices_keep_precision(self):
        assert format_price(0.0004215) == "0.0004215"


class TestAlertMessage:
    def test_short_message_contains_all_required_fields(self):
        setup = make_short_setup()
        text = build_alert_message(setup, market="USDT Perpetual", screenshot_ok=True)
        for expected in (
            "TAMAD STRATEGY",
            "MEXC",
            "USDT Perpetual",
            "BTCUSDT",
            "SHORT",
            "15m",
            "109.5",  # entry
            "113",  # stop loss
            "102.5",  # TP2
            "99",  # TP3
            "1:2 / 1:3",
            "Time Detected",
            "Equal Closing Price",
            "Third Candle respected Resistance",
            "Candle 3 Closed",
        ):
            assert expected in text, f"missing {expected!r}"

    def test_long_message_uses_support_wording(self):
        c1, c2, c3 = long_candles()
        setup = make_short_setup(
            direction=Direction.LONG,
            candle1=c1,
            candle2=c2,
            candle3=c3,
            level=110.02,
            sr_type="swing_low",
            entry=110.5,
            stop_loss=107.0,
            tp2=117.5,
            tp3=121.0,
        )
        text = build_alert_message(setup, market="USDT Perpetual", screenshot_ok=True)
        assert "LONG" in text
        assert "Support" in text
        assert "Red Candle 1" in text
        assert "Green Candle 2" in text

    def test_missing_screenshot_adds_warning(self):
        setup = make_short_setup()
        text = build_alert_message(setup, market="USDT Perpetual", screenshot_ok=False)
        assert "⚠️" in text
        ok_text = build_alert_message(setup, market="USDT Perpetual", screenshot_ok=True)
        assert "⚠️" not in ok_text
