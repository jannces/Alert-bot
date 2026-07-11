"""Report how close near-miss candidates got to passing the third-candle rule.

Reads the ``rejections`` table and, for every candidate whose failed rules
include ``third_candle_rule``, recomputes the pattern level from the stored
candle OHLC and reports how far (in percent) Candle 3's close overshot it.
Sorted closest-first, so a decision to add tolerance to that rule can be made
from real data instead of guesswork — the rule itself is left untouched.

Usage (run from the project root, while the scanner keeps running elsewhere):

    python scripts/near_miss_report.py
    python scripts/near_miss_report.py --limit 10 --db database/tamad.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import NamedTuple

# Allow running as `python scripts/near_miss_report.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.models import Direction  # noqa: E402
from strategy.tamad_strategy import ComparisonMode, pattern_level  # noqa: E402


class NearMiss(NamedTuple):
    overshoot_pct: float
    pair: str
    timeframe_minutes: int
    direction: str
    level: float
    candle3_close: float
    detected_at: str


def compute_overshoot_pct(
    direction: Direction, level: float, candle3_close: float
) -> float:
    """How far Candle 3's close broke through the level, in percent.

    Positive means the close is on the wrong side of the level (the failure
    that was actually recorded); the further from zero, the further the
    candidate is from ever qualifying.
    """
    if direction is Direction.SHORT:
        return (candle3_close - level) / level * 100.0
    return (level - candle3_close) / level * 100.0


def load_near_misses(db_path: str, mode: ComparisonMode) -> list[NearMiss]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT pair, timeframe_minutes, direction, candles_json, created_at "
            "FROM rejections WHERE failed_rules LIKE '%third_candle_rule%' "
            "ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()

    near_misses = []
    for pair, timeframe_minutes, direction_str, candles_json, created_at in rows:
        c1, c2, c3 = json.loads(candles_json)
        direction = Direction(direction_str)
        level = pattern_level(direction, c1["c"], c2["c"], mode)
        overshoot = compute_overshoot_pct(direction, level, c3["c"])
        near_misses.append(
            NearMiss(overshoot, pair, timeframe_minutes, direction_str, level, c3["c"], created_at)
        )
    near_misses.sort(key=lambda n: n.overshoot_pct)
    return near_misses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="database/tamad.sqlite3")
    parser.add_argument("--limit", type=int, default=20, help="rows to print")
    parser.add_argument(
        "--mode",
        default="outer",
        choices=["strict", "midpoint", "outer"],
        help="must match strategy.equal_close.comparison_mode in config.yaml",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"database not found: {args.db} (has the scanner run yet?)")

    near_misses = load_near_misses(args.db, ComparisonMode(args.mode))
    if not near_misses:
        print("No third-candle-rule near-misses recorded yet.")
        return

    print(
        f"{'overshoot%':>10}  {'pair':<12} {'tf':>3} {'dir':<5} "
        f"{'level':>14} {'c3 close':>14}  detected_at"
    )
    for n in near_misses[: args.limit]:
        print(
            f"{n.overshoot_pct:>9.4f}%  {n.pair:<12} {n.timeframe_minutes:>3} "
            f"{n.direction:<5} {n.level:>14.6g} {n.candle3_close:>14.6g}  {n.detected_at}"
        )

    overshoots = [n.overshoot_pct for n in near_misses]
    print()
    print(
        f"n={len(overshoots)}  min={min(overshoots):.4f}%  "
        f"median={statistics.median(overshoots):.4f}%  max={max(overshoots):.4f}%"
    )
    print(
        "\nIf you're considering a third-candle tolerance, the 'min' value above "
        "is the smallest break that would need to be forgiven to pass one more "
        "signal — compare it to your equal_close.tolerance_percent for scale."
    )


if __name__ == "__main__":
    main()
