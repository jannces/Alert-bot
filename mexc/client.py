"""Read-only client for MEXC's public futures market-data API.

Only two endpoints are used, both public and unauthenticated:

- ``GET /api/v1/contract/detail``          — list all contracts
- ``GET /api/v1/contract/kline/{symbol}``  — OHLC candles

There is deliberately NO authentication, NO account access, and NO trading
capability anywhere in this module.

Requests flow through a token-bucket rate limiter (config:
``mexc.requests_per_second``) to stay well under MEXC's public per-IP limits,
and transient failures are retried with exponential backoff + jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config.settings import MexcSettings
from strategy.models import Candle

logger = logging.getLogger(__name__)

_INTERVAL_BY_MINUTES = {1: "Min1", 5: "Min5", 15: "Min15", 30: "Min30", 60: "Min60"}


def resample_candles(
    candles: list[Candle], base_minutes: int, target_minutes: int
) -> list[Candle]:
    """Aggregate native candles into a larger timeframe (e.g. 2×5m → 10m).

    Only complete, gap-free buckets aligned to the target grid are emitted —
    a partial bucket would carry a wrong close, so it is dropped instead.
    """
    if target_minutes % base_minutes != 0:
        raise ValueError(f"{target_minutes}m is not a multiple of {base_minutes}m")
    ratio = target_minutes // base_minutes
    step_ms = target_minutes * 60_000
    base_ms = base_minutes * 60_000

    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        buckets.setdefault(candle.open_time_ms - candle.open_time_ms % step_ms, []).append(candle)

    result: list[Candle] = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda c: c.open_time_ms)
        expected = [start + i * base_ms for i in range(ratio)]
        if [c.open_time_ms for c in group] != expected:
            continue  # incomplete or gappy bucket
        result.append(
            Candle(
                open_time_ms=start,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
        )
    return result


def _native_base(timeframe_minutes: int) -> int | None:
    """Largest native interval that divides the requested timeframe."""
    for native in sorted(_INTERVAL_BY_MINUTES, reverse=True):
        if timeframe_minutes % native == 0:
            return native
    return None


def api_symbol_to_tv(api_symbol: str) -> str:
    """``BTC_USDT`` (MEXC API) → ``BTCUSDT.P`` (TradingView ticker)."""
    return api_symbol.replace("_", "") + ".P"


def tv_symbol_to_api(tv_symbol: str) -> str:
    """``BTCUSDT.P`` → ``BTC_USDT``."""
    base = tv_symbol.removesuffix(".P").removesuffix("USDT")
    return f"{base}_USDT"


class RateLimiter:
    """Simple asyncio token bucket: at most ``rate`` acquisitions per second."""

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


class MexcError(Exception):
    """Raised when MEXC returns an unusable response after all retries."""


class MexcClient:
    """Async MEXC futures market-data client (public endpoints only)."""

    def __init__(self, settings: MexcSettings) -> None:
        self._settings = settings
        self._limiter = RateLimiter(settings.requests_per_second)
        self._client = httpx.AsyncClient(
            base_url=settings.base_url, timeout=settings.timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_usdt_perp_symbols(self) -> list[str]:
        """All active USDT-margined perpetual contracts, as API symbols.

        MEXC contract states: 0 = enabled (normal trading). Anything else
        (paused, delivering, delisted…) is excluded — a setup on a
        non-tradable contract is useless.
        """
        data = await self._get_json("/api/v1/contract/detail")
        contracts = data.get("data") or []
        symbols = [
            c["symbol"]
            for c in contracts
            if isinstance(c, dict)
            and str(c.get("symbol", "")).endswith("_USDT")
            and c.get("quoteCoin", "USDT") == "USDT"
            and c.get("state", 0) == 0
        ]
        logger.info("MEXC symbol discovery: %d active USDT perpetuals", len(symbols))
        return symbols

    async def fetch_klines(
        self, api_symbol: str, timeframe_minutes: int, bars: int
    ) -> list[Candle]:
        """The most recent ``bars`` candles, oldest first.

        Timeframes MEXC does not serve natively (e.g. 10m) are built by
        aggregating the largest native interval that divides them (2×5m),
        still costing a single API request. The newest returned candle is
        usually the live (forming) bar — the caller is responsible for
        discarding unfinished candles by timestamp.
        """
        end_s = int(time.time())
        if timeframe_minutes in _INTERVAL_BY_MINUTES:
            start_s = end_s - bars * timeframe_minutes * 60
            return await self.fetch_klines_range(
                api_symbol, timeframe_minutes, start_s, end_s
            )
        base = _native_base(timeframe_minutes)
        if base is None:
            raise ValueError(f"unsupported timeframe: {timeframe_minutes} minutes")
        start_s = end_s - bars * timeframe_minutes * 60
        native = await self.fetch_klines_range(api_symbol, base, start_s, end_s)
        return resample_candles(native, base, timeframe_minutes)

    async def fetch_klines_range(
        self, api_symbol: str, timeframe_minutes: int, start_s: int, end_s: int
    ) -> list[Candle]:
        """Candles in ``[start_s, end_s]`` (unix seconds), oldest first.

        MEXC caps one response at roughly 2000 points; callers wanting longer
        spans (the backtester) chunk their requests.
        """
        interval = _INTERVAL_BY_MINUTES.get(timeframe_minutes)
        if interval is None:
            raise ValueError(f"unsupported timeframe: {timeframe_minutes} minutes")

        data = await self._get_json(
            f"/api/v1/contract/kline/{api_symbol}",
            params={"interval": interval, "start": start_s, "end": end_s},
        )
        payload = data.get("data") or {}
        times = payload.get("time") or []
        opens = payload.get("open") or []
        highs = payload.get("high") or []
        lows = payload.get("low") or []
        closes = payload.get("close") or []
        volumes = payload.get("vol") or []
        count = min(len(times), len(opens), len(highs), len(lows), len(closes))

        candles = [
            Candle(
                open_time_ms=int(times[i]) * 1000,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]) if i < len(volumes) else 0.0,
            )
            for i in range(count)
        ]
        candles.sort(key=lambda c: c.open_time_ms)
        return candles

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._settings.retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(path, params=params)
                if response.status_code == 200:
                    body = response.json()
                    if isinstance(body, dict) and body.get("success", True):
                        return body
                    last_error = MexcError(f"MEXC error body for {path}: {body}")
                elif response.status_code in (429, 500, 502, 503, 504):
                    last_error = MexcError(f"HTTP {response.status_code} for {path}")
                else:
                    # 4xx other than 429: retrying won't help.
                    raise MexcError(
                        f"HTTP {response.status_code} for {path}: {response.text[:200]}"
                    )
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < self._settings.retries:
                delay = 2.0**attempt + random.uniform(0, 0.5)
                logger.debug(
                    "MEXC %s attempt %d failed (%s); retrying in %.1fs",
                    path,
                    attempt + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)
        raise MexcError(f"MEXC request failed after retries: {path}") from last_error
