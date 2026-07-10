"""SQLite persistence: signal history, rejection log, and duplicate guard.

SQLite (WAL mode) is deliberately chosen: the scanner is a single lightweight
process and needs zero-ops durable storage that survives restarts, so the
duplicate-alert guarantee holds across crashes and redeploys.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    sr_type TEXT NOT NULL,
    sr_level REAL NOT NULL,
    candle3_open_time_ms INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    sent_at TEXT,
    screenshot_attached INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT,
    symbol TEXT,
    timeframe_minutes INTEGER,
    direction TEXT,
    reasons TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, timeframe_minutes);
CREATE INDEX IF NOT EXISTS idx_rejections_symbol ON rejections (symbol, created_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignalRepository:
    """Thread-safe repository over a single SQLite database file."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- duplicate guard ----------------------------------------------------

    def reserve_signal(self, setup: "TamadSetup") -> bool:  # noqa: F821
        """Atomically claim a dedup key.

        Returns ``True`` when this setup has never been seen (and is now
        recorded), ``False`` when it is a duplicate. Claiming happens *before*
        sending so a crash mid-send can never produce a double alert.
        """
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO signals (
                        dedup_key, exchange, symbol, timeframe_minutes, direction,
                        entry, stop_loss, tp2, tp3, sr_type, sr_level,
                        candle3_open_time_ms, detected_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        setup.dedup_key,
                        setup.exchange,
                        setup.symbol,
                        setup.timeframe_minutes,
                        setup.direction.value,
                        setup.entry,
                        setup.stop_loss,
                        setup.tp2,
                        setup.tp3,
                        setup.sr_type,
                        setup.sr_level,
                        setup.candle3.open_time_ms,
                        setup.detected_at.isoformat(),
                        json.dumps(setup.raw_payload),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_sent(self, dedup_key: str, *, screenshot_attached: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE signals SET sent_at = ?, screenshot_attached = ? "
                "WHERE dedup_key = ?",
                (_utcnow(), int(screenshot_attached), dedup_key),
            )
            self._conn.commit()

    def release_signal(self, dedup_key: str) -> None:
        """Drop an unsent reservation (e.g. alert intentionally skipped)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM signals WHERE dedup_key = ? AND sent_at IS NULL",
                (dedup_key,),
            )
            self._conn.commit()

    # -- logging ------------------------------------------------------------

    def record_rejection(
        self,
        *,
        exchange: str | None,
        symbol: str | None,
        timeframe_minutes: int | None,
        direction: str | None,
        reasons: str,
        payload: dict,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rejections (
                    exchange, symbol, timeframe_minutes, direction,
                    reasons, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exchange,
                    symbol,
                    timeframe_minutes,
                    direction,
                    reasons,
                    json.dumps(payload),
                    _utcnow(),
                ),
            )
            self._conn.commit()

    # -- introspection --------------------------------------------------------

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
