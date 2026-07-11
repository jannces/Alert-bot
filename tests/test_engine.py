"""Tests for the scan engine, using a fake MEXC client."""

from __future__ import annotations

import pytest

from config.settings import Settings
from database.repository import SignalRepository
from scanner.engine import ScanEngine
from strategy.models import Candle, Direction, SRKind, TamadSetup
from strategy.sr_levels import SwingHighLowDetector

TF = 15
TF_MS = TF * 60_000
T0 = 1_759_999_500_000  # aligned to the 15m grid


def t(i: int) -> int:
    return T0 + i * TF_MS


def flat(i: int) -> Candle:
    return Candle(t(i), 100.0, 105.0, 95.0, 101.0)


def pattern_series(*, with_pattern: bool = True, with_forming: bool = True) -> list[Candle]:
    """20 closed candles: a swing high at index 8, the Tamad SHORT pattern at
    indices 17-19, plus (optionally) a forming candle the engine must drop."""
    candles = [flat(i) for i in range(17)]
    candles[8] = Candle(t(8), 100.0, 110.1, 95.0, 101.0)  # swing high near 110
    if with_pattern:
        candles += [
            Candle(t(17), 100.0, 111.0, 99.0, 110.0),
            Candle(t(18), 112.0, 113.0, 109.5, 110.02),
            Candle(t(19), 105.0, 112.0, 104.9, 109.5),
        ]
    else:
        candles += [flat(17), flat(18), flat(19)]
    if with_forming:
        candles.append(Candle(t(20), 109.0, 109.8, 108.0, 109.2))  # live bar
    return candles


BOUNDARY_MS = t(20)  # candle at t(19) has just closed


class FakeMexcClient:
    def __init__(self, series: list[Candle], symbols: list[str] | None = None) -> None:
        self.series = series
        self.symbols = symbols or ["TEST_USDT"]
        self.kline_calls = 0

    async def fetch_usdt_perp_symbols(self) -> list[str]:
        return self.symbols

    async def fetch_klines(self, api_symbol: str, tf_minutes: int, bars: int):
        self.kline_calls += 1
        return list(self.series)


class PipelineStub:
    def __init__(self) -> None:
        self.setups: list[TamadSetup] = []

    async def process(self, setup: TamadSetup) -> None:
        self.setups.append(setup)


def make_engine(
    tmp_path, client, *, sr_enabled: bool = True
) -> tuple[ScanEngine, PipelineStub, SignalRepository]:
    settings = Settings()
    settings.strategy.support_resistance.enabled = sr_enabled
    settings.strategy.support_resistance.left_bars = 3
    settings.strategy.support_resistance.right_bars = 3
    repo = SignalRepository(tmp_path / "engine.sqlite3")
    pipeline = PipelineStub()
    engine = ScanEngine(
        settings=settings,
        client=client,
        repository=repo,
        sr_detector=SwingHighLowDetector(3, 3),
        pipeline=pipeline,
    )
    return engine, pipeline, repo


@pytest.mark.asyncio
async def test_pattern_becomes_a_fully_built_candidate(tmp_path):
    engine, pipeline, _ = make_engine(tmp_path, FakeMexcClient(pattern_series()))
    await engine.sweep([TF], BOUNDARY_MS)

    assert len(pipeline.setups) == 1
    setup = pipeline.setups[0]
    assert setup.direction is Direction.SHORT
    assert setup.symbol == "TESTUSDT.P"
    assert setup.timeframe_minutes == TF
    assert setup.entry == 109.5  # candle 3 close
    assert setup.stop_loss == 113.0  # extreme wick
    assert setup.tp2 == 102.5 and setup.tp3 == 99.0
    assert setup.level == 110.0  # strict comparison mode
    assert setup.sr is not None
    assert setup.sr.kind is SRKind.SWING_HIGH and setup.sr.price == 110.1
    assert setup.candle3.open_time_ms == t(19)  # the just-closed bar


@pytest.mark.asyncio
async def test_sr_lookup_skipped_when_filter_disabled(tmp_path):
    engine, pipeline, _ = make_engine(
        tmp_path, FakeMexcClient(pattern_series()), sr_enabled=False
    )
    await engine.sweep([TF], BOUNDARY_MS)

    assert len(pipeline.setups) == 1
    assert pipeline.setups[0].sr is None  # candidate still fully built


@pytest.mark.asyncio
async def test_forming_candle_is_never_evaluated(tmp_path):
    # Same series but the live bar itself would look like a pattern breaker;
    # candle 3 must still be the bar that closed at the boundary.
    engine, pipeline, _ = make_engine(tmp_path, FakeMexcClient(pattern_series()))
    await engine.sweep([TF], BOUNDARY_MS)
    assert pipeline.setups[0].candle3.open_time_ms == t(19)


@pytest.mark.asyncio
async def test_each_bar_is_evaluated_only_once(tmp_path):
    client = FakeMexcClient(pattern_series())
    engine, pipeline, _ = make_engine(tmp_path, client)
    await engine.sweep([TF], BOUNDARY_MS)
    await engine.sweep([TF], BOUNDARY_MS)  # e.g. after a restart mid-boundary

    assert len(pipeline.setups) == 1
    assert client.kline_calls == 1  # second sweep skipped before fetching


@pytest.mark.asyncio
async def test_evaluate_once_survives_restart(tmp_path):
    client = FakeMexcClient(pattern_series())
    engine, pipeline, repo = make_engine(tmp_path, client)
    await engine.sweep([TF], BOUNDARY_MS)
    repo.close()

    engine2, pipeline2, _ = make_engine(tmp_path, client)
    await engine2.sweep([TF], BOUNDARY_MS)
    assert not pipeline2.setups


@pytest.mark.asyncio
async def test_no_pattern_produces_no_candidate_but_marks_bar(tmp_path):
    engine, pipeline, repo = make_engine(
        tmp_path, FakeMexcClient(pattern_series(with_pattern=False))
    )
    await engine.sweep([TF], BOUNDARY_MS)
    assert not pipeline.setups
    assert repo.load_scan_state(TF)["TEST_USDT"] == t(19)


@pytest.mark.asyncio
async def test_lagging_api_leaves_bar_unprocessed_for_retry(tmp_path):
    # The just-closed bar is not in the API response yet.
    series = pattern_series(with_forming=False)[:-1]
    engine, pipeline, repo = make_engine(tmp_path, FakeMexcClient(series))
    await engine.sweep([TF], BOUNDARY_MS)
    assert not pipeline.setups
    assert "TEST_USDT" not in repo.load_scan_state(TF)


@pytest.mark.asyncio
async def test_whitelist_filters_symbols(tmp_path):
    client = FakeMexcClient(pattern_series(), symbols=["TEST_USDT", "OTHER_USDT"])
    engine, pipeline, _ = make_engine(tmp_path, client)
    engine._settings.scanner.symbols.mode = "whitelist"
    engine._settings.scanner.symbols.whitelist = ["OTHERUSDT.P"]
    await engine.sweep([TF], BOUNDARY_MS)
    symbols = {s.symbol for s in pipeline.setups}
    assert symbols == {"OTHERUSDT.P"}
