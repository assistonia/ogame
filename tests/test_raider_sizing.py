"""상위 함종/대형화물 사이징 후보의 안전 상한과 EV 적재량 검증."""
from __future__ import annotations

from bots.rule_based import raider as rd


def _inventory() -> dict[int, int]:
    return {
        rd.LIGHT_FIGHTER: 10,
        rd.HEAVY_FIGHTER: 4,
        rd.CRUISER: 4,
        rd.BATTLESHIP: 2,
        rd.BATTLECRUISER: 2,
    }


def test_combat_budget_supports_capitals_and_caps_every_ship_class_at_65_percent():
    inventory = _inventory()
    max_budget = rd._sizing_max_combat_budget(
        inventory[rd.LIGHT_FIGHTER], inventory[rd.HEAVY_FIGHTER], inventory
    )
    fleet = rd._sizing_combat_split(
        max_budget,
        inventory[rd.LIGHT_FIGHTER],
        inventory[rd.HEAVY_FIGHTER],
        combat_inventory=inventory,
    )

    # 큰 budget을 넣어도 총 기체수가 아니라 함종별로 각각 65% 상한이 적용된다.
    for ship_id, have in inventory.items():
        assert fleet.get(ship_id, 0) <= int(have * rd.SIZING_MAX_COMBAT_FRACTION)
    assert fleet[rd.CRUISER] == 2
    assert fleet[rd.BATTLESHIP] == 1
    assert fleet[rd.BATTLECRUISER] == 1
    assert rd.battle_sim.fleet_cost_value(fleet) == max_budget


def test_single_rare_capital_cannot_be_overdeployed_by_total_count_rounding():
    inventory = {rd.LIGHT_FIGHTER: 100, rd.BATTLECRUISER: 1}
    fleet = rd._sizing_combat_split(
        10**9, 100, 0, combat_inventory=inventory,
    )

    assert fleet[rd.LIGHT_FIGHTER] == 65
    assert rd.BATTLECRUISER not in fleet  # floor(1 * 0.65) == 0


def test_capital_only_inventory_is_a_valid_candidate_once_reserve_is_possible():
    inventory = {rd.CRUISER: 2}
    budget = rd._sizing_max_combat_budget(0, 0, inventory)
    fleet = rd._sizing_fleet(
        budget, 10_000, 0, 0, 2,
        combat_inventory=inventory,
    )

    assert fleet[rd.CRUISER] == 1
    assert fleet[rd.SMALL_CARGO] == 2


def test_cargo_split_uses_large_cargo_only_when_it_reduces_exposed_value():
    # 10k는 SC 2척(8k 가치)이 LC 1척(12k)보다 싸다.
    assert rd._sizing_cargo_split(10_000, have_sc=10, have_lc=2) == {
        rd.SMALL_CARGO: 2,
    }
    # 30k는 LC 1 + SC 1(16k)이 SC 6/LC 2(각 24k)보다 싸다.
    assert rd._sizing_cargo_split(30_000, have_sc=10, have_lc=2) == {
        rd.SMALL_CARGO: 1,
        rd.LARGE_CARGO: 1,
    }


def test_shadow_ev_uses_realizable_loot_when_cargo_capacity_is_short(monkeypatch):
    ev_loot: list[int] = []
    simulated_fleets: list[dict[int, int]] = []
    monkeypatch.setattr(rd.battle_sim, "flight_distance", lambda *_args: 1000)
    monkeypatch.setattr(rd.battle_sim, "fuel_estimate", lambda *_args: 0.0)
    monkeypatch.setattr(
        rd.battle_sim,
        "simulate",
        lambda fleet, *_args, **_kwargs: (
            simulated_fleets.append(dict(fleet))
            or {"win_prob": 0.9, "attacker_loss_value": 0, "debris": {}}
        ),
    )
    monkeypatch.setattr(
        rd.battle_sim,
        "attack_ev",
        lambda loot, _sim, _has_rec, fuel_cost=0.0: ev_loot.append(int(loot)) or loot,
    )

    note = rd._sizing_shadow_log(
        (1, 2, 3), {}, {}, 100_000,
        have_lf=0, have_hf=0, have_sc=0, have_rec=0,
        levels={rd.CRUISER: 2, rd.LARGE_CARGO: 1}, own=(1, 2, 4),
    )

    assert note and "CR1" in note
    assert simulated_fleets
    assert all(fleet.get(rd.CRUISER) == 1 for fleet in simulated_fleets)
    assert set(ev_loot) == {rd.LARGE_CARGO_CAPACITY}
