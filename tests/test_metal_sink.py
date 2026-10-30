"""금속 과잉 sink 순수 의사결정 검증(DB/HTTP 부작용 없음)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import metal_sink as ms


READY_LEVELS = {
    21: 4,    # shipyard
    115: 6,   # combustion drive
    ms.ROCKET_LAUNCHER: 0,
}
BASE_RESERVE = {"metal": 0, "crystal": 0, "deuterium": 0}
METAL_HEAVY = {
    "metal": 225_000,
    "crystal": 55_000,
    "deuterium": 20_000,
    "metal_max": 500_000,
}


def _plan(**overrides):
    args = {
        "levels": dict(READY_LEVELS),
        "resource_state": dict(METAL_HEAVY),
        "reserve": dict(BASE_RESERVE),
        "personality_key": "miner",
        "energy": 10,
        "on_homeworld": True,
        "colony_action_pending": False,
        "building_queue_depth": 2,
        "research_queue_depth": 1,
        "unit_queue_depth": 0,
        "fleet_slots_free": 1,
        "safe_fleetsave_target": True,
        "parked_savable_value": 40_000,
        "fleetsave_min_value": 40_000,
        "total_large_cargo": 0,
        "planet_count": 2,
    }
    args.update(overrides)
    return ms.plan_metal_sink(**args)


def test_ids_and_costs_match_existing_canonical_objects():
    assert ms.LARGE_CARGO == 203
    assert ms.ROCKET_LAUNCHER == 401
    assert ms.UNIT_COST == {
        203: (6_000, 6_000, 0),
        401: (2_000, 0, 0),
    }
    assert ms.LARGE_CARGO_VALUE == 12_000


def test_minimum_rocket_screen_is_first_and_bounded_to_five():
    decision = _plan()
    assert decision.action == ("defense", 401, 5)
    assert decision.reason == "minimum-defense"
    assert decision.spend == {"metal": 10_000, "crystal": 0, "deuterium": 0}


def test_minimum_defense_respects_existing_count_and_surplus_fraction():
    # 안전 잉여 metal=20,000, 틱 예산은 25%=5,000 → 2기만 가능.
    resources = {"metal": 45_000, "crystal": 15_000, "deuterium": 10_000}
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 4}
    decision = _plan(levels=levels, resource_state=resources)
    assert decision.action == ("defense", 401, 1), "목표 5기까지 남은 1기만 지어야 함"
    assert decision.spend_metal <= int(decision.metal_surplus * 0.25)


def test_large_cargo_after_minimum_defense_uses_only_crystal_surplus_fraction():
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 5}
    decision = _plan(levels=levels)
    # crystal 안전 잉여 40k 중 25%=10k만 개방 → 6000C 화물선 1기.
    assert decision.action == ("ship", 203, 1)
    assert decision.reason == "transport-capacity"
    assert decision.spend == {"metal": 6_000, "crystal": 6_000, "deuterium": 0}
    assert decision.spend_crystal <= int(decision.crystal_surplus * 0.25)


def test_cargo_batch_must_cross_fleetsave_threshold_immediately():
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 5}
    # 보호 reserve 이후 M/C 잉여 300k/96k. 25% C 예산=24k → 4기를 살 수 있고,
    # parked=0에서 miner 40k 임계를 넘으려면 ceil(40/12)=4기 필요.
    resources = {"metal": 325_000, "crystal": 111_000, "deuterium": 20_000}
    decision = _plan(
        levels=levels,
        resource_state=resources,
        parked_savable_value=0,
        fleetsave_min_value=40_000,
    )
    assert decision.action == ("ship", 203, 4)


def test_cargo_is_rejected_if_affordable_batch_would_remain_below_fleetsave_threshold():
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 5}
    # 기본 자원은 1기만 허용하지만 parked=0이면 4기가 필요 → cargo 대신 제한 방어.
    decision = _plan(
        levels=levels,
        parked_savable_value=0,
        fleetsave_min_value=40_000,
    )
    assert decision.action == ("defense", 401, 5)
    assert decision.reason == "bounded-metal-defense"


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"safe_fleetsave_target": False}, "bounded-metal-defense"),
        ({"fleet_slots_free": 0}, "bounded-metal-defense"),
        ({"fleet_slots_free": None}, "bounded-metal-defense"),
        ({"parked_savable_value": None}, "bounded-metal-defense"),
        ({"fleetsave_min_value": None}, "bounded-metal-defense"),
        ({"total_large_cargo": None}, "bounded-metal-defense"),
    ],
)
def test_unknown_or_unsafe_fleetsave_state_never_builds_cargo(override, expected_reason):
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 5}
    decision = _plan(levels=levels, **override)
    assert decision.kind == "defense"
    assert decision.reason == expected_reason


def test_large_cargo_requires_real_server_prerequisites():
    levels = {21: 4, 115: 5, ms.ROCKET_LAUNCHER: 5}  # combustion 6 미달
    decision = _plan(levels=levels)
    assert decision.action == ("defense", 401, 5)
    assert decision.reason == "bounded-metal-defense"


def test_total_cargo_includes_in_flight_and_prevents_overproduction():
    # explorer는 이 모듈의 방어 분기를 쓰지 않는다. 행성 levels에 화물선이 0이어도
    # 호출부 총량(비행/큐 포함)이 목표 8이면 추가 화물선을 만들면 안 된다.
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 0, ms.LARGE_CARGO: 0}
    decision = _plan(
        levels=levels,
        personality_key="explorer",
        total_large_cargo=8,
    )
    assert decision.action is None
    assert decision.reason == "no-safe-sink-candidate"


def test_cargo_target_scales_with_planets_but_caps_at_twelve():
    assert ms._large_cargo_target(1) == 8
    assert ms._large_cargo_target(2) == 8
    assert ms._large_cargo_target(3) == 12
    assert ms._large_cargo_target(99) == 12


def test_turtle_is_fully_owned_by_existing_defense_logic():
    decision = _plan(personality_key="turtle")
    assert decision.action is None
    assert decision.reason == "turtle-owns-defense-sink"


def test_unknown_personality_fails_closed():
    decision = _plan(personality_key="future-personality")
    assert decision.action is None
    assert decision.reason == "personality-unknown"


def test_explorer_never_uses_duplicate_defense_branch():
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: 0}
    decision = _plan(
        levels=levels,
        personality_key="explorer",
        safe_fleetsave_target=False,
    )
    assert decision.action is None
    assert decision.reason == "no-safe-sink-candidate"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"energy": None}, "energy-unknown"),
        ({"energy": "10"}, "energy-unknown"),
        ({"energy": -1}, "energy-deficit"),
        ({"on_homeworld": False}, "colony-scope-disabled"),
        ({"colony_action_pending": True}, "colony-action-reserved"),
    ],
)
def test_energy_and_colony_guards_fail_closed(override, reason):
    decision = _plan(**override)
    assert decision.action is None
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"unit_queue_depth": 1}, "unit-queue-busy"),
        ({"building_queue_depth": 4}, "building-queue-pressure"),
        ({"research_queue_depth": 3}, "research-queue-pressure"),
        ({"unit_queue_depth": None}, "queue-state-unknown"),
        ({"building_queue_depth": -1}, "queue-state-unknown"),
    ],
)
def test_all_queue_guards_are_conservative(override, reason):
    decision = _plan(**override)
    assert decision.action is None
    assert decision.reason == reason


def test_reserve_multiplier_and_absolute_floor_are_never_spent():
    reserve = {"metal": 100_000, "crystal": 20_000, "deuterium": 8_000}
    resources = {"metal": 325_000, "crystal": 55_000, "deuterium": 20_000}
    decision = _plan(resource_state=resources, reserve=reserve)

    protected = ms._safe_reserve(reserve)
    assert protected == {"metal": 125_000, "crystal": 25_000, "deuterium": 10_000}
    assert resources["metal"] - decision.spend_metal >= protected["metal"]
    assert resources["crystal"] - decision.spend_crystal >= protected["crystal"]
    assert resources["deuterium"] - decision.spend_deuterium >= protected["deuterium"]


def test_normal_resource_ratio_does_not_trigger_sink():
    resources = {"metal": 75_000, "crystal": 45_000, "deuterium": 20_000, "metal_max": 100_000}
    decision = _plan(resource_state=resources)
    assert decision.action is None
    assert decision.reason == "metal-not-overabundant"


def test_storage_pressure_can_trigger_even_at_non_extreme_ratio():
    # 안전 잉여 65k/30k → 2.17:1로 비율 트리거(2.5)는 미달, 창고 90%가 트리거.
    resources = {"metal": 90_000, "crystal": 45_000, "deuterium": 20_000, "metal_max": 100_000}
    decision = _plan(resource_state=resources)
    assert decision.action == ("defense", 401, 5)


def test_defense_stops_at_hard_cap_when_cargo_is_unsafe():
    levels = {**READY_LEVELS, ms.ROCKET_LAUNCHER: ms.MAX_ROCKET_TARGET}
    decision = _plan(levels=levels, safe_fleetsave_target=False)
    assert decision.action is None
    assert decision.reason == "no-safe-sink-candidate"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"reserve": None}, "reserve-unknown"),
        ({"reserve": {"metal": 0, "crystal": 0}}, "reserve-unknown"),
        ({"reserve": {"metal": -1, "crystal": 0, "deuterium": 0}}, "reserve-unknown"),
        ({"resource_state": {"metal": 100_000, "crystal": 10_000}}, "resource-state-invalid"),
        ({"resource_state": {"metal": -1, "crystal": 10_000, "deuterium": 10_000}}, "resource-state-invalid"),
    ],
)
def test_missing_or_invalid_resource_commitments_fail_closed(override, reason):
    decision = _plan(**override)
    assert decision.action is None
    assert decision.reason == reason


def test_malformed_levels_or_planet_count_close_only_unsafe_cargo_branch():
    # 최소 방어는 이미 채운 상태. levels의 선행조건 값이나 행성 수가 비정상이면
    # 대형화물은 금지한다. 선행조건 자체를 모르면 방어도 닫고, 행성 수만 잘못된 경우에는
    # 선행조건이 알려진 고정 방어 상한 안에서만 fallback한다.
    malformed = {21: "four", 115: 6, ms.ROCKET_LAUNCHER: 5}
    malformed_decision = _plan(levels=malformed)
    assert malformed_decision.action is None
    assert malformed_decision.reason == "no-safe-sink-candidate"

    planet_decision = _plan(planet_count=0, levels={**READY_LEVELS, ms.ROCKET_LAUNCHER: 5})
    assert planet_decision.action == ("defense", 401, 5)
    assert planet_decision.reason == "bounded-metal-defense"


def test_decision_is_immutable_and_spend_property_does_not_expose_state():
    decision = _plan()
    with pytest.raises(FrozenInstanceError):
        decision.amount = 99
    external = decision.spend
    external["metal"] = 0
    assert decision.spend_metal == 10_000


def test_format_decision_is_stable_for_log_only_integration():
    action = ms.format_decision(_plan())
    assert action.startswith("metal-sink PLAN defense rocket_launcher(401)x5")
    assert "spend=10000/0/0" in action

    hold = ms.format_decision(_plan(energy=-1))
    assert hold == "metal-sink HOLD energy-deficit surplus=0/0/0"
