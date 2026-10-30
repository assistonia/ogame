"""광산 ROI가 공용 성장 플래너의 안전 우선순위를 침범하지 않는지 검증한다."""
from __future__ import annotations

from bots.ai_agents import policy_engine
from bots.rule_based import investment, personality, storage


LEVELS = {
    1: 8,
    2: 6,
    3: 3,
    4: 6,
    14: 0,
    31: 0,
    21: 0,
}
RESOURCE_STATE = {
    "metal": 50_000,
    "crystal": 50_000,
    "deuterium": 50_000,
    "energy": 100_000,
    "metal_max": 1_000_000,
    "crystal_max": 1_000_000,
    "deuterium_max": 1_000_000,
    "temp_max": 40,
}


def _mine_ids(candidates: list[tuple]) -> list[int]:
    return [
        action[1]
        for action in candidates
        if action[0] == "build" and action[1] in personality.MINE_BUILDINGS
    ]


def _non_mine_slots(candidates: list[tuple]) -> list[tuple[int, tuple]]:
    return [
        (index, action)
        for index, action in enumerate(candidates)
        if not (action[0] == "build" and action[1] in personality.MINE_BUILDINGS)
    ]


def test_common_primary_base_orders_only_mine_slots_by_roi():
    legacy = personality._plan_primary_base("miner", LEVELS, tick_index=0)
    planned = personality._plan_primary_base(
        "miner",
        LEVELS,
        tick_index=0,
        energy=100_000,
        resource_state=RESOURCE_STATE,
    )

    expected = [
        technology_id
        for technology_id, _score in investment.mine_roi_order(
            LEVELS,
            temp_max=RESOURCE_STATE["temp_max"],
            energy_surplus=100_000,
        )
        if technology_id in _mine_ids(legacy)
    ]
    assert _mine_ids(planned) == expected
    # 시설과 태양광은 원래 슬롯 그대로다. ROI가 움직이는 것은 광산 세 종류뿐이다.
    assert _non_mine_slots(planned) == _non_mine_slots(legacy)


def test_roi_does_not_promote_a_mine_past_resource_floor_group():
    levels = dict(LEVELS)
    levels.update({1: 0, 2: 10, 3: 10, 4: 10})
    planned = personality._plan_primary_base(
        "miner",
        levels,
        tick_index=4,
        energy=100_000,
        resource_state=RESOURCE_STATE,
    )
    assert planned[0] == ("build", 1)


def test_missing_or_invalid_roi_inputs_keep_legacy_order():
    legacy = personality._plan_primary_base("miner", LEVELS, tick_index=0)

    assert personality._plan_primary_base(
        "miner", LEVELS, 0, energy=None, resource_state=RESOURCE_STATE
    ) == legacy
    assert personality._plan_primary_base(
        "miner", LEVELS, 0, energy=100_000, resource_state=None
    ) == legacy
    assert personality._plan_primary_base(
        "miner",
        LEVELS,
        0,
        energy=100_000,
        resource_state={**RESOURCE_STATE, "temp_max": "broken"},
    ) == legacy


def test_energy_and_storage_still_precede_roi_mines(monkeypatch):
    emergency = personality.plan_primary(
        "miner",
        LEVELS,
        tick_index=0,
        energy=-1,
        resource_state=RESOURCE_STATE,
        state_key=None,
        allow_satellite=False,
    )
    assert emergency[0] == ("build", 4)

    monkeypatch.setattr(storage, "storage_ids_needed", lambda *args, **kwargs: [22])
    with_storage = personality.plan_primary(
        "miner",
        LEVELS,
        tick_index=0,
        energy=100_000,
        resource_state=RESOURCE_STATE,
        state_key=None,
    )
    assert with_storage[0] == ("build", 22)


def test_ai_policy_cannot_reverse_shared_mine_roi_order():
    shared = personality._plan_primary_base(
        "miner",
        LEVELS,
        tick_index=0,
        energy=100_000,
        resource_state=RESOURCE_STATE,
    )
    shared_mines = _mine_ids(shared)
    assert len(shared_mines) == 3

    policy_order = list(reversed(shared_mines))
    assert policy_order != shared_mines
    ai_planned = policy_engine.apply_build_priority(
        shared, policy_order, energy=100_000
    )
    assert _mine_ids(ai_planned) == shared_mines

