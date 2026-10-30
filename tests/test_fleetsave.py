"""플릿세이브 의사결정/안전 정책 단위검증 (실 DB/HTTP 파견 없음).

OGameX 소스와 맞춘 핵심 불변식:

* Transport(3)는 왕복이고 Deployment(4)는 편도다. 이 모듈은 transport만 호출한다.
* ``getFleetSlotsInUse``는 ``processed=0``인 송신 미션(귀환 row 포함) 중 type 10만 뺀다.
* 외부 Transport는 0자원이어도 수신자 메시지를 만들므로 기본 외부 allowlist는 비어 있다.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import fleetsave as fs


EMAIL = "raider@bots.local"
OWN = {
    "id": 1, "name": "home", "galaxy": 1, "system": 50, "position": 5,
    "is_current": True,
}
COLONY = {
    "id": 2, "name": "colony", "galaxy": 1, "system": 51, "position": 8,
    "is_current": False,
}


def _row(g: int, s: int, p: int, **overrides) -> dict:
    row = {
        "galaxy": g,
        "system": s,
        "position": p,
        "player_id": 100,
        "inactive": True,
        "long_inactive": False,
        "vacation": False,
        "admin": False,
        "banned": False,
        "available_missions": [fs.fleet.MISSION_TRANSPORT],
    }
    row.update(overrides)
    return row


# --- 가치/함선/거리 순수 계산 --------------------------------------------
def test_ship_values_match_ogamex_build_cost_totals():
    assert fs.SHIP_VALUE == {
        202: 4_000,
        203: 12_000,
        204: 4_000,
        205: 10_000,
        206: 29_000,
        207: 60_000,
        211: 90_000,
        213: 125_000,
        214: 10_000_000,
        215: 85_000,
        218: 160_000,
        219: 31_000,
    }


def test_fleet_value_counts_registered_types_only_and_accepts_numeric_strings():
    assert fs.fleet_value({"202": "2", 204: 3, 212: 999}) == 20_000


def test_savable_ships_excludes_stationary_and_other_module_ships():
    levels = {
        202: 2, 204: 1, 208: 4, 209: 5, 210: 6, 212: 7, 217: 8,
        203: 0, 205: -1,
    }
    assert fs.savable_ships(levels) == {202: 2, 204: 1}


def test_non_explorer_has_no_fixed_identity_reserve():
    ships = {202: 8, 203: 20, 204: 30, 219: 4}
    assert fs.ships_after_identity_reserve(ships, "raider", ships) == (ships, {})


def test_explorer_reserves_all_expedition_cargo_and_saves_only_combat_excess():
    ships = {202: 10, 203: 20, 204: 30, 219: 4}
    levels = {**ships, 124: 4}
    to_save, reserved = fs.ships_after_identity_reserve(ships, "explorer", levels)
    assert reserved == {202: 10, 203: 20, 219: 4}
    assert to_save == {204: 30}


def test_fleet_slots_formula_is_conservative_without_general_bonus():
    assert fs.fleet_slots_max({}) == 1
    assert fs.fleet_slots_max({fs.COMPUTER_TECHNOLOGY: 3}) == 4


def test_system_distance_uses_ogamex_donut_boundary():
    assert fs._system_distance(1, 499) == 1
    assert fs._system_distance(1, 2) == 1
    assert fs._system_distance(100, 110) == 10


def test_coordinate_distance_matches_ogamex_base_formulas():
    own = {"galaxy": 1, "system": 1, "position": 5}
    assert fs._coordinate_distance(
        {"galaxy": 1, "system": 1, "position": 6}, own
    ) == 1_005
    assert fs._coordinate_distance(
        {"galaxy": 1, "system": 2, "position": 1}, own
    ) == 2_795
    assert fs._coordinate_distance(
        {"galaxy": 2, "system": 1, "position": 1}, own
    ) == 20_000


def test_nearest_colony_uses_flight_distance_not_galaxy_first_heuristic():
    own = {"galaxy": 1, "system": 1, "position": 5}
    far_same_galaxy = {"id": 2, "galaxy": 1, "system": 200, "position": 5}
    nearer_other_galaxy = {"id": 3, "galaxy": 2, "system": 400, "position": 15}
    assert fs.nearest_colony([far_same_galaxy, nearer_other_galaxy], own)["id"] == 3
    assert fs.nearest_colony([], own) is None


# --- 슬롯 DB 복제 ---------------------------------------------------------
class _Cursor:
    def __init__(self, row):
        self.row = row
        self.sql = None
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


def test_active_slots_query_matches_server_and_includes_return_rows(monkeypatch):
    cursor = _Cursor({"n": 3})
    monkeypatch.setattr(fs.db_reader, "_conn", lambda: _Connection(cursor))

    assert fs.active_slots_in_use(EMAIL) == 3
    normalized = " ".join(cursor.sql.split()).lower()
    assert "fm.user_id" in normalized and "u.email = %s" in normalized
    assert "fm.processed = 0" in normalized
    assert "fm.mission_type != 10" in normalized
    assert "parent_id" not in normalized, "귀환 row(parent_id 있음)도 슬롯 1개로 세야 한다"
    assert cursor.params == (EMAIL,)


def test_active_slots_empty_result_is_zero(monkeypatch):
    monkeypatch.setattr(fs.db_reader, "_conn", lambda: _Connection(_Cursor(None)))
    assert fs.active_slots_in_use(EMAIL) == 0


def test_active_slots_db_failure_fails_closed(monkeypatch):
    def fail():
        raise OSError("db down")

    monkeypatch.setattr(fs.db_reader, "_conn", fail)
    assert fs.active_slots_in_use(EMAIL) == 10 ** 6


def test_active_fleetsaves_counts_only_live_transport_rows(monkeypatch):
    cursor = _Cursor({"n": 2})
    monkeypatch.setattr(fs.db_reader, "_conn", lambda: _Connection(cursor))

    assert fs.active_fleetsaves_in_use(EMAIL) == 2
    normalized = " ".join(cursor.sql.split()).lower()
    assert "fm.processed = 0" in normalized
    assert "fm.mission_type = 3" in normalized
    assert "parent_id" not in normalized
    assert cursor.params == (EMAIL,)


def test_active_fleetsaves_db_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(fs.db_reader, "_conn", lambda: (_ for _ in ()).throw(OSError()))
    assert fs.active_fleetsaves_in_use(EMAIL) == 10 ** 6


# --- 외부 목적지 안전 정책 ------------------------------------------------
def test_empty_foreign_allowlist_skips_http_scan(monkeypatch):
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {})
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy",
        lambda *_args: pytest.fail("빈 allowlist에서는 galaxy HTTP를 호출하면 안 됨"),
    )
    assert fs._scan_foreign_target(object(), OWN) is None


def test_foreign_scan_requires_allowlist_inactive_and_transport(monkeypatch):
    candidates = [
        _row(1, 50, 6, inactive=False),
        _row(1, 50, 7, vacation=True),
        _row(1, 50, 8, admin=True),
        _row(1, 50, 9, banned=True),
        _row(1, 50, 10, available_missions=[]),
        _row(1, 50, 11),  # 유일한 정상 후보
        {"galaxy": 1, "system": 50},  # 비정상 응답은 예외 없이 무시
        _row(2, 50, 12),  # 요청한 은하 밖 응답도 무시
    ]
    monkeypatch.setattr(
        fs, "FOREIGN_TARGET_ALLOWLIST",
        {coord: 100 for coord in (
            {(1, 50, p) for p in range(6, 12)} | {(2, 50, 12)}
        )},
    )
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy", lambda _client, _g, s: candidates if s == 50 else [],
    )
    assert fs._scan_foreign_target(object(), OWN) == (1, 50, 11)


def test_foreign_scan_prefers_long_inactive_before_distance(monkeypatch):
    monkeypatch.setattr(
        fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 50, 6): 100, (1, 51, 8): 100}
    )

    def scan(_client, _g, s):
        if s == 50:
            return [_row(1, 50, 6)]
        if s == 51:
            return [_row(1, 51, 8, long_inactive=True)]
        return []

    monkeypatch.setattr(fs.fleet, "scan_galaxy", scan)
    assert fs._scan_foreign_target(object(), OWN) == (1, 51, 8)


def test_foreign_scan_rejects_reassigned_allowlisted_coordinate(monkeypatch):
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 50, 6): 100})
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy",
        lambda _client, _g, s: [_row(1, 50, 6, player_id=999)] if s == 50 else [],
    )
    assert fs._scan_foreign_target(object(), OWN) is None


def test_foreign_scan_wraps_system_boundary(monkeypatch):
    own = {**OWN, "system": 1}
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 499, 8): 100})
    scanned = []

    def scan(_client, _g, s):
        scanned.append(s)
        return [_row(1, 499, 8)] if s == 499 else []

    monkeypatch.setattr(fs.fleet, "scan_galaxy", scan)
    assert fs._scan_foreign_target(object(), own) == (1, 499, 8)
    assert set(scanned) == {1, 2, 499}


def test_foreign_scan_skips_protected_human_system_even_if_allowlisted(monkeypatch):
    own = {**OWN, "system": 43}
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 44, 5): 100})
    scanned = []
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy",
        lambda _client, _g, s: scanned.append(s) or [_row(1, s, 5)],
    )
    assert fs._scan_foreign_target(object(), own) is None
    assert 44 not in scanned


def test_foreign_scan_continues_after_one_system_failure(monkeypatch):
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 51, 8): 100})

    def scan(_client, _g, s):
        if s == 50:
            raise TimeoutError("scan timeout")
        return [_row(1, 51, 8)] if s == 51 else []

    monkeypatch.setattr(fs.fleet, "scan_galaxy", scan)
    assert fs._scan_foreign_target(object(), OWN) == (1, 51, 8)


def test_pick_target_prefers_own_colony_without_scanning(monkeypatch):
    monkeypatch.setattr(
        fs, "_scan_foreign_target",
        lambda *_args: pytest.fail("자기 식민지가 있으면 외부 스캔하면 안 됨"),
    )
    assert fs.pick_target(object(), EMAIL, OWN, [COLONY]) == ((1, 51, 8), True)


def test_pick_target_returns_none_for_single_planet_by_default(monkeypatch):
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {})
    assert fs.pick_target(object(), EMAIL, OWN, []) == (None, False)


# --- 메인 tick: 모든 외부 부작용 mock ------------------------------------
def _patch_ready(
    monkeypatch,
    *,
    parked: dict[int, int] | None = None,
    planets: list[dict] | None = None,
    slots: int = 0,
    saves: int = 0,
    response: dict | None = None,
):
    parked = {202: 3, fs.COMPUTER_TECHNOLOGY: 1} if parked is None else parked
    planets = [OWN, COLONY] if planets is None else planets
    response = {"ok": True, "message": "sent"} if response is None else response
    sent = []
    monkeypatch.setattr(fs.db_reader, "get_levels_db", lambda _email: dict(parked))
    monkeypatch.setattr(fs.db_reader, "list_planets_db", lambda _email: list(planets))
    monkeypatch.setattr(fs, "active_slots_in_use", lambda _email: slots)
    monkeypatch.setattr(fs, "active_fleetsaves_in_use", lambda _email: saves)
    monkeypatch.setattr(fs, "_ship_name", lambda tid: f"ship{tid}")

    def transport(client, target, ships, resources, **kw):
        sent.append((client, target, dict(ships), resources, dict(kw)))
        return dict(response)

    monkeypatch.setattr(fs.fleet, "transport", transport)
    return sent


def test_counter_mode_keeps_defense_fleet_parked_during_incoming_attack(monkeypatch):
    monkeypatch.setattr(fs, "incoming_hostile_attacks", lambda _email: 2)
    monkeypatch.setattr(
        fs.db_reader,
        "get_levels_db",
        lambda _email: pytest.fail("counter hold는 함대를 읽거나 파견하지 않아야 함"),
    )

    note = fs.fleetsave_tick(
        object(), EMAIL, {204: 100}, "raider", 1, mode="counter"
    )

    assert note == "fleetsave COUNTER HOLD (접근 공격 2건, 방어함대 유지)"


def test_counter_mode_returns_to_adaptive_after_attack(monkeypatch):
    sent = _patch_ready(monkeypatch)
    monkeypatch.setattr(fs, "incoming_hostile_attacks", lambda _email: 0)
    monkeypatch.setattr(fs, "recent_hostile_attack", lambda _email: True)

    note = fs.fleetsave_tick(
        object(), EMAIL, {}, "raider", 1, mode="counter"
    )

    assert note and "fleetsave SEND" in note
    assert len(sent) == 1


def test_tick_refreshes_parked_ships_and_does_not_reuse_stale_snapshot(monkeypatch):
    sent = _patch_ready(monkeypatch, parked={fs.COMPUTER_TECHNOLOGY: 3})
    stale = {202: 99, fs.COMPUTER_TECHNOLOGY: 3}
    assert fs.fleetsave_tick(object(), EMAIL, stale, "raider", 1) is None
    assert sent == []


def test_tick_falls_back_to_snapshot_when_parked_db_read_fails(monkeypatch):
    sent = _patch_ready(monkeypatch)

    def fail(_email):
        raise OSError("db down")

    monkeypatch.setattr(fs.db_reader, "get_levels_db", fail)
    snapshot = {202: 2, fs.COMPUTER_TECHNOLOGY: 1}
    note = fs.fleetsave_tick(object(), EMAIL, snapshot, "raider", 8_000)
    assert note and "fleetsave SEND" in note
    assert sent[0][2] == {202: 2}


def test_tick_skips_value_below_threshold_before_slot_or_planet_queries(monkeypatch):
    monkeypatch.setattr(fs.db_reader, "get_levels_db", lambda _email: {202: 1})
    monkeypatch.setattr(
        fs, "active_slots_in_use",
        lambda _email: pytest.fail("임계 미만이면 슬롯 조회 전에 끝나야 함"),
    )
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 4_001) is None


def test_tick_with_zero_threshold_still_never_dispatches_empty_fleet(monkeypatch):
    sent = _patch_ready(monkeypatch, parked={}, planets=[OWN, COLONY])
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 0) is None
    assert sent == []


def test_tick_skips_when_conservative_slots_are_full(monkeypatch):
    sent = _patch_ready(
        monkeypatch,
        parked={202: 2, fs.COMPUTER_TECHNOLOGY: 2},
        slots=3,
    )
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1) is None
    assert sent == []


def test_tick_skips_when_one_fleetsave_is_already_active(monkeypatch):
    sent = _patch_ready(
        monkeypatch,
        parked={202: 20, fs.COMPUTER_TECHNOLOGY: 3},
        slots=1,
        saves=1,
    )
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1) is None
    assert sent == []


def test_tick_zero_count_cap_uses_remaining_server_slot(monkeypatch):
    sent = _patch_ready(
        monkeypatch,
        parked={202: 20, fs.COMPUTER_TECHNOLOGY: 3},
        slots=1,
        saves=1,
    )

    note = fs.fleetsave_tick(
        object(), EMAIL, {}, "raider", 1,
        max_active_fleetsaves=0,
        fleet_slot_cap=6,
    )

    assert note and "fleetsave SEND" in note
    assert len(sent) == 1


def test_tick_skips_without_current_planet(monkeypatch):
    sent = _patch_ready(monkeypatch, planets=[COLONY])
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1) is None
    assert sent == []


def test_tick_single_planet_safe_default_has_no_scan_or_transport(monkeypatch):
    sent = _patch_ready(monkeypatch, planets=[OWN])
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {})
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy",
        lambda *_args: pytest.fail("외부 allowlist가 없으면 스캔 금지"),
    )
    assert fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1) is None
    assert sent == []


def test_tick_sends_all_and_only_savable_ships_as_zero_cargo_transport(monkeypatch):
    client = object()
    parked = {
        202: 2, 204: 3, 209: 4, 210: 5, 212: 6, 217: 7,
        fs.COMPUTER_TECHNOLOGY: 1,
    }
    sent = _patch_ready(monkeypatch, parked=parked)

    note = fs.fleetsave_tick(client, EMAIL, {}, "raider", 20_000)

    assert note == (
        "fleetsave SEND 1:51:8 "
        "(식민지, ship2022, ship2043, 가치~20000, 10%)"
    )
    assert sent == [
        (client, (1, 51, 8), {202: 2, 204: 3}, (0, 0, 0), {"speed": 1})
    ]


def test_explorer_tick_keeps_all_expedition_cargo_reserve(monkeypatch):
    parked = {
        202: 10, 203: 20, 204: 30, 219: 4,
        124: 4,
        fs.COMPUTER_TECHNOLOGY: 3,
    }
    sent = _patch_ready(monkeypatch, parked=parked)

    note = fs.fleetsave_tick(object(), EMAIL, {}, "explorer", 1)

    assert note and "원정예약~" in note
    assert sent[0][2] == {204: 30}


def test_explorer_tick_never_saves_identity_reserve_only(monkeypatch):
    parked = {
        202: 2, 203: 4, 204: 1, 219: 1,
        fs.COMPUTER_TECHNOLOGY: 3,
    }
    sent = _patch_ready(monkeypatch, parked=parked)

    assert fs.fleetsave_tick(object(), EMAIL, {}, "explorer", 1) is None
    assert sent == []


def test_tick_reports_transport_rejection_without_raising(monkeypatch):
    sent = _patch_ready(
        monkeypatch, response={"ok": False, "message": "not enough deuterium"}
    )
    note = fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1)
    assert note == "fleetsave idle (not enough deuterium)"
    assert len(sent) == 1


def test_tick_catches_unexpected_transport_exception(monkeypatch):
    _patch_ready(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("network broke")

    monkeypatch.setattr(fs.fleet, "transport", fail)
    note = fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1)
    assert note == "fleetsave err RuntimeError: network broke"


def test_tick_uses_allowlisted_foreign_target_only_when_explicit(monkeypatch):
    sent = _patch_ready(monkeypatch, planets=[OWN])
    monkeypatch.setattr(fs, "FOREIGN_TARGET_ALLOWLIST", {(1, 51, 9): 100})
    monkeypatch.setattr(
        fs.fleet, "scan_galaxy",
        lambda _client, _g, s: [_row(1, 51, 9)] if s == 51 else [],
    )
    note = fs.fleetsave_tick(object(), EMAIL, {}, "raider", 1)
    assert note and "1:51:9 (이웃" in note
    assert sent[0][1] == (1, 51, 9)
