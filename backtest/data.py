"""Historical candle loading with chunking and a local JSON cache.

MEXC caps one kline response at ~2000 points, so longer spans are fetched in
windows and stitched. Fetched series are cached per (symbol, timeframe, day)
under ``backtest/cache/`` so re-running the backtest with different filters
costs zero API calls.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from mexc.client import MexcClient
from strategy.models import Candle

logger = logging.getLogger(__name__)

_CHUNK_BARS = 1900  # safely under MEXC's ~2000-point response cap


class HistoricalDataLoader:
    def __init__(self, client: MexcClient, cache_dir: str | Path = "backtest/cache") -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def load(
        self, api_symbol: str, timeframe_minutes: int, days: int
    ) -> list[Candle]:
        """The last ``days`` of closed candles for one symbol, oldest first."""
        cache_path = self._cache_path(api_symbol, timeframe_minutes, days)
        if cache_path.exists():
            return self._read_cache(cache_path)

        end_s = int(time.time())
        start_s = end_s - days * 86_400
        step_s = _CHUNK_BARS * timeframe_minutes * 60
        candles: dict[int, Candle] = {}
        window_start = start_s
        while window_start < end_s:
            window_end = min(window_start + step_s, end_s)
            chunk = await self._client.fetch_klines_range(
                api_symbol, timeframe_minutes, window_start, window_end
            )
            for candle in chunk:
                candles[candle.open_time_ms] = candle
            window_start = window_end + 1

        ordered = [candles[t] for t in sorted(candles)]
        # Drop the newest bar — it may still be forming.
        if ordered and ordered[-1].close_time_ms(timeframe_minutes) > end_s * 1000:
            ordered.pop()
        self._write_cache(cache_path, ordered)
        logger.info(
            "loaded %s %dm: %d candles (%d days)",
            api_symbol,
            timeframe_minutes,
            len(ordered),
            days,
        )
        return ordered

    def _cache_path(self, api_symbol: str, timeframe_minutes: int, days: int) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self._cache_dir / f"{api_symbol}_{timeframe_minutes}m_{days}d_{today}.json"

    @staticmethod
    def _read_cache(path: Path) -> list[Candle]:
        rows = json.loads(path.read_text())
        return [Candle(r["t"], r["o"], r["h"], r["l"], r["c"], r.get("v", 0.0)) for r in rows]

    @staticmethod
    def _write_cache(path: Path, candles: list[Candle]) -> None:
        path.write_text(
            json.dumps(
                [
                    {"t": c.open_time_ms, "o": c.open, "h": c.high, "l": c.low,
                     "c": c.close, "v": c.volume}
                    for c in candles
                ]
            )
        )
