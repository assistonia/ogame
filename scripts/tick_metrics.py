"""Durable per-account universe tick metrics.

The existing metrics collector owns periodic game-state snapshots. Universe tick
outcomes have a different cadence, so this focused writer adds one independent
table to the same SQLite database and commits each completed/suppressed tick.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "scripts" / "metrics.db"


class TickMetricsWriter:
    """Lazy, single-threaded SQLite writer used by the scheduler result loop."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, *, timeout_sec: float = 5.0):
        self.db_path = db_path
        self.timeout_sec = timeout_sec
        self._con: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._con is not None:
            return self._con

        con = sqlite3.connect(self.db_path, timeout=self.timeout_sec)
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS universe_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    email TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    engine TEXT NOT NULL,
                    cycle INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    skip_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_universe_ticks_ts_email
                    ON universe_ticks(ts, email);
                """
            )
            con.commit()
        except Exception:
            con.close()
            raise
        self._con = con
        return con

    def record(
        self,
        *,
        email: str,
        duration_sec: float,
        engine: str,
        cycle: int,
        result: str,
        skip_reason: str | None,
    ) -> None:
        con = self._connection()
        try:
            con.execute(
                """INSERT INTO universe_ticks(
                       ts, email, duration_sec, engine, cycle, result, skip_reason
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    email,
                    max(0.0, float(duration_sec)),
                    engine,
                    int(cycle),
                    result,
                    skip_reason,
                ),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
