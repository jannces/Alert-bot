"""Tests for TradingView chart links."""

from __future__ import annotations

from tradingview.links import chart_link
from tests.fixtures import make_short_setup


def test_link_encodes_symbol_and_interval():
    url = chart_link(make_short_setup(), "MEXC")
    assert url == (
        "https://www.tradingview.com/chart/?symbol=MEXC%3ABTCUSDT.P&interval=15"
    )


def test_hourly_interval():
    url = chart_link(make_short_setup(timeframe_minutes=60), "MEXC")
    assert url.endswith("&interval=60")
