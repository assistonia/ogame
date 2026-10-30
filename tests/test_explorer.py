"""탐험가 원정 슬롯·최소 편성·fail-closed 정책 회귀검증."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import explorer as ex


EMAIL = "explorer@bots.local"
ASTRO = ex.ASTROPHYSICS


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


def test_active_expeditions_matches_server_processed_query(monkeypatch):
    cursor = _Cursor({"n": 2})
    monkeypatch.setattr(ex.db_reader, "_conn", lambda: _Connection(cursor))

    assert ex._active_expeditions(object(), EMAIL) == 2
    sql = " ".join(cursor.sql.lower().split())
    assert "fm.processed = 0" in sql
    assert "fm.mission_type = %s" in sql
    assert "parent_id" not in sql, "귀환 중 type-15도 원정 슬롯을 계속 점유한다"
    assert cursor.params == (EMAIL, ex.fleet.MISSION_EXPEDITION)


def test_active_expeditions_fails_closed_when_db_and_http_fail(monkeypatch):
    monkeypatch.setattr(
        ex.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    monkeypatch.setattr(
        ex.fleet, "fleet_events", lambda _client: (_ for _ in ()).throw(TimeoutError())
    )
    assert ex._active_expeditions(object(), EMAIL) == ex.SOFT_MAX_EXPEDITIONS


def test_discoverer_bonus_uses_actual_class_and_fails_conservative(monkeypatch):
    monkeypatch.setattr(
        ex.db_reader,
        "_conn",
        lambda: _Connection(_Cursor({"character_class": ex.DISCOVERER_CLASS_ID})),
    )
    assert ex._discoverer_slot_bonus(EMAIL) == ex.DISCOVERER_EXP_SLOTS

    monkeypatch.setattr(
        ex.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError("db down"))
    )
    assert ex._discoverer_slot_bonus(EMAIL) == 0


def test_general_bonus_is_included_in_real_fleet_slot_cap(monkeypatch):
    monkeypatch.setattr(
        ex.db_reader,
        "_conn",
        lambda: _Connection(_Cursor({"character_class": ex.GENERAL_CLASS_ID})),
    )
    assert ex.fleet_slots({108: 5}, EMAIL) == 8


def test_identity_reserve_keeps_all_cargo_for_every_expedition_slot():
    levels = {
        ex.LARGE_CARGO: 100,
        ex.SMALL_CARGO: 100,
        ex.LIGHT_FIGHTER: 100,
        ex.PATHFINDER: 20,
        ex.ASTROPHYSICS: 4,
    }
    reserve = ex.expedition_identity_reserve(levels)
    assert reserve == {
        ex.LARGE_CARGO: 100,
        ex.SMALL_CARGO: 100,
        ex.PATHFINDER: 4,
    }
    assert ex.expedition_fleet_ready(
        ex.build_expedition_fleet(reserve, risk_fraction=1.0)
    )


def test_identity_reserve_uses_heavy_fighter_fallback():
    reserve = ex.expedition_identity_reserve(
        {ex.LARGE_CARGO: 4, ex.HEAVY_FIGHTER: 9}
    )
    assert reserve == {ex.LARGE_CARGO: 4, ex.HEAVY_FIGHTER: 2}
    assert ex.expedition_fleet_ready(
        ex.build_expedition_fleet(reserve, risk_fraction=1.0)
    )


@pytest.mark.parametrize(
    "ships",
    [
        {ex.LARGE_CARGO: 2},
        {ex.LIGHT_FIGHTER: 3},
        {ex.PATHFINDER: 1, ex.LARGE_CARGO: 1},
    ],
)
def test_expedition_fleet_ready_requires_three_ships_and_cargo(ships):
    assert not ex.expedition_fleet_ready(ships)


def test_expedition_fleet_ready_allows_pathfinderless_cargo_fleet():
    assert ex.expedition_fleet_ready({ex.LARGE_CARGO: 3})


def test_tick_uses_real_class_slot_cap_and_does_not_send_when_full(monkeypatch):
    levels = {
        ASTRO: 2,
        ex.LARGE_CARGO: 10,
        ex.LIGHT_FIGHTER: 10,
    }
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda _email: 0)
    monkeypatch.setattr(ex, "_mission_state", lambda _email: (1, 1, 0))
    monkeypatch.setattr(
        ex, "_own_system", lambda *_args: pytest.fail("슬롯 만원이면 좌표 조회 금지")
    )
    assert ex._expedition_tick_inner(object(), EMAIL, levels) == (
        "expedition idle (진행중 1/1)"
    )


def test_tick_sends_at_most_one_expedition_per_tick(monkeypatch):
    levels = {
        ASTRO: 2,
        ex.LARGE_CARGO: 10,
        ex.LIGHT_FIGHTER: 10,
        ex.PATHFINDER: 2,
    }
    sent = []
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda _email: 2)
    monkeypatch.setattr(ex, "_mission_state", lambda _email: (0, 0, 0))
    monkeypatch.setattr(ex, "_own_system", lambda *_args: (1, 20))
    monkeypatch.setattr(
        ex.fleet,
        "send_fleet",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(ex, "_ship_name", lambda tid: f"ship{tid}")

    note = ex._expedition_tick_inner(object(), EMAIL, levels)
    assert note and "expedition SEND" in note
    assert len(sent) == 1
    assert sent[0][0][1] == ex.fleet.MISSION_EXPEDITION


def test_full_autonomy_pathfinderless_class_can_send(monkeypatch):
    levels = {
        ASTRO: 4,
        108: 4,
        ex.LARGE_CARGO: 40,
        ex.LIGHT_FIGHTER: 8,
    }
    sent = []
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda _email: 0)
    monkeypatch.setattr(ex, "_mission_state", lambda _email: (0, 0, 0))
    monkeypatch.setattr(ex.db_reader, "get_account_ship_totals_db", lambda _email: levels)
    monkeypatch.setattr(ex, "_own_system", lambda *_args: (1, 20))
    monkeypatch.setattr(
        ex.fleet,
        "send_fleet",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(ex, "_ship_name", lambda tid: f"ship{tid}")

    note = ex._expedition_tick_inner(
        object(),
        "general@bots.local",
        levels,
        aggressive=True,
        risk_fraction=1.0,
        total_exposure_fraction=1.0,
    )

    assert note and "expedition SEND" in note
    ships = sent[0][0][3]
    assert ex.PATHFINDER not in ships
    assert ships[ex.LARGE_CARGO] > ex.EXP_TEMPLATE[ex.LARGE_CARGO]
    assert sum(ships.get(t, 0) for t in ex.COMBAT_TIER_ORDER) <= 1


def test_raid_reservation_covers_every_fillable_expedition_slot(monkeypatch):
    levels = {
        ASTRO: 4,
        108: 6,
        ex.LARGE_CARGO: 100,
        ex.PATHFINDER: 4,
    }
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda _email: 2)
    monkeypatch.setattr(ex, "_mission_state", lambda _email: (0, 0, 0))
    monkeypatch.setattr(ex.db_reader, "get_account_ship_totals_db", lambda _email: levels)

    slots, ships = ex.expedition_raid_reservation(
        EMAIL,
        levels,
        aggressive=True,
        use_server_cap=True,
        risk_fraction=1.0,
        total_exposure_fraction=1.0,
    )

    assert slots == 4
    assert ships == {ex.PATHFINDER: 4, ex.LARGE_CARGO: 100}


def test_raid_reservation_does_not_idle_account_without_viable_expedition(monkeypatch):
    levels = {ASTRO: 4, 108: 6, ex.LARGE_CARGO: 2}
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda _email: 2)
    monkeypatch.setattr(ex, "_mission_state", lambda _email: (0, 0, 0))
    monkeypatch.setattr(ex.db_reader, "get_account_ship_totals_db", lambda _email: levels)

    assert ex.expedition_raid_reservation(
        EMAIL,
        levels,
        aggressive=True,
        use_server_cap=True,
        risk_fraction=1.0,
        total_exposure_fraction=1.0,
    ) == (0, {})
