from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared import db_reader
from scripts import run_universe


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


def test_due_fleet_probe_matches_server_acs_and_hold_semantics(monkeypatch):
    cursor = _Cursor({"has_due": 1})
    monkeypatch.setattr(db_reader, "_conn", lambda: _Connection(cursor))

    assert db_reader.has_due_fleet_missions_db(
        "ai01@bots.local", now_ts=1_234_567
    ) is True

    sql = " ".join(cursor.sql.lower().split())
    assert "fm.processed = 0" in sql
    assert "p.id = fm.planet_id_from or p.id = fm.planet_id_to" in sql
    assert "fm.mission_type = 5 and fm.parent_id is null" in sql
    assert "fm.time_arrival - coalesce(fm.time_holding, 0) <= %s" in sql
    assert "fm.mission_type <> 5 or fm.parent_id is not null" in sql
    assert "fm.time_arrival + coalesce(fm.time_holding, 0) <= %s" in sql
    assert "fm.canceled" not in sql
    assert cursor.params == ("ai01@bots.local", 1_234_567, 1_234_567)


def test_due_fleet_probe_false_when_server_has_nothing_to_process(monkeypatch):
    cursor = _Cursor({"has_due": 0})
    monkeypatch.setattr(db_reader, "_conn", lambda: _Connection(cursor))
    assert db_reader.has_due_fleet_missions_db("ai01@bots.local", now_ts=7) is False


class _Client:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.settlement_calls = 0

    def settle_overview(self):
        self.settlement_calls += 1
        if self.fail:
            raise RuntimeError("temporary")


def test_account_tick_settles_overview_exactly_once_only_when_due(monkeypatch):
    client = _Client()
    monkeypatch.setattr(
        run_universe.db_reader, "has_due_fleet_missions_db", lambda _email: True
    )

    assert run_universe._settle_due_fleets(client, "ai01@bots.local") is True
    assert client.settlement_calls == 1


def test_no_due_fleet_does_not_render_overview(monkeypatch):
    client = _Client()
    monkeypatch.setattr(
        run_universe.db_reader, "has_due_fleet_missions_db", lambda _email: False
    )

    assert run_universe._settle_due_fleets(client, "ai01@bots.local") is False
    assert client.settlement_calls == 0


def test_due_probe_or_settlement_failure_preserves_planner_path(monkeypatch):
    client = _Client()
    monkeypatch.setattr(
        run_universe.db_reader,
        "has_due_fleet_missions_db",
        lambda _email: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    assert run_universe._settle_due_fleets(client, "ai01@bots.local") is False
    assert client.settlement_calls == 0

    failing_client = _Client(fail=True)
    monkeypatch.setattr(
        run_universe.db_reader, "has_due_fleet_missions_db", lambda _email: True
    )
    assert run_universe._settle_due_fleets(
        failing_client, "ai01@bots.local"
    ) is False
    assert failing_client.settlement_calls == 1
