"""Tests for the MEXC market-data client (mocked transport, no network)."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from config.settings import MexcSettings
from mexc.client import (
    MexcClient,
    MexcError,
    RateLimiter,
    api_symbol_to_tv,
    tv_symbol_to_api,
)


class TestSymbolMapping:
    def test_api_to_tv(self):
        assert api_symbol_to_tv("BTC_USDT") == "BTCUSDT.P"
        assert api_symbol_to_tv("1000PEPE_USDT") == "1000PEPEUSDT.P"

    def test_tv_to_api(self):
        assert tv_symbol_to_api("BTCUSDT.P") == "BTC_USDT"
        assert tv_symbol_to_api("1000PEPEUSDT.P") == "1000PEPE_USDT"


@pytest.mark.asyncio
async def test_rate_limiter_spaces_acquisitions():
    limiter = RateLimiter(rate_per_second=100)  # 10ms spacing
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.035  # 4 gaps × 10ms, with scheduling slack


def make_client(handler) -> MexcClient:
    settings = MexcSettings(requests_per_second=1000, retries=1, timeout_seconds=5)
    client = MexcClient(settings)
    client._client = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(handler)
    )
    return client


@pytest.mark.asyncio
async def test_symbol_discovery_filters_inactive_and_non_usdt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/contract/detail"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"symbol": "BTC_USDT", "quoteCoin": "USDT", "state": 0},
                    {"symbol": "ETH_USDT", "quoteCoin": "USDT", "state": 0},
                    {"symbol": "PAUSED_USDT", "quoteCoin": "USDT", "state": 2},
                    {"symbol": "BTC_USD", "quoteCoin": "USD", "state": 0},
                ],
            },
        )

    client = make_client(handler)
    assert await client.fetch_usdt_perp_symbols() == ["BTC_USDT", "ETH_USDT"]
    await client.aclose()


@pytest.mark.asyncio
async def test_klines_are_parsed_and_sorted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/contract/kline/BTC_USDT"
        assert request.url.params["interval"] == "Min15"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    # Deliberately unsorted; times are SECONDS in the API.
                    "time": [1700000900, 1700000000],
                    "open": [101.0, 100.0],
                    "high": [103.0, 102.0],
                    "low": [99.5, 99.0],
                    "close": [102.0, 101.0],
                },
            },
        )

    client = make_client(handler)
    candles = await client.fetch_klines("BTC_USDT", 15, 2)
    assert [c.open_time_ms for c in candles] == [1700000000000, 1700000900000]
    assert candles[0].open == 100.0 and candles[1].close == 102.0
    await client.aclose()


@pytest.mark.asyncio
async def test_server_errors_are_retried_then_raise():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = make_client(handler)
    with pytest.raises(MexcError):
        await client.fetch_klines("BTC_USDT", 15, 10)
    assert calls == 2  # first try + 1 retry
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failure():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "time": [1700000000],
                    "open": [1.0],
                    "high": [2.0],
                    "low": [0.5],
                    "close": [1.5],
                },
            },
        )

    client = make_client(handler)
    candles = await client.fetch_klines("BTC_USDT", 15, 1)
    assert len(candles) == 1 and calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_range_fetch_requires_native_timeframe():
    # fetch_klines resamples non-native timeframes (e.g. 10m from 5m), but
    # the raw range endpoint only speaks MEXC's native intervals.
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        await client.fetch_klines_range("BTC_USDT", 10, 0, 1000)
    await client.aclose()


@pytest.mark.asyncio
async def test_10m_timeframe_is_resampled_from_5m():
    t0 = 1_700_000_400  # aligned to a 10m boundary (seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["interval"] == "Min5"  # native base
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "time": [t0, t0 + 300, t0 + 600, t0 + 900, t0 + 1200],
                    "open": [100.0, 102.0, 104.0, 103.0, 105.0],
                    "high": [103.0, 105.0, 106.0, 104.5, 107.0],
                    "low": [99.0, 101.0, 102.0, 101.5, 104.0],
                    "close": [102.0, 104.0, 103.0, 104.2, 106.0],
                    "vol": [10.0, 20.0, 5.0, 7.0, 3.0],
                },
            },
        )

    client = make_client(handler)
    candles = await client.fetch_klines("BTC_USDT", 10, 5)
    # 5 five-minute bars → 2 complete 10m buckets; the trailing bar is dropped.
    assert len(candles) == 2
    first, second = candles
    assert first.open_time_ms == t0 * 1000
    assert (first.open, first.high, first.low, first.close) == (100.0, 105.0, 99.0, 104.0)
    assert first.volume == 30.0
    assert second.open_time_ms == (t0 + 600) * 1000
    assert (second.open, second.high, second.low, second.close) == (104.0, 106.0, 101.5, 104.2)
    await client.aclose()


def test_resample_drops_gappy_buckets():
    from mexc.client import resample_candles
    from strategy.models import Candle

    step = 300_000  # 5m in ms
    candles = [
        Candle(0, 1, 2, 0.5, 1.5),
        Candle(step, 1.5, 2.5, 1.0, 2.0),
        # 10m bucket at 600_000 is missing its first 5m bar:
        Candle(600_000 + step, 2.0, 3.0, 1.5, 2.5),
    ]
    result = resample_candles(candles, 5, 10)
    assert len(result) == 1 and result[0].open_time_ms == 0
