"""Durable, low-volume observability for the AI raid decision funnel."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sqlite3
import threading
from typing import Any


METRICS_DB = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "metrics.db"
_TRACKED_NAMED_ACCOUNTS = frozenset({
    "pirate01@bots.local",
    "explorer01@bots.local",
})
_INIT_LOCK = threading.Lock()
_initialized = False
_COORD_RE = re.compile(r"(?<!\d)(\d+):(\d+):(\d+)(?!\d)")


def _tracked(email: str) -> bool:
    normalized = str(email).lower()
    return normalized.startswith("ai") or normalized in _TRACKED_NAMED_ACCOUNTS


def _stage(note: str) -> str:
    if "ATTACK" in note:
        return "launched"
    if "PREFLIGHT SPY" in note:
        return "preflight_requested"
    if "PREFLIGHT WAIT" in note:
        return "preflight_wait"
    if "PREFLIGHT HOLD" in note:
        return "preflight_hold"
    if "raid SPY" in note:
        return "scouted"
    if "raid idle" in note:
        return "idle"
    if "HARVEST" in note:
        return "harvest"
    if "COUNTER" in note:
        return "counter_hold"
    return "other"


def _number(note: str, label: str) -> int | None:
    match = re.search(rf"{re.escape(label)}[~ ]*(\d+)", note)
    return int(match.group(1)) if match else None


def _ensure_schema(con: sqlite3.Connection) -> None:
    global _initialized
    if _initialized:
        return
    with _INIT_LOCK:
        if _initialized:
            return
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS attack_funnel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                email TEXT NOT NULL,
                target TEXT,
                stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                expected_loot INTEGER,
                fleet_value INTEGER,
                fleet_value_limit INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_attack_funnel_email_ts "
            "ON attack_funnel(email, ts)"
        )
        _initialized = True


def record_result(email: str, note: str | None, **metadata: Any) -> None:
    """Append one terminal raid decision. Observability must never break a tick."""
    if not note or not _tracked(email):
        return
    match = _COORD_RE.search(note)
    target = ":".join(match.groups()) if match else None
    try:
        METRICS_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(METRICS_DB, timeout=2) as con:
            con.execute("PRAGMA busy_timeout=2000")
            _ensure_schema(con)
            con.execute(
                """
                INSERT INTO attack_funnel(
                    ts,email,target,stage,reason,expected_loot,
                    fleet_value,fleet_value_limit,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    str(email).lower(),
                    target,
                    _stage(note),
                    note,
                    _number(note, "예상약탈"),
                    _number(note, "함대가치"),
                    _number(note, "동적한도"),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
    except Exception:  # noqa: BLE001 - metrics are fail-open by contract
        return
