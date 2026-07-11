"""TradingView chart links for Telegram alerts.

Every alert carries a link that opens the live TradingView chart with the
signal's symbol and timeframe pre-selected, e.g.

    https://www.tradingview.com/chart/?symbol=MEXC%3ABTCUSDT.P&interval=15
"""

from __future__ import annotations

from urllib.parse import quote

from strategy.models import TamadSetup

_BASE = "https://www.tradingview.com/chart/"


def chart_link(setup: TamadSetup, tv_prefix: str) -> str:
    symbol = f"{tv_prefix}:{setup.symbol}"
    return f"{_BASE}?symbol={quote(symbol, safe='')}&interval={setup.tv_interval}"
