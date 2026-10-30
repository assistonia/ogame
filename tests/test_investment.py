"""투자 계산기의 공식·단위·고배속 틱 보정을 검증한다."""
from __future__ import annotations

import math

import pytest

from bots.rule_based import investment as inv
from bots.shared import game_objects as go


M = go.METAL_MINE
C = go.CRYSTAL_MINE
D = go.DEUTERIUM_SYNTHESIZER
N = 15


def _manual_build_time(
    technology_id: int,
    target_level: int,
    robot_lv: int,
    nanite_lv: int,
    speed: int,
) -> int:
    cost = go.object_cost(technology_id, target_level)
    assert cost is not None
    denominator = 2500 * (1 + robot_lv) * speed * (2**nanite_lv)
    if technology_id != N:
        denominator *= max(4 - target_level / 2, 1)
    return max(int(((cost["metal"] + cost["crystal"]) / denominator) * 3600), 1)


def test_build_time_matches_server_for_regular_and_nanite_buildings():
    assert inv.build_time_seconds(M, 17, 4, 0, 100) == _manual_build_time(
        M, 17, 4, 0, 100
    )
    assert inv.build_time_seconds(N, 3, 10, 2, 50) == _manual_build_time(
        N, 3, 10, 2, 50
    )


def test_build_time_rejects_wrong_object_type_and_invalid_levels():
    with pytest.raises(ValueError, match="건물/시설이 아닌"):
        inv.build_time_seconds(go.ENERGY_TECHNOLOGY, 2, 1, 0)
    with pytest.raises(ValueError, match="robot_lv"):
        inv.build_time_seconds(M, 2, -1, 0)
    with pytest.raises(ValueError, match="target_level"):
        inv.build_time_seconds(M, 0, 1, 0)


def test_zero_economy_speed_uses_server_sanity_fallback_one():
    assert inv.build_time_seconds(M, 10, 2, 0, 0) == inv.build_time_seconds(
        M, 10, 2, 0, 1
    )


def test_research_time_includes_economy_research_and_discoverer_multipliers():
    target = 11
    lab = 9
    economy = 50
    research_speed = 50
    cost = go.object_cost(go.ENERGY_TECHNOLOGY, target)
    assert cost is not None
    base = int(
        (
            (cost["metal"] + cost["crystal"])
            / (1000 * (1 + lab) * economy * research_speed)
        )
        * 3600
    )
    expected = max(int(base * 0.75), 1)
    assert (
        inv.research_time_seconds(
            go.ENERGY_TECHNOLOGY,
            target,
            lab,
            economy,
            research_speed,
            0.75,
        )
        == expected
    )


def test_research_time_rejects_building_id():
    with pytest.raises(ValueError, match="연구가 아닌"):
        inv.research_time_seconds(M, 2, 1)


def _midgame_levels() -> dict[int, int]:
    return {M: 17, C: 14, D: 13, go.ROBOT_FACTORY: 4, N: 0}


def test_robot_payback_is_none_when_x100_savings_are_hidden_by_four_minute_tick():
    # 원시 건설시간은 35+25+28초 줄지만, 세 건 모두 전/후 240초 안에 끝난다.
    plans = [(M, 18), (C, 15), (D, 14)]
    assert (
        inv.robot_payback_hours(
            _midgame_levels(),
            plans,
            universe_speed=100,
            temp_max=40,
            available_resources={
                "metal": 10**9,
                "crystal": 10**9,
                "deuterium": 10**9,
            },
        )
        is None
    )


def test_robot_payback_has_hour_units_and_exposes_components():
    levels = _midgame_levels()
    plans = [(M, 18), (M, 19), (M, 20), (M, 21), (M, 22)]
    rich = {"metal": 10**9, "crystal": 10**9, "deuterium": 10**9}
    result = inv.robot_payback_analysis(
        levels,
        plans,
        universe_speed=100,
        temp_max=40,
        available_resources=rich,
    )
    assert result is not None
    assert result.plan_before_seconds > result.plan_after_seconds
    assert (
        result.saved_seconds == result.plan_before_seconds - result.plan_after_seconds
    )
    assert result.effective_robot_build_seconds == 240
    expected_hours = (
        result.effective_robot_build_seconds
        / (result.saved_seconds / result.plan_before_seconds)
        / 3600
    )
    assert result.payback_hours == pytest.approx(expected_hours)
    assert inv.robot_payback_hours(
        levels,
        plans,
        universe_speed=100,
        temp_max=40,
        available_resources=rich,
    ) == pytest.approx(result.payback_hours)


def test_robot_resource_shortfall_increases_payback_and_uses_slowest_resource():
    levels = _midgame_levels()
    plans = [(M, 18), (M, 19), (M, 20), (M, 21), (M, 22)]
    rich = {"metal": 10**9, "crystal": 10**9, "deuterium": 10**9}
    funded = inv.robot_payback_analysis(
        levels, plans, temp_max=40, available_resources=rich
    )
    unfunded = inv.robot_payback_analysis(
        levels,
        plans,
        temp_max=40,
        available_resources={"metal": 0, "crystal": 0, "deuterium": 0},
    )
    assert funded is not None and unfunded is not None
    assert funded.resource_wait_seconds == 0
    assert unfunded.resource_wait_seconds > 0
    assert unfunded.payback_hours > funded.payback_hours


def test_robot_plan_must_be_sequential_and_must_not_include_robot_factory():
    with pytest.raises(ValueError, match="목표 레벨은 18"):
        inv.robot_payback_hours(_midgame_levels(), [(M, 19)], temp_max=40)
    with pytest.raises(ValueError, match="중복 포함"):
        inv.robot_payback_hours(_midgame_levels(), [(go.ROBOT_FACTORY, 5)], temp_max=40)


def _manual_marginal_roi(
    technology_id: int,
    level: int,
    *,
    speed: int,
    temp_max: int,
    plasma_level: int = 0,
) -> float:
    base = {M: 30.0, C: 20.0, D: 10.0}[technology_id]
    temp_factor = 1.0 if technology_id != D else 1.44 - 0.004 * (temp_max - 20)
    before_raw = base * level * 1.1**level * speed * temp_factor
    after_raw = base * (level + 1) * 1.1 ** (level + 1) * speed * temp_factor
    resource = {M: "metal", C: "crystal", D: "deuterium"}[technology_id]
    plasma_rate = {"metal": 0.01, "crystal": 0.0066, "deuterium": 0.0033}[resource]
    before = math.ceil(before_raw + math.floor(before_raw * plasma_rate * plasma_level))
    after = math.ceil(after_raw + math.floor(after_raw * plasma_rate * plasma_level))
    value = {M: 1.0, C: 1.5, D: 3.0}[technology_id]
    cost = go.object_cost(technology_id, level + 1)
    assert cost is not None
    # 비용도 생산과 같은 메탈 환산 단위: 나누기가 아니라 곱하기다.
    cost_value = cost["metal"] + 1.5 * cost["crystal"] + 3.0 * cost["deuterium"]
    return ((after - before) * value) / cost_value


def test_mine_roi_uses_same_resource_value_units_for_output_and_cost():
    levels = {M: 17, C: 14, D: 13, 122: 0}
    order = inv.mine_roi_order(levels, temp_max=100, universe_speed=100)
    scores = dict(order)
    for technology_id in (M, C, D):
        assert scores[technology_id] == pytest.approx(
            _manual_marginal_roi(
                technology_id,
                levels[technology_id],
                speed=100,
                temp_max=100,
            )
        )
    assert order == sorted(order, key=lambda item: (-item[1], item[0]))
    assert all(math.isfinite(score) and score > 0 for _, score in order)


def test_mine_roi_scales_with_speed_and_deuterium_temperature():
    levels = {M: 17, C: 14, D: 13}
    speed_50 = dict(inv.mine_roi_order(levels, temp_max=50, universe_speed=50))
    speed_100 = dict(inv.mine_roi_order(levels, temp_max=50, universe_speed=100))
    assert speed_100[M] == pytest.approx(speed_50[M] * 2)

    cold = dict(inv.mine_roi_order(levels, temp_max=-50, universe_speed=100))
    hot = dict(inv.mine_roi_order(levels, temp_max=100, universe_speed=100))
    assert cold[D] > hot[D]
    assert cold[M] == pytest.approx(hot[M])


def test_mine_roi_reproduces_resource_specific_plasma_flooring():
    levels = {M: 17, C: 14, D: 13, 122: 7}
    scores = dict(inv.mine_roi_order(levels, temp_max=30, universe_speed=50))
    for technology_id in (M, C, D):
        assert scores[technology_id] == pytest.approx(
            _manual_marginal_roi(
                technology_id,
                levels[technology_id],
                speed=50,
                temp_max=30,
                plasma_level=7,
            )
        )


def test_mine_roi_requires_temperature_and_respects_energy_guards():
    levels = {M: 17, C: 14, D: 13}
    with pytest.raises(ValueError, match="temp_max"):
        inv.mine_roi_order(levels)
    assert inv.mine_roi_order(levels, energy_ok=False) == []

    # 여유 에너지 0이면 세 광산 모두 다음 레벨 추가소비를 감당하지 못한다.
    assert inv.mine_roi_order(levels, temp_max=40, energy_surplus=0) == []
