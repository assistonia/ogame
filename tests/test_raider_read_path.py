from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import raider
from bots.shared import db_reader, fleet


class _Client:
    email = "ai01@bots.local"

    def overview_html(self):
        raise AssertionError("DB success must not render overview")

    class session:
        @staticmethod
        def get(*_args, **_kwargs):
            raise AssertionError("DB success must not GET galaxy")


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_own_coords_uses_current_planet_db_without_http(monkeypatch):
    monkeypatch.setattr(
        db_reader,
        "list_planets_db",
        lambda _email: [
            {"galaxy": 1, "system": 8, "position": 4, "is_current": False},
            {"galaxy": 2, "system": 17, "position": 9, "is_current": True},
        ],
    )
    assert raider.own_coords(_Client()) == (2, 17, 9)


def test_active_fleet_info_uses_single_db_aggregate_without_event_http(monkeypatch):
    cursor = _Cursor({"active": 4, "attacking": 1, "harvesting": 2})
    monkeypatch.setattr(db_reader, "_conn", lambda: _Connection(cursor))
    monkeypatch.setattr(
        fleet,
        "fleet_events",
        lambda _client: (_ for _ in ()).throw(AssertionError("HTTP fallback used")),
    )

    assert raider._active_fleet_info(_Client()) == (4, True, 2)
    assert "fm.parent_id IS NULL" in cursor.sql
    assert "fm.processed=0" in cursor.sql
    assert cursor.params == (
        fleet.MISSION_ATTACK,
        fleet.MISSION_RECYCLE,
        "ai01@bots.local",
        fleet.MISSION_MISSILE,
    )


def test_fleet_csrf_reuses_client_session_token_without_get():
    client = _Client()
    client._token = "shared-session-token"
    assert fleet._csrf(client) == "shared-session-token"
    assert client._fleet_csrf == "shared-session-token"
