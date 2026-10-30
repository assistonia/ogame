"""레이더 DB 첩보 intel 안전 게이트와 battle_sim shadow 배선 회귀 테스트."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import raider as rd


COORD = (1, 50, 7)


def test_discoverer_expected_loot_uses_server_75_percent():
    assert rd.expected_raid_loot({"res_sum": 1_000_000}, 75) == 750_000
    assert rd.expected_raid_loot({"res_sum": 1_000_000}, 50) == 500_000


def test_zero_defense_force_uses_large_cargo_when_it_is_more_efficient():
    ships = rd._attack_force(
        100_000,
        have_lf=4,
        have_hf=0,
        have_sc=0,
        have_lc=4,
    )
    assert ships == {rd.LIGHT_FIGHTER: 4, rd.LARGE_CARGO: 4}


def _resources(total_each: int = 10000) -> str:
    return (f'{{"metal":{total_each},"crystal":{total_each},'
            f'"deuterium":{total_each},"energy":100}}')


def _report(*, fresh=True, defense=None, ships=None, research=None, res_sum=30000):
    return {
        "fresh": fresh,
        "age_sec": 10 if fresh else rd.SPY_REPORT_MAX_AGE_SEC + 1,
        "resources": {"metal": 10000, "crystal": 10000, "deuterium": 10000},
        "res_sum": res_sum,
        "ships": ships,
        "defense_units": defense,
        "defense": sum(defense.values()) if defense is not None else None,
        "research": research,
    }


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_db_latest_row_preserves_unknown_and_known_zero(monkeypatch):
    """최신 defense=NULL이면 과거 known-zero로 후퇴하지 않고, []은 실제 0으로 보존."""
    rows = [
        {"report_id": 9, "g": 1, "s": 50, "p": 7, "resources": _resources(),
         "ships": None, "defense": None, "research": None, "age_sec": 5,
         "created_at": "new"},
        {"report_id": 8, "g": 1, "s": 50, "p": 7, "resources": _resources(),
         "ships": "[]", "defense": "[]", "research": "[]", "age_sec": 15,
         "created_at": "old"},
        {"report_id": 7, "g": 1, "s": 50, "p": 8, "resources": _resources(),
         "ships": "[]", "defense": "[]", "research": "[]", "age_sec": 20,
         "created_at": "other"},
    ]
    cursor = _Cursor(rows)
    monkeypatch.setattr(rd.db_reader, "_conn", lambda: _Conn(cursor))

    reports = rd.read_spy_reports("raider@bots.local")

    newest = reports[(1, 50, 7)]
    assert newest["report_id"] == 9
    assert newest["defense"] is None and newest["defense_units"] is None
    assert newest["ships"] is None and newest["research"] is None
    known_zero = reports[(1, 50, 8)]
    assert known_zero["defense"] == 0 and known_zero["defense_units"] == {}
    assert known_zero["ships"] == {} and known_zero["research"] == {}
    assert known_zero["res_sum"] == 30000 and known_zero["fresh"] is True
    assert cursor.params == ("raider@bots.local",)
    assert "messages" in cursor.sql and "ORDER BY er.created_at DESC" in cursor.sql


def test_report_safety_requires_fresh_explicit_defense_zero():
    assert rd._safe_zero_defense_report(_report(defense={}, ships={})) is True
    assert rd._safe_zero_defense_report(_report(defense=None, ships=None)) is False
    assert rd._safe_zero_defense_report(_report(defense={}, ships=None)) is False
    assert rd._safe_zero_defense_report(_report(fresh=False, defense={}, ships={})) is False
    assert rd._safe_zero_defense_report(_report(defense={401: 1}, ships={})) is False
    assert rd._safe_zero_defense_report(_report(defense={}, ships={204: 1})) is False
    assert rd._safe_zero_defense_report(_report(defense={}, ships={212: 20, 202: 3})) is True


def _patch_tick(monkeypatch, reports, candidates=None):
    # 이 파일은 preflight 이전의 shadow/canary 계약을 독립 검증한다.
    # 운영 기본값은 test_raider_preflight.py에서 따로 고정한다.
    monkeypatch.setattr(rd, "RAID_ATTACK_PREFLIGHT_ENABLED", False)
    candidates = candidates or [{"coord": COORD, "inactive": True}]
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _c: (0, False, 0))
    monkeypatch.setattr(rd, "own_coords", lambda _c: (1, 50, 5))
    monkeypatch.setattr(rd, "scan_targets", lambda *_a, **_k: list(candidates))
    monkeypatch.setattr(rd, "read_spy_reports", lambda _email: dict(reports))
    monkeypatch.setattr(rd, "_load_state", lambda _email: {"targets": {}})
    monkeypatch.setattr(rd, "_save_state", lambda *_a, **_k: None)
    monkeypatch.setattr(rd.time, "time", lambda: 10000.0)


@pytest.mark.parametrize("owned", [1, 3, 4, 6])
def test_unknown_defense_never_attacks_and_accumulates_full_intel_probes(monkeypatch, owned):
    _patch_tick(monkeypatch, {COORD: _report(defense=None, ships=None, research=None)})
    attacks, spies = [], []
    monkeypatch.setattr(rd.fleet, "attack", lambda *a, **k: attacks.append((a, k)) or {"ok": True})
    monkeypatch.setattr(rd.fleet, "spy",
                        lambda _c, target, probes: spies.append((target, probes)) or {"ok": True})

    note = rd._raid_tick_inner(object(), "r@x", {rd.ESPIONAGE_PROBE: owned})

    assert attacks == []
    assert spies == []
    assert note and f"정찰기 축적 {owned}/7" in note


def test_unknown_defense_sends_seven_for_full_intel(monkeypatch):
    _patch_tick(monkeypatch, {COORD: _report(defense=None, ships=None, research=None)})
    attacks, spies = [], []
    monkeypatch.setattr(rd.fleet, "attack", lambda *a, **k: attacks.append((a, k)) or {"ok": True})
    monkeypatch.setattr(rd.fleet, "spy",
                        lambda _c, target, probes: spies.append((target, probes)) or {"ok": True})

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.ESPIONAGE_PROBE: 7, rd.ESPIONAGE_TECH: 3}
    )

    assert attacks == [] and spies == [(COORD, 7)]
    assert note and "정찰기7" in note and "full-intel 목표" in note


def test_dynamic_probe_formula_matches_server_thresholds():
    def known(defender_level):
        return _report(defense={}, ships={}, research={rd.ESPIONAGE_TECH: defender_level})

    assert rd.desired_spy_probes(known(3), attacker_espionage=3) == 7
    assert rd.desired_spy_probes(known(4), attacker_espionage=3) == 8
    assert rd.desired_spy_probes(known(5), attacker_espionage=3) == 11
    assert rd.desired_spy_probes(known(3), attacker_espionage=7) == 1
    assert rd.desired_spy_probes(None, 3, previous_sent=0) == 7
    assert rd.desired_spy_probes(None, 3, previous_sent=7) == 8
    assert rd.desired_spy_probes(None, 3, previous_sent=8) == 11
    assert rd.desired_spy_probes(None, 3, previous_sent=11) == 16
    assert rd.desired_spy_probes(None, 3, previous_sent=16) == 23
    assert rd.desired_spy_probes(None, 3, previous_sent=23) == 32
    assert rd.desired_spy_probes(known(7), attacker_espionage=3) == 23


def test_spy_losses_back_off_exponentially_and_replay_is_idempotent():
    state = {"targets": {}}

    assert rd._apply_spy_loss(state, COORD, 1000, message_id=1) == 1800
    assert rd._apply_spy_loss(state, COORD, 1100, message_id=2) == 7200
    assert rd._apply_spy_loss(state, COORD, 1200, message_id=3) == 28800
    assert rd._spy_quarantined(state, COORD, 1201)
    losses = list(state["targets"]["1:50:7"]["spy_losses"])

    rd._apply_spy_loss(state, COORD, 1200, message_id=3)
    assert state["targets"]["1:50:7"]["spy_losses"] == losses


def test_target_rotation_uses_account_specific_stable_tie_break():
    candidates = [
        {"coord": (1, 50, 6), "inactive": True, "player_id": 6},
        {"coord": (1, 50, 7), "inactive": True, "player_id": 7},
        {"coord": (1, 50, 8), "inactive": True, "player_id": 8},
    ]

    first = rd._prioritize_targets(
        candidates, {"targets": {}}, set(), email="ai01@bots.local", now=1000
    )
    repeat = rd._prioritize_targets(
        candidates, {"targets": {}}, set(), email="ai01@bots.local", now=1000
    )
    assert [row["coord"] for row in first] == [row["coord"] for row in repeat]
    orders = {
        tuple(
            row["coord"]
            for row in rd._prioritize_targets(
                candidates,
                {"targets": {}},
                set(),
                email=f"ai{index:02d}@bots.local",
                now=1000,
            )
        )
        for index in range(1, 11)
    }
    assert len(orders) > 1


def test_stale_zero_defense_and_no_probe_do_not_use_blind_fallback(monkeypatch):
    _patch_tick(monkeypatch, {COORD: _report(fresh=False, defense={}, ships={}, research={})})
    attacks = []
    monkeypatch.setattr(rd.fleet, "attack", lambda *a, **k: attacks.append((a, k)) or {"ok": True})

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 10, rd.SMALL_CARGO: 3}
    )

    assert attacks == []
    assert note and "ATTACK*" not in note and "stale" in note


def _decision(recommend: bool) -> dict:
    return {
        "recommend": recommend,
        "sim": {"win_prob": 0.0, "attacker_loss_value": 1234,
                "expected_debris": (0.0, 0.0)},
        "ev": -1234,
        "log": f"[battle_sim/log] recommend={recommend}",
    }


def test_shadow_reject_cannot_block_existing_safe_attack_and_records_prediction(monkeypatch):
    """recommend=False도 기존 fresh/known defense0 공격을 막지 못한다(SHADOW ONLY)."""
    _patch_tick(monkeypatch, {COORD: _report(defense={}, ships={}, research={})})
    decide_calls, attacks, predictions = [], [], []
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **kw: decide_calls.append(kw) or _decision(False),
    )
    monkeypatch.setattr(
        rd.fleet, "attack",
        lambda _c, target, ships: attacks.append((target, ships)) or {"ok": True},
    )
    monkeypatch.setattr(
        rd.battle_guard, "record_prediction",
        lambda *a, **k: predictions.append((a, k)),
    )

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 8, rd.SMALL_CARGO: 4}
    )

    assert len(decide_calls) == 1 and decide_calls[0]["mode"] == "log"
    assert attacks and attacks[0][0] == COORD
    assert len(predictions) == 1 and predictions[0][1]["mode"] == "log"
    assert note and "SHADOW ONLY" in note and "raid ATTACK" in note


def test_shadow_approve_cannot_attack_known_defense(monkeypatch):
    """recommend=True도 기존 defense0 하드게이트를 우회하지 못한다."""
    _patch_tick(monkeypatch, {
        COORD: _report(defense={401: 10}, ships={}, research={109: 2, 110: 2, 111: 2})
    })
    decide_calls, attacks = [], []
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **kw: decide_calls.append(kw) or _decision(True),
    )
    monkeypatch.setattr(rd.fleet, "attack",
                        lambda *a, **k: attacks.append((a, k)) or {"ok": True})

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 10, rd.SMALL_CARGO: 4}
    )

    assert len(decide_calls) == 1
    assert attacks == []
    assert note and "SHADOW ONLY" in note and "raid idle" in note


def test_research_unknown_is_partial_conservative_and_not_simulated(monkeypatch):
    report = _report(defense={}, ships={}, research=None)
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **_kw: pytest.fail("unknown research must not be fabricated as level zero"),
    )

    shadow = rd._battle_sim_shadow(
        [{"coord": COORD}], {COORD: report}, {109: 3, 110: 3, 111: 3},
        have_lf=10, have_hf=0, have_sc=4, have_rec=0,
        min_loot=rd.MIN_EXPECTED_LOOT, debris_factor=rd.DEBRIS_NO_RECYCLER_FACTOR,
    )

    assert shadow is not None
    assert "partial/conservative" in shadow["log"] and "research 미상" in shadow["log"]
    assert "decision" not in shadow


def test_shadow_evaluates_at_most_one_of_multiple_targets(monkeypatch):
    c2 = (1, 50, 8)
    calls = []
    monkeypatch.setattr(rd.battle_sim, "decide",
                        lambda **kw: calls.append(kw) or _decision(True))
    reports = {
        COORD: _report(defense={}, ships={}, research={}),
        c2: _report(defense={401: 1}, ships={}, research={}),
    }

    shadow = rd._battle_sim_shadow(
        [{"coord": COORD}, {"coord": c2}], reports, {},
        have_lf=10, have_hf=0, have_sc=4, have_rec=0,
        min_loot=rd.MIN_EXPECTED_LOOT, debris_factor=rd.DEBRIS_NO_RECYCLER_FACTOR,
    )

    assert shadow is not None and len(calls) == 1


def test_canary_live_attacks_only_same_full_intel_recommended_target(monkeypatch):
    report = _report(
        defense={401: 1}, ships={},
        research={109: 2, 110: 2, 111: 2, rd.ESPIONAGE_TECH: 3},
    )
    _patch_tick(monkeypatch, {COORD: report})
    monkeypatch.setattr(rd, "_canary_live_ready", lambda _e, _lv: True)
    monkeypatch.setattr(rd.battle_sim, "is_tripped", lambda _e: False)
    decide_calls, attacks, predictions = [], [], []
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **kw: decide_calls.append(kw) or _decision(True),
    )
    monkeypatch.setattr(
        rd.fleet, "attack",
        lambda _c, target, ships: attacks.append((target, ships)) or {"ok": True},
    )
    monkeypatch.setattr(
        rd.battle_guard, "record_prediction",
        lambda *a, **k: predictions.append((a, k)),
    )

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 10, rd.SMALL_CARGO: 4, 108: 1},
        battle_mode=rd.BATTLE_SIM_CANARY_MODE,
    )

    assert len(decide_calls) == 1 and decide_calls[0]["mode"] == "live"
    assert attacks and attacks[0][0] == COORD
    assert attacks[0][1] == decide_calls[0]["attacker"]
    assert predictions and predictions[0][1]["mode"] == "live"
    assert note and "ATTACK[CANARY]" in note and "full-intel" in note


def test_canary_live_readiness_is_ai06_allowlisted_and_requires_two_planets(monkeypatch):
    two = [{"id": 1}, {"id": 2}]
    monkeypatch.setattr(rd.db_reader, "list_planets_db", lambda _email: two)
    assert rd._canary_live_ready("ai06@bots.local", {108: 1}) is True
    assert rd._canary_live_ready("ai01@bots.local", {108: 1}) is False
    monkeypatch.setattr(rd.db_reader, "list_planets_db", lambda _email: [{"id": 1}])
    assert rd._canary_live_ready("ai06@bots.local", {108: 1}) is False


def test_canary_live_rejection_blocks_even_old_zero_defense_gate(monkeypatch):
    _patch_tick(monkeypatch, {
        COORD: _report(defense={}, ships={}, research={rd.ESPIONAGE_TECH: 3})
    })
    monkeypatch.setattr(rd, "_canary_live_ready", lambda _e, _lv: True)
    monkeypatch.setattr(rd.battle_sim, "is_tripped", lambda _e: False)
    decide_calls = []
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **kw: decide_calls.append(kw) or _decision(False),
    )
    attacks = []
    monkeypatch.setattr(rd.fleet, "attack",
                        lambda *a, **k: attacks.append((a, k)) or {"ok": True})

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 10, rd.SMALL_CARGO: 4, 108: 1},
        battle_mode=rd.BATTLE_SIM_CANARY_MODE,
    )

    assert attacks == []
    assert decide_calls and decide_calls[0]["mode"] == "live"
    assert note and "raid idle" in note


def test_canary_safety_trip_falls_back_to_zero_defense_gate(monkeypatch):
    _patch_tick(monkeypatch, {
        COORD: _report(defense={}, ships={}, research={rd.ESPIONAGE_TECH: 3})
    })
    monkeypatch.setattr(rd, "_canary_live_ready", lambda _e, _lv: True)
    monkeypatch.setattr(rd.battle_sim, "is_tripped", lambda _e: True)
    monkeypatch.setattr(rd.battle_sim, "decide", lambda **_kw: _decision(False))
    attacks, predictions = [], []
    monkeypatch.setattr(rd.fleet, "attack",
                        lambda _c, target, ships: attacks.append((target, ships)) or {"ok": True})
    monkeypatch.setattr(rd.battle_guard, "record_prediction",
                        lambda *a, **k: predictions.append((a, k)))

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.LIGHT_FIGHTER: 10, rd.SMALL_CARGO: 4, 108: 1},
        battle_mode=rd.BATTLE_SIM_CANARY_MODE,
    )

    assert attacks and attacks[0][0] == COORD
    assert predictions and predictions[0][1]["mode"] == "log"
    assert note and "SAFETY TRIPPED" in note and "방어0" in note


def test_account_wide_spy_cooldown_prevents_per_tick_target_rotation(monkeypatch):
    _patch_tick(monkeypatch, {COORD: _report(defense=None, ships=None, research=None)})
    monkeypatch.setattr(
        rd, "_load_state", lambda _email: {"targets": {}, "last_spy": 9900.0}
    )
    spies = []
    monkeypatch.setattr(rd.fleet, "spy",
                        lambda *a, **k: spies.append((a, k)) or {"ok": True})

    note = rd._raid_tick_inner(
        object(), "r@x", {rd.ESPIONAGE_PROBE: 12, rd.ESPIONAGE_TECH: 3}
    )

    assert spies == []
    assert note and "계정 정찰 쿨다운" in note


def test_reconcile_is_best_effort(monkeypatch):
    monkeypatch.setattr(rd.battle_guard, "reconcile",
                        lambda _email: (_ for _ in ()).throw(RuntimeError("db down")))
    assert rd.raid_tick(object(), "r@x", {}) is None
