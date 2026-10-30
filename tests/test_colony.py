"""식민 확장의 DB 우선 조회·레이더 안전 가드 단위검증(DB/HTTP 실호출 없음)."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import colony, miner_bot


EMAIL = "raider@bots.local"
HOME = {
    "id": 1,
    "name": "home",
    "galaxy": 1,
    "system": 50,
    "position": 5,
    "is_current": True,
}
COLONY = {
    "id": 2,
    "name": "save-target",
    "galaxy": 1,
    "system": 51,
    "position": 8,
    "is_current": False,
}


class _Response:
    def __init__(self, text: str):
        self.text = text


class _Session:
    def __init__(self, text: str = "", *, fail: bool = False):
        self.text = text
        self.fail = fail
        self.gets: list[str] = []

    def get(self, url, **_kwargs):
        self.gets.append(str(url))
        if self.fail:
            raise OSError("http down")
        return _Response(self.text)


class _Client:
    base_url = "http://game.test"

    def __init__(self, *, html: str = "", http_fail: bool = False):
        self.session = _Session(html, fail=http_fail)
        self.researched: list[int] = []
        self.ships: list[tuple[int, int]] = []

    def research(self, technology_id: int):
        self.researched.append(technology_id)
        return {"status": "success"}

    def build_ships(self, technology_id: int, amount: int):
        self.ships.append((technology_id, amount))
        return {"status": "success"}


class _Cursor:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql = " ".join(sql.split()).lower()
        self.params = params

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, cursor: _Cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _ready_research_levels() -> dict[int, int]:
    return {
        colony.RESEARCH_LAB: 3,
        colony.ENERGY_TECHNOLOGY: 1,
        colony.IMPULSE_DRIVE: 3,
        colony.ESPIONAGE_TECHNOLOGY: 4,
        colony.ASTROPHYSICS: 0,
        colony.SHIPYARD: 4,
        colony.COLONY_SHIP: 0,
    }


def _ready_ship_levels() -> dict[int, int]:
    return {
        **_ready_research_levels(),
        colony.ASTROPHYSICS: 1,
    }


def test_astro_planet_formulas_and_raider_first_target():
    assert [colony.max_planets_for_astro(i) for i in range(5)] == [1, 2, 2, 3, 3]
    assert colony.astro_needed_for(1) == 0
    assert colony.astro_needed_for(2) == 1
    assert colony.astro_needed_for(3) == 3


def test_raider_research_chain_reaches_missing_espionage4_then_astro1():
    levels = _ready_research_levels()
    levels[colony.ESPIONAGE_TECHNOLOGY] = 3
    assert colony.next_research_step(levels, 1) == colony.ESPIONAGE_TECHNOLOGY
    levels[colony.ESPIONAGE_TECHNOLOGY] = 4
    assert colony.next_research_step(levels, 1) == colony.ASTROPHYSICS


def test_planet_list_uses_db_without_overview_http(monkeypatch):
    monkeypatch.setattr(colony.db_reader, "list_planets_db", lambda _email: [dict(HOME)])
    monkeypatch.setattr(
        colony.planets_mod,
        "list_planets",
        lambda _client: pytest.fail("DB 성공 때 overview를 호출하면 안 됨"),
    )
    assert colony._list_planets(_Client(), EMAIL) == [HOME]


def test_planet_list_falls_back_to_existing_overview_parser_on_db_failure(monkeypatch):
    def fail(_email):
        raise OSError("db down")

    fallback = [{**HOME, "current": True}]
    monkeypatch.setattr(colony.db_reader, "list_planets_db", fail)
    monkeypatch.setattr(colony.planets_mod, "list_planets", lambda _client: fallback)
    assert colony._list_planets(_Client(), EMAIL) == fallback


def test_own_system_accepts_db_is_current_shape():
    rows = [{**HOME, "is_current": False}, {**COLONY, "is_current": True}]
    assert colony._own_system(object(), rows) == (1, 51)


@pytest.mark.parametrize(("count", "busy"), [(0, False), (2, True)])
def test_research_queue_uses_user_wide_db_count_without_http(monkeypatch, count, busy):
    cursor = _Cursor(one={"n": count})
    monkeypatch.setattr(colony.db_reader, "_conn", lambda: _Connection(cursor))
    client = _Client(http_fail=True)

    assert colony.research_in_progress(client, EMAIL) is busy
    assert client.session.gets == []
    assert "research_queues" in cursor.sql and "building_queues" in cursor.sql
    assert "rq.processed = 0" in cursor.sql and "rq.canceled = 0" in cursor.sql
    assert "bq.object_id = %s" in cursor.sql and "bq.canceled = 0" in cursor.sql
    assert cursor.params == (colony.RESEARCH_LAB, EMAIL)


def test_research_lab_guard_only_blocks_research_for_lab_actions():
    assert miner_bot.research_blocked_by_lab({31}, []) is True
    assert miner_bot.research_blocked_by_lab(set(), [("facility", 31)]) is True
    assert miner_bot.research_blocked_by_lab({14}, [("build", 1)]) is False


def test_research_queue_db_failure_uses_http_fallback_then_fails_closed(monkeypatch):
    monkeypatch.setattr(
        colony.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    active = _Client(html='<span id="researchCountdown"></span>')
    assert colony.research_in_progress(active, EMAIL) is True
    assert len(active.session.gets) == 1

    broken = _Client(http_fail=True)
    assert colony.research_in_progress(broken, EMAIL) is True


@pytest.mark.parametrize(("row", "busy"), [({"n": 0}, False), ({"n": 1}, True), (None, True)])
def test_unit_queue_is_current_planet_scoped_and_fail_closed(monkeypatch, row, busy):
    cursor = _Cursor(one=row)
    monkeypatch.setattr(colony.db_reader, "_conn", lambda: _Connection(cursor))
    assert colony.unit_queue_in_progress(EMAIL) is busy
    assert "u.planet_current = p.id" in cursor.sql
    assert "uq.processed = 0" in cursor.sql


def test_unit_queue_db_failure_is_busy(monkeypatch):
    monkeypatch.setattr(
        colony.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    assert colony.unit_queue_in_progress(EMAIL) is True


def test_colonisation_mission_uses_parent_db_row_without_event_http(monkeypatch):
    cursor = _Cursor(one={"n": 1})
    monkeypatch.setattr(colony.db_reader, "_conn", lambda: _Connection(cursor))
    monkeypatch.setattr(
        colony.fleet,
        "fleet_events",
        lambda _client: pytest.fail("DB 성공 때 eventlist HTTP 금지"),
    )
    assert colony.colonisation_in_flight(_Client(), EMAIL) is True
    assert "fm.parent_id is null" in cursor.sql
    assert "fm.processed = 0" in cursor.sql and "fm.canceled = 0" in cursor.sql
    assert cursor.params == (EMAIL, colony.fleet.MISSION_COLONISATION)


def test_colonisation_db_failure_uses_event_fallback_and_both_fail_closed(monkeypatch):
    monkeypatch.setattr(
        colony.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    monkeypatch.setattr(
        colony.fleet,
        "fleet_events",
        lambda _client: {"events": [{"return_flight": False, "mission_type": 7}]},
    )
    assert colony.colonisation_in_flight(_Client(), EMAIL) is True

    monkeypatch.setattr(
        colony.fleet,
        "fleet_events",
        lambda _client: (_ for _ in ()).throw(OSError("http down")),
    )
    assert colony.colonisation_in_flight(_Client(), EMAIL) is True


def test_empty_slot_uses_db_and_central_position_priority(monkeypatch):
    cursor = _Cursor(many=[{"position": 8}, {"position": 9}])
    monkeypatch.setattr(colony.db_reader, "_conn", lambda: _Connection(cursor))
    monkeypatch.setattr(
        colony.fleet,
        "scan_galaxy",
        lambda *_args: pytest.fail("DB 성공 때 galaxy HTTP 금지"),
    )
    assert colony.find_empty_slot(_Client(), 1, 50) == (1, 50, 7)
    assert "planet_type = 1" in cursor.sql
    assert cursor.params == (1, 50)


def test_empty_slot_db_failure_falls_back_to_existing_galaxy_scan(monkeypatch):
    monkeypatch.setattr(
        colony.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    monkeypatch.setattr(
        colony.fleet,
        "scan_galaxy",
        lambda _client, _g, _s: [{"position": 8}],
    )
    assert colony.find_empty_slot(_Client(), 1, 50) == (1, 50, 9)


def test_former_human_system_is_available_after_exclusion_removal(monkeypatch):
    monkeypatch.setattr(
        colony.db_reader,
        "_conn",
        lambda: (_ for _ in ()).throw(OSError("db down")),
    )
    monkeypatch.setattr(colony.fleet, "scan_galaxy", lambda *_args: [])
    assert colony.find_empty_slot(_Client(), 1, 44) == (1, 44, 8)


@pytest.mark.parametrize(
    ("maximum", "used", "reserve", "available"),
    [(4, 2, 1, True), (4, 3, 1, False), (1, 0, 1, False), (2, 0, 1, True)],
)
def test_colonisation_slot_guard_leaves_raider_reserve(
    monkeypatch, maximum, used, reserve, available
):
    monkeypatch.setattr(colony.fleetsave, "fleet_slots_max", lambda _levels: maximum)
    monkeypatch.setattr(colony.fleetsave, "active_slots_in_use", lambda _email: used)
    assert colony.fleet_slot_available(EMAIL, {}, reserve_slots=reserve) is available


def test_two_planet_raider_stops_after_single_db_list_read(monkeypatch):
    reads = []
    monkeypatch.setattr(
        colony.db_reader,
        "list_planets_db",
        lambda email: reads.append(email) or [dict(HOME), dict(COLONY)],
    )
    monkeypatch.setattr(
        colony,
        "research_in_progress",
        lambda *_args: pytest.fail("2행성이면 연구 큐까지 보면 안 됨"),
    )
    client = _Client(http_fail=True)
    assert colony.colony_tick(client, EMAIL, {}, max_planets=2, reserve_fleet_slots=1) is None
    assert reads == [EMAIL]
    assert client.session.gets == []


def test_missing_planet_state_fails_closed_without_research_or_ship(monkeypatch):
    monkeypatch.setattr(colony.db_reader, "list_planets_db", lambda _email: [])
    monkeypatch.setattr(colony.planets_mod, "list_planets", lambda _client: [])
    client = _Client()
    note = colony.colony_tick(client, EMAIL, _ready_research_levels(), max_planets=2)
    assert note == "colony idle (행성 목록 조회 실패)"
    assert client.researched == [] and client.ships == []


def test_research_path_obeys_db_queue_guard(monkeypatch):
    monkeypatch.setattr(colony, "_list_planets", lambda *_args: [dict(HOME)])
    client = _Client()

    monkeypatch.setattr(colony, "research_in_progress", lambda *_args: True)
    assert colony.colony_tick(client, EMAIL, _ready_research_levels(), max_planets=2) is None
    assert client.researched == []

    monkeypatch.setattr(colony, "research_in_progress", lambda *_args: False)
    note = colony.colony_tick(client, EMAIL, _ready_research_levels(), max_planets=2)
    assert note and "astrophysics(124)" in note
    assert client.researched == [colony.ASTROPHYSICS]


def test_colony_ship_build_waits_for_shared_unit_queue(monkeypatch):
    monkeypatch.setattr(colony, "_list_planets", lambda *_args: [dict(HOME)])
    monkeypatch.setattr(colony, "colonisation_in_flight", lambda *_args: False)
    client = _Client()

    monkeypatch.setattr(colony, "unit_queue_in_progress", lambda _email: True)
    assert colony.colony_tick(client, EMAIL, _ready_ship_levels(), max_planets=2) is None
    assert client.ships == []

    monkeypatch.setattr(colony, "unit_queue_in_progress", lambda _email: False)
    assert colony.colony_tick(client, EMAIL, _ready_ship_levels(), max_planets=2) == \
        "colony BUILD 식민선(208)x1"
    assert client.ships == [(colony.COLONY_SHIP, 1)]


def test_full_slot_guard_runs_before_any_empty_slot_scan(monkeypatch):
    levels = {**_ready_ship_levels(), colony.COLONY_SHIP: 1}
    monkeypatch.setattr(colony, "_list_planets", lambda *_args: [dict(HOME)])
    monkeypatch.setattr(colony, "colonisation_in_flight", lambda *_args: False)
    monkeypatch.setattr(colony, "fleet_slot_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        colony,
        "find_empty_slot",
        lambda *_args: pytest.fail("슬롯이 없으면 빈 슬롯 조회도 하면 안 됨"),
    )
    assert colony.colony_tick(_Client(), EMAIL, levels, max_planets=2) == \
        "colony idle (함대 슬롯 보류)"


def test_successful_raider_colonisation_uses_db_current_system_and_reserved_slot(monkeypatch):
    levels = {**_ready_ship_levels(), colony.COLONY_SHIP: 1}
    seen = {}
    monkeypatch.setattr(colony, "_list_planets", lambda *_args: [dict(HOME)])
    monkeypatch.setattr(colony, "colonisation_in_flight", lambda *_args: False)

    def slots(email, _levels, *, reserve_slots):
        seen["slot"] = (email, reserve_slots)
        return True

    monkeypatch.setattr(colony, "fleet_slot_available", slots)
    monkeypatch.setattr(
        colony,
        "find_empty_slot_across_systems",
        lambda _client, g, _systems, **_kwargs: (g, 47, 8),
    )
    monkeypatch.setattr(colony, "_load_state", lambda _email: {})
    monkeypatch.setattr(colony, "_save_state", lambda _email, state: seen.setdefault("state", state))
    monkeypatch.setattr(
        colony.fleet,
        "colonize",
        lambda _client, target: seen.setdefault("target", target) or {"ok": True},
    )

    # 위 lambda는 setdefault가 target tuple을 반환하므로 명시 함수로 교체한다.
    def colonize(_client, target):
        seen["target"] = target
        return {"ok": True}

    monkeypatch.setattr(colony.fleet, "colonize", colonize)
    note = colony.colony_tick(
        _Client(), EMAIL, levels, max_planets=2, reserve_fleet_slots=1,
    )
    assert note == "colony COLONIZE 1:47:8 (식민선 파견)"
    assert seen["slot"] == (EMAIL, 1)
    assert seen["target"] == (1, 47, 8)
    assert "1:47:8" in seen["state"]


def test_colonisation_uses_distributed_galaxy_scan_and_zero_cooldown(monkeypatch):
    levels = {**_ready_ship_levels(), colony.COLONY_SHIP: 1}
    scanned: list[int] = []
    sent: list[tuple[int, int, int]] = []
    monkeypatch.setattr(colony, "_list_planets", lambda *_args: [dict(HOME)])
    monkeypatch.setattr(colony, "colonisation_in_flight", lambda *_args: False)
    monkeypatch.setattr(colony, "fleet_slot_available", lambda *_args, **_kwargs: True)

    def find(_client, galaxy, systems, **_kwargs):
        scanned.extend(systems)
        return (galaxy, 45, 8)

    monkeypatch.setattr(colony, "find_empty_slot_across_systems", find)
    monkeypatch.setattr(colony, "_load_state", lambda _email: {"1:45:8": 10_000.0})
    monkeypatch.setattr(colony, "_save_state", lambda *_args: None)
    monkeypatch.setattr(colony.time, "time", lambda: 10_001.0)

    def colonize(_client, target):
        sent.append(target)
        return {"ok": True}

    monkeypatch.setattr(colony.fleet, "colonize", colonize)
    note = colony.colony_tick(
        _Client(), EMAIL, levels, max_planets=2,
        scan_span=5, cooldown_sec=0,
    )

    assert note == "colony COLONIZE 1:45:8 (식민선 파견)"
    assert len(scanned) == colony.MAX_SYSTEM
    assert set(scanned) == set(range(1, colony.MAX_SYSTEM + 1))
    assert sent == [(1, 45, 8)]
