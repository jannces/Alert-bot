"""SQLite persistence: signal snapshots, near-miss rejections, duplicate
guard, and per-bar scan state.

SQLite (WAL mode) is deliberately chosen: the scanner is a single lightweight
process and needs zero-ops durable storage that survives restarts, so the
duplicate-alert and evaluate-once guarantees hold across crashes and
redeploys.

The ``signals`` table stores a complete snapshot of every sent alert (pair,
timeframe, OHLC of all three candles, entry, stop, targets, screenshot path,
timestamps, and the serialized validation report) — the raw dataset for
future backtesting and win-rate analytics.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from strategy.models import TamadSetup, ValidationReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    direction TEXT NOT NULL,

    c1_open_time_ms INTEGER NOT NULL,
    c1_open REAL NOT NULL, c1_high REAL NOT NULL, c1_low REAL NOT NULL, c1_close REAL NOT NULL,
    c2_open_time_ms INTEGER NOT NULL,
    c2_open REAL NOT NULL, c2_high REAL NOT NULL, c2_low REAL NOT NULL, c2_close REAL NOT NULL,
    c3_open_time_ms INTEGER NOT NULL,
    c3_open REAL NOT NULL, c3_high REAL NOT NULL, c3_low REAL NOT NULL, c3_close REAL NOT NULL,

    level REAL NOT NULL,
    sr_kind TEXT,
    sr_price REAL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    risk REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,

    detected_at TEXT NOT NULL,
    sent_at TEXT,
    screenshot_path TEXT,
    screenshot_attached INTEGER NOT NULL DEFAULT 0,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    direction TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    passed_rules TEXT NOT NULL,
    failed_rules TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    candles_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_state (
    symbol TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    last_bar_open_ms INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe_minutes)
);

CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals (pair, timeframe_minutes);
CREATE INDEX IF NOT EXISTS idx_rejections_pair ON rejections (pair, created_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candles_json(setup: TamadSetup) -> str:
    return json.dumps(
        [
            {"t": c.open_time_ms, "o": c.open, "h": c.high, "l": c.low, "c": c.close}
            for c in setup.candles
        ]
    )


def _report_json(report: ValidationReport) -> str:
    return json.dumps(
        [
            {"name": c.name, "passed": c.passed, "detail": c.detail}
            for c in report.checks
        ]
    )


class SignalRepository:
    """Thread-safe repository over a single SQLite database file."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # Lightweight migrations for databases created by earlier versions.
        for table in ("signals", "rejections"):
            self._ensure_column(table, "grade", "TEXT NOT NULL DEFAULT 'full'")
        self._conn.commit()
        self._lock = threading.Lock()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {
            row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- duplicate guard ------------------------------------------------------

    def reserve_signal(self, setup: TamadSetup, report: ValidationReport) -> bool:
        """Atomically claim a dedup key, storing the full signal snapshot.

        Returns ``True`` when this setup has never been seen (and is now
        recorded), ``False`` when it is a duplicate. Claiming happens *before*
        sending so a crash mid-send can never produce a double alert.
        """
        c1, c2, c3 = setup.candles
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO signals (
                        dedup_key, exchange, symbol, pair, timeframe_minutes, direction,
                        c1_open_time_ms, c1_open, c1_high, c1_low, c1_close,
                        c2_open_time_ms, c2_open, c2_high, c2_low, c2_close,
                        c3_open_time_ms, c3_open, c3_high, c3_low, c3_close,
                        level, sr_kind, sr_price,
                        entry, stop_loss, risk, tp2, tp3,
                        detected_at, validation_json, created_at, grade
                    ) VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,
                              ?,?,?, ?,?,?,?,?, ?,?,?,?)
                    """,
                    (
                        setup.dedup_key,
                        setup.exchange,
                        setup.symbol,
                        setup.pair,
                        setup.timeframe_minutes,
                        setup.direction.value,
                        c1.open_time_ms, c1.open, c1.high, c1.low, c1.close,
                        c2.open_time_ms, c2.open, c2.high, c2.low, c2.close,
                        c3.open_time_ms, c3.open, c3.high, c3.low, c3.close,
                        setup.level,
                        setup.sr.kind.value if setup.sr else None,
                        setup.sr.price if setup.sr else None,
                        setup.entry,
                        setup.stop_loss,
                        setup.risk,
                        setup.tp2,
                        setup.tp3,
                        setup.detected_at.isoformat(),
                        _report_json(report),
                        _utcnow(),
                        setup.grade.value,
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_sent(
        self,
        dedup_key: str,
        *,
        screenshot_path: str | None,
        screenshot_attached: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE signals SET sent_at = ?, screenshot_path = ?, "
                "screenshot_attached = ? WHERE dedup_key = ?",
                (_utcnow(), screenshot_path, int(screenshot_attached), dedup_key),
            )
            self._conn.commit()

    def release_signal(self, dedup_key: str) -> None:
        """Drop an unsent reservation (delivery failed or alert skipped)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM signals WHERE dedup_key = ? AND sent_at IS NULL",
                (dedup_key,),
            )
            self._conn.commit()

    # -- near-miss rejection log (D3) -------------------------------------------

    def record_rejection(self, setup: TamadSetup, report: ValidationReport) -> None:
        """Persist a near-miss: a candidate that failed final validation."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rejections (
                    exchange, symbol, pair, timeframe_minutes, direction,
                    detected_at, passed_rules, failed_rules, failure_reason,
                    candles_json, created_at, grade
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    setup.exchange,
                    setup.symbol,
                    setup.pair,
                    setup.timeframe_minutes,
                    setup.direction.value,
                    setup.detected_at.isoformat(),
                    json.dumps(list(report.passed_names)),
                    json.dumps(list(report.failed_names)),
                    report.summary(),
                    _candles_json(setup),
                    _utcnow(),
                    setup.grade.value,
                ),
            )
            self._conn.commit()

    # -- scan state: evaluate each bar at most once -------------------------------

    def load_scan_state(self, timeframe_minutes: int) -> dict[str, int]:
        """Last processed bar-open time per symbol for one timeframe."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, last_bar_open_ms FROM scan_state "
                "WHERE timeframe_minutes = ?",
                (timeframe_minutes,),
            ).fetchall()
        return {symbol: int(ms) for symbol, ms in rows}

    def set_scan_state(
        self, symbol: str, timeframe_minutes: int, bar_open_ms: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scan_state (symbol, timeframe_minutes, last_bar_open_ms, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT (symbol, timeframe_minutes)
                DO UPDATE SET last_bar_open_ms = excluded.last_bar_open_ms,
                              updated_at = excluded.updated_at
                """,
                (symbol, timeframe_minutes, bar_open_ms, _utcnow()),
            )
            self._conn.commit()

    # -- introspection ---------------------------------------------------------------

    def sent_signal_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE sent_at IS NOT NULL"
            ).fetchone()
        return int(row[0])

    def rejection_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM rejections").fetchone()
        return int(row[0])
