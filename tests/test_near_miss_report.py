"""Tests for the near-miss diagnostic script's overshoot calculation."""

from __future__ import annotations

import json
import sqlite3

from scripts.near_miss_report import compute_overshoot_pct, load_near_misses
from strategy.models import Direction
from strategy.tamad_strategy import ComparisonMode


class TestComputeOvershoot:
    def test_short_overshoot_is_positive_when_close_above_level(self):
        assert compute_overshoot_pct(Direction.SHORT, 100.0, 100.5) == 0.5

    def test_short_at_level_is_zero(self):
        assert compute_overshoot_pct(Direction.SHORT, 100.0, 100.0) == 0.0

    def test_long_overshoot_is_positive_when_close_below_level(self):
        assert compute_overshoot_pct(Direction.LONG, 100.0, 99.5) == 0.5

    def test_long_at_level_is_zero(self):
        assert compute_overshoot_pct(Direction.LONG, 100.0, 100.0) == 0.0


class TestLoadNearMisses:
    def _make_db(self, tmp_path, rows: list[tuple]) -> str:
        db_path = str(tmp_path / "near_miss.sqlite3")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE rejections (pair TEXT, timeframe_minutes INTEGER, "
            "direction TEXT, failed_rules TEXT, candles_json TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO rejections VALUES (?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()
        return db_path

    def test_only_third_candle_rule_failures_are_included(self, tmp_path):
        candles = json.dumps(
            [{"c": 100.0}, {"c": 100.0}, {"c": 100.5}]
        )
        db = self._make_db(
            tmp_path,
            [
                ("BTCUSDT", 15, "SHORT", '["third_candle_rule"]', candles, "t1"),
                ("ETHUSDT", 15, "SHORT", '["sr_present"]', candles, "t2"),
            ],
        )
        results = load_near_misses(db, ComparisonMode.STRICT)
        assert len(results) == 1
        assert results[0].pair == "BTCUSDT"

    def test_sorted_closest_first(self, tmp_path):
        big_miss = json.dumps([{"c": 100.0}, {"c": 100.0}, {"c": 105.0}])
        small_miss = json.dumps([{"c": 100.0}, {"c": 100.0}, {"c": 100.1}])
        db = self._make_db(
            tmp_path,
            [
                ("BIG", 15, "SHORT", '["third_candle_rule"]', big_miss, "t1"),
                ("SMALL", 15, "SHORT", '["third_candle_rule"]', small_miss, "t2"),
            ],
        )
        results = load_near_misses(db, ComparisonMode.STRICT)
        assert [r.pair for r in results] == ["SMALL", "BIG"]

    def test_recomputes_level_from_stored_candles(self, tmp_path):
        # strict SHORT level = min(close1, close2) = 100.0
        candles = json.dumps([{"c": 100.0}, {"c": 100.05}, {"c": 100.2}])
        db = self._make_db(
            tmp_path,
            [("BTCUSDT", 15, "SHORT", '["third_candle_rule"]', candles, "t1")],
        )
        results = load_near_misses(db, ComparisonMode.STRICT)
        assert results[0].level == 100.0
