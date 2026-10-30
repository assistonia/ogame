"""탐험가 원정 루틴의 순수 로직 단위검증 (HTTP/DB 없이).

expedition_tick 자체는 함대를 파견하므로(부작용) 여기서 실행하지 않고, 함대 구성·슬롯
계산·최소함대 가드 같은 결정 로직만 고정 입력으로 검증한다. 실제 파견 검증은 드라이런
(explorer 1명 1회 파견 → fleet_missions type-15 생성 확인)으로 별도 수행했다.

실행: `python3 -m pytest tests/test_expedition.py` 또는 `python3 tests/test_expedition.py`.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import explorer as ex

LARGE_CARGO, SMALL_CARGO, LIGHT_FIGHTER, HEAVY_FIGHTER, PATHFINDER, ASTRO = (
    203, 202, 204, 205, 219, 124
)


def test_fleet_uses_pathfinder_when_available():
    """PF 보너스는 한 척으로 고정하고 나머지 예산은 화물에 쓴다."""
    lv = {LARGE_CARGO: 10, SMALL_CARGO: 3, LIGHT_FIGHTER: 20, PATHFINDER: 3, ASTRO: 2}
    fleet = ex.build_expedition_fleet(lv)
    assert fleet[PATHFINDER] == 1, "파스파인더는 정예 1기여야"
    assert fleet[LARGE_CARGO] > ex.EXP_TEMPLATE[LARGE_CARGO]
    assert LIGHT_FIGHTER not in fleet, "PF가 있는데 전투함을 추가하지 않아야"


def test_fleet_fallback_without_pathfinder():
    """PF를 건조할 수 없는 클래스도 화물+최대 한 척 호위로 폴백한다."""
    lv = {LARGE_CARGO: 10, SMALL_CARGO: 3, LIGHT_FIGHTER: 20, PATHFINDER: 0, ASTRO: 2}
    fleet = ex.build_expedition_fleet(lv)
    assert PATHFINDER not in fleet
    assert fleet.get(LARGE_CARGO, 0) >= 1 and fleet.get(LIGHT_FIGHTER, 0) >= 1
    assert sum(fleet.get(t, 0) for t in ex.COMBAT_TIER_ORDER) == 1


def test_fleet_without_cargo_is_not_dispatched_even_with_escort():
    """호위만 있고 적재선이 없으면 원정 편성으로 인정하지 않는다."""
    lv = {HEAVY_FIGHTER: 10, ASTRO: 2}
    fleet = ex.build_expedition_fleet(lv)
    assert not ex.expedition_fleet_ready(fleet)


def test_fleet_below_min_is_empty_enough_to_skip():
    """함대가 빈약하면(총 함선 < MIN_EXP_SHIPS) 원정을 안 보내도록 작은 함대가 나온다."""
    lv = {LARGE_CARGO: 1, LIGHT_FIGHTER: 2, ASTRO: 1}
    fleet = ex.build_expedition_fleet(lv)
    assert sum(fleet.values()) < ex.MIN_EXP_SHIPS


def test_full_autonomy_can_use_server_valid_single_cargo_fallback():
    lv = {LARGE_CARGO: 1, ASTRO: 1}

    ships = ex.build_expedition_fleet(
        lv, aggressive=True, risk_fraction=1.0, minimum_ships=1
    )

    assert ships == {LARGE_CARGO: 1}
    assert ex.expedition_fleet_ready(ships, minimum_ships=1)
    assert not ex.expedition_fleet_ready(ships)  # 규칙봇 안전 기본값은 유지


def test_expedition_slots_formula():
    """원정 슬롯 = floor(sqrt(astro)) + Discoverer(2). 서버 공식 복제."""
    assert ex.expedition_slots({ASTRO: 0}) == 2   # floor(sqrt0)=0 +2
    assert ex.expedition_slots({ASTRO: 2}) == 3   # floor(sqrt2)=1 +2
    assert ex.expedition_slots({ASTRO: 9}) == 5   # floor(sqrt9)=3 +2


def test_aggressive_fleet_scales_and_preserves_other_slots():
    lv = {LARGE_CARGO: 100, SMALL_CARGO: 40, LIGHT_FIGHTER: 30,
          HEAVY_FIGHTER: 8, PATHFINDER: 8, ASTRO: 9}
    ships = ex.build_expedition_fleet(lv, aggressive=True, slots_to_preserve=4)
    assert ships[LARGE_CARGO] > ex.EXP_TEMPLATE[LARGE_CARGO]
    assert ships[PATHFINDER] == 1
    assert sum(ships.get(t, 0) for t in ex.COMBAT_TIER_ORDER) == 0
    assert ex.expedition_fleet_value(ships) <= ex.expedition_fleet_value(lv) * ex.AGGRESSIVE_FLEET_VALUE_FRACTION


def test_aggressive_fleet_uses_highest_available_combat_tier():
    lv = {LARGE_CARGO: 30, LIGHT_FIGHTER: 20, HEAVY_FIGHTER: 4, 206: 3,
          ASTRO: 4}
    ships = ex.build_expedition_fleet(lv, aggressive=True, slots_to_preserve=2)
    assert ships.get(206) == 1
    assert ships.get(HEAVY_FIGHTER, 0) == 0
    assert ships.get(LIGHT_FIGHTER, 0) == 0


def test_aggressive_evenly_divides_pathfinders_and_cargo_across_slots():
    lv = {LARGE_CARGO: 20, LIGHT_FIGHTER: 8, PATHFINDER: 5, ASTRO: 9}
    ships = ex.build_expedition_fleet(lv, aggressive=True, slots_to_preserve=4,
                                      risk_fraction=1.0, value_budget=10**9)
    assert ships == {PATHFINDER: 1, LARGE_CARGO: 4}


def test_aggressive_obeys_remaining_total_exposure_not_per_launch_only():
    lv = {LARGE_CARGO: 100, LIGHT_FIGHTER: 20, PATHFINDER: 10, ASTRO: 9}
    ships = ex.build_expedition_fleet(lv, aggressive=True, value_budget=100_000)
    assert ex.expedition_fleet_value(ships) <= 100_000


def test_cargo_has_no_artificial_capacity_ceiling_and_identity_is_exact():
    lv = {LARGE_CARGO: 100_000, SMALL_CARGO: 100_000,
          LIGHT_FIGHTER: 100, PATHFINDER: 100, ASTRO: 9}
    ships = ex.build_expedition_fleet(
        lv, aggressive=True, risk_fraction=1.0, value_budget=10**12
    )
    assert ships[PATHFINDER] == 1
    assert sum(ships.get(t, 0) for t in ex.COMBAT_TIER_ORDER) == 0
    assert ships[LARGE_CARGO] == lv[LARGE_CARGO]
    assert ships[SMALL_CARGO] == lv[SMALL_CARGO]
    assert ex.expedition_cargo_capacity(ships) > 25_000_000


def test_rule_fleet_is_adaptive_instead_of_fixed_three_large_cargos():
    lv = {LARGE_CARGO: 100, LIGHT_FIGHTER: 20, PATHFINDER: 4, ASTRO: 4}
    ships = ex.build_expedition_fleet(
        lv, aggressive=False, slots_to_preserve=3, risk_fraction=1.0
    )
    assert ships[PATHFINDER] == 1
    assert ships[LARGE_CARGO] == 25
    assert ships[LARGE_CARGO] > ex.EXP_TEMPLATE[LARGE_CARGO]


def test_remaining_exposure_budget_is_divided_by_remaining_slots():
    lv = {LARGE_CARGO: 100, SMALL_CARGO: 100, PATHFINDER: 4, ASTRO: 4}
    ships = ex.build_expedition_fleet(
        lv, slots_to_preserve=3, risk_fraction=1.0, value_budget=400_000
    )
    assert ex.expedition_fleet_value(ships) <= 100_000
    assert ships[PATHFINDER] == 1


def test_sequential_launches_split_dispatchable_fleet_evenly():
    levels = {LARGE_CARGO: 100, LIGHT_FIGHTER: 20, PATHFINDER: 5, ASTRO: 9}
    remaining = 5
    values = []
    while remaining:
        ships = ex.build_expedition_fleet(
            levels,
            slots_to_preserve=remaining - 1,
            risk_fraction=1.0,
            value_budget=10**9,
        )
        values.append(ex.expedition_fleet_value(ships))
        assert ships[PATHFINDER] == 1
        assert ships[LARGE_CARGO] == 20
        for tid, amount in ships.items():
            levels[tid] -= amount
        remaining -= 1
    assert len(set(values)) == 1
    assert levels[LARGE_CARGO] == 0 and levels[PATHFINDER] == 0


def test_pathfinderless_cargo_only_fallback_is_valid():
    ships = ex.build_expedition_fleet(
        {LARGE_CARGO: 12, ASTRO: 4},
        slots_to_preserve=2,
        risk_fraction=1.0,
    )
    assert ships == {LARGE_CARGO: 4}
    assert ex.expedition_fleet_ready(ships)


def test_slot_guard_counts_general_fleet_slots_too(monkeypatch):
    monkeypatch.setattr(ex, "_mission_state", lambda email: (0, 3, 0))
    monkeypatch.setattr(ex, "_discoverer_slot_bonus", lambda email: 2)
    result = ex._expedition_tick_inner(object(), "explorer01@bots.local",
        {ASTRO: 9, 108: 2, LARGE_CARGO: 20, LIGHT_FIGHTER: 8, PATHFINDER: 5},
        aggressive=True)
    assert "fleet slots" in result


def test_db_state_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(ex, "_mission_state", lambda email: None)
    result = ex._expedition_tick_inner(object(), "explorer01@bots.local",
        {ASTRO: 9, 108: 9, LARGE_CARGO: 20, LIGHT_FIGHTER: 8, PATHFINDER: 5},
        aggressive=True)
    assert "DB state unavailable" in result


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
