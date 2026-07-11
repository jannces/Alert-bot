"""Tests for candle-close boundary math."""

from __future__ import annotations

from scanner.scheduler import next_boundary_ms, timeframes_closing_at

M15 = 15 * 60_000
H1 = 60 * 60_000


class TestNextBoundary:
    def test_mid_candle_rounds_up(self):
        now = 10 * M15 + 1
        assert next_boundary_ms(now, 15) == 11 * M15

    def test_exact_boundary_moves_to_next(self):
        now = 10 * M15
        assert next_boundary_ms(now, 15) == 11 * M15

    def test_hourly_boundary(self):
        now = 3 * H1 + 5
        assert next_boundary_ms(now, 60) == 4 * H1


class TestTimeframesClosing:
    def test_quarter_hour_closes_only_15m(self):
        boundary = 5 * H1 + 15 * 60_000
        assert timeframes_closing_at(boundary, [15, 30, 60]) == [15]

    def test_half_hour_closes_15_and_30(self):
        boundary = 5 * H1 + 30 * 60_000
        assert timeframes_closing_at(boundary, [15, 30, 60]) == [15, 30]

    def test_full_hour_closes_all(self):
        boundary = 6 * H1
        assert timeframes_closing_at(boundary, [15, 30, 60]) == [15, 30, 60]
