from __future__ import annotations

import pytest

from bots.rule_based import (
    fleetsave,
    production_capacity,
    raider,
    strategic_growth as sg,
    turtle,
)
from bots.shared import db_reader


READY_LEVELS = {
    21: 12,
    31: 12,
    106: 20,
    108: 20,
    109: 20,
    110: 20,
    111: 20,
    113: 20,
    114: 20,
    115: 20,
    117: 20,
    118: 20,
    120: 20,
    121: 20,
}
RESOURCES = {"metal": 1_000_000, "crystal": 1_000_000, "deuterium": 1_000_000}


@pytest.fixture(autouse=True)
def _available_production_queue(monkeypatch):
    monkeypatch.setattr(
        sg.production_capacity,
        "production_queue_snapshot",
        lambda email: production_capacity.ProductionQueueSnapshot(
            available=True,
            email=email,
            planet_id=1,
            observed_at=0,
            economy_speed=25,
            building_queue_depth=0,
            building_queue_seconds=0,
            building_targets=(),
            unit_queue_depth=0,
            unit_queue_seconds=0,
            unit_remaining_amount=0,
            unit_items=(),
        ),
    )


def test_dynamic_fleet_investment_has_no_fixed_total_terminal_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)
    actions = sg.strategic_ship_candidates(
        "raider",
        READY_LEVELS,
        RESOURCES,
        {203: 100_000},  # 과거 어떤 고정 목표보다 훨씬 많아도 투자 기회가 있으면 계속 성장
        email="raider@test",
        strategy={"reinvestment_ratio": 0.5, "weights": {203: 1}},
    )

    # 민간 함대만 이미 과대할 때도 고정 총량에서 멈추지 않고, 새 공통 억지력
    # 축으로 재투자를 이어 간다. (화물 비중만 영구 100%로 고정하지 않는다.)
    assert actions and actions[0][1] in sg.COMBAT_SHIPS
    assert actions[0][2] > 0


def test_cargo_shortfall_biases_next_investment_to_large_cargo(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)
    sg.record_raid_opportunity(
        "cargo@test",
        potential_loot=2_000_000,
        realized_loot=500_000,
        target=(1, 20, 8),
        outcome="profitable-fleet",
    )

    actions = sg.strategic_ship_candidates(
        "raider",
        READY_LEVELS,
        RESOURCES,
        {},
        email="cargo@test",
        strategy={"reinvestment_ratio": 0.5, "weights": {202: 1, 203: 1}},
    )

    assert actions[0][1] == 203
    snapshot = sg.opportunity_snapshot("cargo@test")
    assert snapshot["cargo_shortfall"] == 1_500_000
    assert snapshot["cargo_saturation"] == 0.25


def test_surplus_pathfinders_redirect_expedition_investment_to_cargo(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)
    levels = {**READY_LEVELS, sg.ASTROPHYSICS: 4}

    actions = sg.strategic_ship_candidates(
        "explorer",
        levels,
        RESOURCES,
        {sg.PATHFINDER: 100},
        email="explorer@test",
        strategy={
            "reinvestment_ratio": 0.5,
            "doctrine": "expeditioneer",
            "weights": {sg.PATHFINDER: 45, 203: 15},
        },
    )

    assert actions and actions[0][1] == 203
    assert all(tid != sg.PATHFINDER for _, tid, _ in actions)
    assert sg.expedition_pathfinder_target(levels) == 5


def test_pathfinder_remains_candidate_until_all_slots_have_one():
    levels = {**READY_LEVELS, sg.ASTROPHYSICS: 4}

    actions = sg.strategic_ship_candidates(
        "explorer",
        levels,
        RESOURCES,
        {sg.PATHFINDER: 3},
        email="young-explorer@test",
        strategy={
            "reinvestment_ratio": 0.5,
            "doctrine": "expeditioneer",
            "weights": {sg.PATHFINDER: 1},
        },
    )

    assert actions and any(tid == sg.PATHFINDER for _, tid, _ in actions)


def test_expedition_mix_keeps_growing_cargo_and_combat_without_capacity_endpoint(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)
    levels = {**READY_LEVELS, sg.ASTROPHYSICS: 4}

    actions = sg.strategic_ship_candidates(
        "explorer",
        levels,
        RESOURCES,
        {sg.PATHFINDER: 100},
        email="capacity-explorer@test",
        strategy={
            "reinvestment_ratio": 0.5,
            "doctrine": "expeditioneer",
            "weights": {sg.PATHFINDER: 45, sg.LARGE_CARGO: 35},
        },
    )

    assert actions[0][1] == sg.LARGE_CARGO
    assert actions[0][2] > 3
    assert any(tid in sg.COMBAT_SHIPS for _, tid, _ in actions[1:])


def test_large_existing_expedition_fleet_still_builds_more_cargo(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)
    levels = {**READY_LEVELS, sg.ASTROPHYSICS: 16}

    actions = sg.strategic_ship_candidates(
        "explorer",
        levels,
        RESOURCES,
        {sg.LARGE_CARGO: 100_000, 215: 3_529, sg.PATHFINDER: 100},
        email="uncapped-explorer@test",
        strategy={
            "reinvestment_ratio": 0.5,
            "doctrine": "expeditioneer",
            "weights": {sg.LARGE_CARGO: 1},
        },
    )

    assert any(
        tid == sg.LARGE_CARGO and amount > 0 for _, tid, amount in actions
    )


def test_non_expedition_civilian_policy_still_grows_combat_deterrence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sg, "STATE_DIR", tmp_path)

    actions = sg.strategic_ship_candidates(
        "miner",
        READY_LEVELS,
        RESOURCES,
        {},
        email="civilian-miner@test",
        strategy={
            "reinvestment_ratio": 0.5,
            "doctrine": "balanced",
            "weights": {sg.LARGE_CARGO: 1},
        },
    )

    assert any(tid == sg.LARGE_CARGO for _, tid, _ in actions)
    assert any(tid in sg.COMBAT_SHIPS for _, tid, _ in actions)


def test_individual_doctrine_is_stable_and_supported():
    first = sg.individual_doctrine("bot042@bots.local", "raider")
    assert first == sg.individual_doctrine("bot042@bots.local", "raider")
    assert first in sg.DOCTRINES


def test_full_intel_debris_value_counts_military_ships_without_weakening_legacy_gate():
    military = {raider.BATTLESHIP: 10}
    assert raider.ship_debris_value(military) == 0  # 방어0 레거시 게이트는 계속 보수적
    assert raider.all_ship_debris_value(military) == 180_000


def test_adaptive_fleetsave_uses_current_or_recent_threat(monkeypatch):
    monkeypatch.setattr(fleetsave, "incoming_hostile_attacks", lambda _email: 0)
    monkeypatch.setattr(fleetsave, "recent_hostile_attack", lambda _email: False)
    assert fleetsave.adaptive_fleetsave_needed("bot@test") is False

    monkeypatch.setattr(fleetsave, "incoming_hostile_attacks", lambda _email: 1)
    assert fleetsave.adaptive_fleetsave_needed("bot@test") is True


def test_llm_defense_strategy_accounts_for_future_production(monkeypatch):
    monkeypatch.setattr(
        turtle, "read_resources",
        lambda _client: {"metal": 0, "crystal": 0, "deuterium": 0},
    )
    monkeypatch.setattr(turtle, "_surplus", lambda *_a, **_k: (1_000_000, 1_000_000, 1_000_000))
    monkeypatch.setattr(turtle, "_read_defense_counts", lambda _client: {401: 0})
    monkeypatch.setattr(
        db_reader, "get_production_per_hour_db",
        lambda _email: {"metal": 100_000, "crystal": 0, "deuterium": 0},
    )
    builds: list[tuple[int, int]] = []
    monkeypatch.setattr(
        turtle, "build_defense",
        lambda _client, tid, amount, **_kwargs: (
            builds.append((tid, amount)) or {"status": "success"}
        ),
    )

    note = turtle.defense_tick(
        object(), "ai@test", {21: 1}, energy=10, defense_ratio=1.0,
        strategy={
            "mode": "adaptive",
            "stored_loot_multiplier": 1.0,
            "production_hours": 1.0,
            "rebuild_on_attack": False,
        },
    )

    assert builds == [(401, turtle.FODDER_BATCH)]
    assert note and "방어~0/50000" in note
