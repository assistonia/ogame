"""레이더 성장 목표가 실전 기능의 선행 트리를 실제로 닫는지 검증."""
from __future__ import annotations

from bots.rule_based import personality, turtle
from bots.rule_based.raider import RECYCLER_REQUIREMENTS


def _target_levels_for(personality_key: str, kind: str) -> dict[int, int]:
    levels: dict[int, int] = {}
    for technology_id, target in personality.GROWTH[personality_key][kind]:
        levels[technology_id] = max(levels.get(technology_id, 0), target)
    return levels


def _target_levels(kind: str) -> dict[int, int]:
    return _target_levels_for("raider", kind)


def test_raider_curriculum_closes_recycler_prerequisite_tree():
    levels = _target_levels("facility_targets")
    levels.update(_target_levels("research_targets"))

    assert all(levels.get(tid, 0) >= target for tid, target in RECYCLER_REQUIREMENTS)
    assert personality.prereqs_met(110, levels), "shielding2 자체 선행조건도 충족해야 한다"


def test_raider_curriculum_can_reveal_full_espionage_intel():
    research = _target_levels("research_targets")
    ship_targets = {
        technology_id: target
        for technology_id, target, _batch in personality.GROWTH["raider"]["ship_targets"]
    }

    assert research.get(106, 0) >= 4
    assert ship_targets.get(210, 0) >= 12


def test_raider_curriculum_closes_military_research_and_capital_ship_trees():
    levels = _target_levels("facility_targets")
    research = _target_levels("research_targets")
    levels.update(research)
    ship_targets = {
        technology_id: target
        for technology_id, target, _batch in personality.GROWTH["raider"]["ship_targets"]
    }

    # 사용자가 지정한 군사 연구 5종은 함종 언락 최소치에서 멈추지 않고
    # 전 레이더의 공통 전투 기반이 되도록 의미 있는 목표치까지 올라간다.
    assert research[109] >= 6  # weapon
    assert research[110] >= 6  # shielding
    assert research[111] >= 6  # armor
    assert research[117] >= 6  # impulse
    assert research[121] >= 5  # ion

    # 커리큘럼 최종 상태에서 각 연구 자체의 서버 선행조건과
    # 순양함/전함/순양전함+대형화물 선행조건이 모두 닫힌다.
    for technology_id, _target in personality.GROWTH["raider"]["research_targets"]:
        assert personality.prereqs_met(technology_id, levels)
    for ship_id in (203, 206, 207, 215):
        assert ship_targets.get(ship_id, 0) > 0
        assert personality.prereqs_met(ship_id, levels)


def test_raider_research_milestones_unlock_cruiser_before_endgame_targets():
    # 기존 레이더 커리큘럼을 끝낸 직후: 순양함 단계의 첫 미달 에너지4가
    # 에너지5 최종치와 중복해 나오지 않고 최우선 연구가 된다.
    levels = _target_levels("facility_targets")
    levels.update({
        113: 3, 115: 6, 106: 4, 108: 3, 109: 4,
        111: 2, 117: 2, 110: 3,
    })
    actions = personality.plan_secondary("raider", levels, tick_index=2)
    research_ids = [action[1] for action in actions if action[0] == "research"]

    assert research_ids[0] == 113
    assert len(research_ids) == len(set(research_ids))

    # 순양함 최소 마일스톤을 닫으면 최종 무기/엔진 연구가 남아 있어도
    # 순양함 건조 후보가 즉시 생긴다.
    levels.update({113: 4, 120: 5, 121: 2, 117: 4})
    actions = personality.plan_secondary("raider", levels, tick_index=1)
    assert any(action[0] == "ship" and action[1] == 206 for action in actions)


def test_turtle_curriculum_reaches_an_executable_gauss_path():
    levels = _target_levels_for("turtle", "facility_targets")
    levels.update(_target_levels_for("turtle", "research_targets"))

    assert personality.prereqs_met(turtle.GAUSS_CANNON, levels)
    candidates = turtle.plan_defense(
        {turtle.ROCKET_LAUNCHER: 16, turtle.GAUSS_CANNON: 0}, levels
    )
    assert candidates[0] == (turtle.GAUSS_CANNON, 2)
