"""OGameX 투자 계산기: 건설/연구 시간, 로봇공장 상각, 광산 ROI.

이 모듈은 DB/HTTP를 읽지 않는 순수 계산 계층이다. 공식의 기준은 현재 같이
배포된 OGameX 서버다.

* 건설 시간: ``PlanetService::getBuildingConstructionTime``
* 연구 시간: ``PlanetService::getTechnologyResearchTime``
* 광산 생산: ``BuildingObjects`` / ``GameObjectProduction::calculateMine``

중요한 단위 규칙
------------------

* 자원 가치는 3:2:1 교환비의 메탈 환산값을 사용한다. 즉 metal=1,
  crystal=1.5, deuterium=3 이며, **생산과 비용 양쪽에 같은 가중치**를
  곱한다.
* ``mine_roi_order``의 점수는 ``시간당 생산가치 / 투자비용``이라 단위가
  ``1/hour``다. 역수가 단순 회수시간(hour)이다.
* 로봇공장은 자원을 생산하지 않는다. ``robot_payback_hours``는 로봇공장
  투자에 드는 실제 경과시간(부족 자원 재생산 + 건설 행동)을, 앞으로의 건설
  믹스가 만드는 건설시간 절감률로 회수하는 데 걸리는 **경과시간(hour)**을
  반환한다. 초/초 비율을 시간이라고 부르지 않는다.
* 봇은 기본 4분마다 한 번 행동한다. 건설이 그보다 빨리 끝나도 다음 행동을
  앞당길 수 없으므로 로봇공장 상각에서 각 건설시간은 최소 한 틱으로 본다.

한계
----

광산 계산은 서버의 기본 광산 생산과 플라즈마 보너스만 복제한다. 행성 위치,
Collector/Geologist, crawler, item 같은 계정별 보너스는 ``production_multipliers``
또는 실제 ``production_per_hour``를 호출부가 넘겨야 한다. 듀테륨 온도 보정을
추측하지 않도록 ``temp_max``를 필수로 받는다(OGameX는 ``temp_min=temp_max-40``).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping, Sequence

from bots.shared import game_objects as go


_SECONDS_PER_HOUR = 3600
_NANO_FACTORY = 15
_DEFAULT_TICK_SECONDS = 240

_RESOURCE_NAMES = ("metal", "crystal", "deuterium")
_RESOURCE_VALUES: dict[str, float] = {
    "metal": 1.0,
    "crystal": 1.5,
    "deuterium": 3.0,
}
_MINE_IDS = (go.METAL_MINE, go.CRYSTAL_MINE, go.DEUTERIUM_SYNTHESIZER)
_MINE_RESOURCE = {
    go.METAL_MINE: "metal",
    go.CRYSTAL_MINE: "crystal",
    go.DEUTERIUM_SYNTHESIZER: "deuterium",
}
_MINE_BASE_PRODUCTION = {
    go.METAL_MINE: 30.0,
    go.CRYSTAL_MINE: 20.0,
    go.DEUTERIUM_SYNTHESIZER: 10.0,
}
# PlanetService/GameObjectProduction: plasma level당 자원별 보너스.
_PLASMA_BONUS_PER_LEVEL = {
    "metal": 0.01,
    "crystal": 0.0066,
    "deuterium": 0.0033,
}


@dataclass(frozen=True)
class RobotPayback:
    """로봇공장 +1 투자 분석의 중간값과 최종 회수시간.

    ``plan_*_seconds``와 ``saved_seconds``는 봇 행동 간격을 반영한 유효 건설
    시간이다. ``resource_wait_seconds``는 현재 보유량으로 모자란 로봇공장 비용을
    현재 생산량으로 다시 모으는 보수적 시간이며, 세 자원 중 가장 늦게 모이는
    자원을 기준으로 한다(자원 간 자유교환을 가정하지 않음).
    """

    target_level: int
    robot_cost: dict[str, int]
    robot_build_seconds: int
    effective_robot_build_seconds: int
    resource_wait_seconds: float
    plan_before_seconds: int
    plan_after_seconds: int
    saved_seconds: int
    payback_hours: float
    pays_back_within_plan: bool


@dataclass(frozen=True)
class NaniteInvestmentRecommendation:
    """Explainable, scenario-level recommendation in metal-equivalent value.

    The model intentionally exposes rounded components rather than claiming an
    exact combat-income forecast.  It compares the same bounded mine plan with
    and without the Robotics-10/Computer-10/Nanite path, then adds conservative
    ship and defense throughput scenarios.
    """

    target_nanite_level: int
    recommended: bool
    economically_positive: bool
    ready_now: bool
    reason: str
    horizon_hours: float
    universe_speed: int
    robot_level: int
    computer_level: int
    nanite_level: int
    queues_known: bool
    building_queue_seconds: int
    unit_queue_seconds: int
    facility_cost: dict[str, int]
    facility_cost_value: float
    nanite_build_seconds: int
    prerequisite_seconds: int
    resource_wait_seconds: float
    building_speed_benefit: float
    ship_speed_benefit: float
    defense_speed_benefit: float
    reinvestment_benefit: float
    delayed_mine_cost: float
    shipyard_downtime_cost: float
    total_benefit: float
    total_cost: float
    net_benefit: float
    payback_hours: float | None


def _positive_speed(value: int | float, name: str) -> float:
    """서버와 같이 0 배속은 1로 보정하고, 음수/비수치는 거부한다."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}는 유한한 수여야 합니다.")
    if value < 0:
        raise ValueError(f"{name}는 0 이상이어야 합니다.")
    return 1.0 if value == 0 else float(value)


def _nonnegative_level(value: int, name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}는 0 이상의 정수여야 합니다.")
    return value


def _object_cost(technology_id: int, target_level: int) -> dict[str, int]:
    cost = go.object_cost(technology_id, target_level)
    if cost is None:
        raise ValueError(
            f"비용표에 없는 technology_id={technology_id}, target_level={target_level}"
        )
    return cost


def build_time_seconds(
    technology_id: int,
    target_level: int,
    robot_lv: int,
    nanite_lv: int,
    universe_speed: int = 100,
) -> int:
    """건물/시설 ``target_level``의 서버 건설시간을 초로 반환한다.

    ``universe_speed``는 OGameX ``economy_speed``다. 나노봇 공장 자체는 서버와
    같이 목표 레벨 보정계수 ``max(4-L/2, 1)``를 적용하지 않는다.
    """
    if technology_id not in go.BUILDINGS and technology_id not in go.FACILITIES:
        raise ValueError(f"건물/시설이 아닌 technology_id={technology_id}")
    if not isinstance(target_level, int) or target_level <= 0:
        raise ValueError("target_level는 1 이상의 정수여야 합니다.")
    robot = _nonnegative_level(robot_lv, "robot_lv")
    nanite = _nonnegative_level(nanite_lv, "nanite_lv")
    speed = _positive_speed(universe_speed, "universe_speed")

    cost = _object_cost(technology_id, target_level)
    cost_sum = cost["metal"] + cost["crystal"]
    divisor = 2500 * (1 + robot) * speed * (2**nanite)
    if technology_id != _NANO_FACTORY:
        divisor *= max(4 - (target_level / 2), 1)

    return max(int((cost_sum / divisor) * _SECONDS_PER_HOUR), 1)


def research_time_seconds(
    technology_id: int,
    target_level: int,
    lab_lv: int,
    universe_speed: int = 100,
    research_speed: int = 100,
    character_class_multiplier: float = 1.0,
) -> int:
    """연구 ``target_level``의 서버 연구시간을 초로 반환한다.

    OGameX의 분모는 ``economy_speed * research_speed``다. ``lab_lv``에는 은하간
    연구망까지 합친 서버의 유효 연구소 레벨을 넘겨야 한다. Discoverer라면 서버의
    클래스 보정값 ``0.75``를 ``character_class_multiplier``로 넘긴다.
    """
    if technology_id not in go.RESEARCH:
        raise ValueError(f"연구가 아닌 technology_id={technology_id}")
    if not isinstance(target_level, int) or target_level <= 0:
        raise ValueError("target_level는 1 이상의 정수여야 합니다.")
    lab = _nonnegative_level(lab_lv, "lab_lv")
    economy = _positive_speed(universe_speed, "universe_speed")
    research = _positive_speed(research_speed, "research_speed")
    if (
        not isinstance(character_class_multiplier, (int, float))
        or not math.isfinite(character_class_multiplier)
        or character_class_multiplier <= 0
    ):
        raise ValueError("character_class_multiplier는 0보다 큰 유한한 수여야 합니다.")

    cost = _object_cost(technology_id, target_level)
    cost_sum = cost["metal"] + cost["crystal"]
    seconds = int(
        (cost_sum / (1000 * (1 + lab) * economy * research)) * _SECONDS_PER_HOUR
    )
    # 서버는 기본 초를 먼저 int로 자른 뒤 클래스 배율을 적용해 다시 int로 자른다.
    if character_class_multiplier != 1.0:
        seconds = int(seconds * character_class_multiplier)
    return max(seconds, 1)


def _temperature_factor(temp_max: float) -> float:
    if not isinstance(temp_max, (int, float)) or not math.isfinite(temp_max):
        raise ValueError("temp_max는 유한한 수여야 합니다.")
    # OGameX 행성 생성/이동: temp_min = temp_max - 40.
    temp_avg = float(temp_max) - 20.0
    return 1.44 - (0.004 * temp_avg)


def _mine_base_production_per_hour(
    technology_id: int,
    level: int,
    universe_speed: int,
    temp_max: float | None,
) -> float:
    """보너스 전 서버 광산 생산량. 로봇 비용 재생산 시간 계산에도 사용한다."""
    if technology_id not in _MINE_IDS:
        raise ValueError(f"광산이 아닌 technology_id={technology_id}")
    level = _nonnegative_level(level, "mine level")
    if level == 0:
        return 0.0
    speed = _positive_speed(universe_speed, "universe_speed")
    temperature = 1.0
    if technology_id == go.DEUTERIUM_SYNTHESIZER:
        if temp_max is None:
            raise ValueError("듀테륨 생산 계산에는 temp_max가 필요합니다.")
        temperature = _temperature_factor(temp_max)
    return (
        _MINE_BASE_PRODUCTION[technology_id]
        * level
        * (1.1**level)
        * temperature
        * speed
    )


def _resource_production_from_levels(
    levels: Mapping[int, int],
    universe_speed: int,
    temp_max: float | None,
) -> dict[str, float]:
    return {
        _MINE_RESOURCE[tid]: _mine_base_production_per_hour(
            tid, max(0, int(levels.get(tid, 0))), universe_speed, temp_max
        )
        for tid in _MINE_IDS
    }


def _known_mine_output_per_hour(
    technology_id: int,
    level: int,
    universe_speed: int,
    temp_max: float,
    production_factor: float,
    plasma_level: int,
) -> int:
    """서버에서 이 모듈 입력만으로 확정 가능한 광산+플라즈마 생산량.

    OGameX는 에너지 생산계수를 기본 광산 생산에 먼저 곱하고, 그 값에 대해
    플라즈마 보너스를 ``floor``한 뒤 최종 자원 생산을 ``ceil``한다.
    """
    base = (
        _mine_base_production_per_hour(technology_id, level, universe_speed, temp_max)
        * production_factor
    )
    resource = _MINE_RESOURCE[technology_id]
    plasma = math.floor(base * _PLASMA_BONUS_PER_LEVEL[resource] * plasma_level)
    return math.ceil(base + plasma)


def _effective_build_seconds(seconds: int, tick_interval_seconds: int) -> int:
    """짧은 건설은 다음 봇 행동까지 큐 회전을 앞당기지 못하므로 최소 한 틱."""
    if not isinstance(tick_interval_seconds, int) or tick_interval_seconds < 0:
        raise ValueError("tick_interval_seconds는 0 이상의 정수여야 합니다.")
    return (
        seconds if tick_interval_seconds == 0 else max(seconds, tick_interval_seconds)
    )


def _validate_plan(
    levels: Mapping[int, int], planned_builds: Sequence[tuple[int, int]]
) -> None:
    """계획이 현재 레벨부터 한 단계씩 이어지는 실제 가능한 업그레이드인지 검사."""
    simulated = {
        tid: max(0, int(levels.get(tid, 0))) for tid in (*go.BUILDINGS, *go.FACILITIES)
    }
    for item in planned_builds:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(
                "planned_builds 항목은 (technology_id, target_level) 튜플이어야 합니다."
            )
        technology_id, target_level = item
        if technology_id not in go.BUILDINGS and technology_id not in go.FACILITIES:
            raise ValueError(f"계획에 건물/시설이 아닌 technology_id={technology_id}")
        if technology_id == go.ROBOT_FACTORY:
            raise ValueError("평가 대상 로봇공장을 planned_builds에 중복 포함할 수 없습니다.")
        expected = simulated.get(technology_id, 0) + 1
        if target_level != expected:
            raise ValueError(
                f"technology_id={technology_id} 목표 레벨은 {expected}이어야 합니다 "
                f"(입력={target_level})."
            )
        simulated[technology_id] = target_level


def robot_payback_analysis(
    levels: Mapping[int, int],
    planned_builds: Sequence[tuple[int, int]],
    universe_speed: int = 100,
    *,
    tick_interval_seconds: int = _DEFAULT_TICK_SECONDS,
    temp_max: float | None = None,
    available_resources: Mapping[str, float] | None = None,
    production_per_hour: Mapping[str, float] | None = None,
) -> RobotPayback | None:
    """로봇공장 +1의 차원 일치 상각 분석을 반환한다.

    반환값이 ``None``인 경우는 (a) 계획이 비었거나, (b) 봇 틱까지 반영하면 시간이
    전혀 절약되지 않거나, (c) 부족한 자원을 현재 생산으로 회복할 수 없을 때다.

    ``production_per_hour``에는 가능하면 DB가 정산한 실제 생산량을 넘긴다. 없으면
    ``levels``/``temp_max``로 보너스 전 생산을 보수적으로 계산한다. 보유 자원을 넘기지
    않으면 로봇공장 비용 전액을 새로 생산해야 한다고 본다.
    """
    if not planned_builds:
        return None
    _validate_plan(levels, planned_builds)
    speed = _positive_speed(universe_speed, "universe_speed")
    # build_time_seconds가 같은 검사를 수행하지만, 빈 계획에서도 옵션 오류를 놓치지 않는다.
    _effective_build_seconds(1, tick_interval_seconds)

    robot_lv = _nonnegative_level(int(levels.get(go.ROBOT_FACTORY, 0)), "robot level")
    nanite_initial = _nonnegative_level(
        int(levels.get(_NANO_FACTORY, 0)), "nanite level"
    )
    target_level = robot_lv + 1
    robot_cost = _object_cost(go.ROBOT_FACTORY, target_level)
    robot_build_seconds = build_time_seconds(
        go.ROBOT_FACTORY, target_level, robot_lv, nanite_initial, int(speed)
    )
    effective_robot_build = _effective_build_seconds(
        robot_build_seconds, tick_interval_seconds
    )

    # 나노 공장이 계획 중 올라가면 그 뒤 항목에는 새 나노 레벨을 반영한다.
    before_nanite = nanite_initial
    after_nanite = nanite_initial
    before_total = 0
    after_total = 0
    for technology_id, target in planned_builds:
        before = build_time_seconds(
            technology_id, target, robot_lv, before_nanite, int(speed)
        )
        after = build_time_seconds(
            technology_id, target, robot_lv + 1, after_nanite, int(speed)
        )
        before_total += _effective_build_seconds(before, tick_interval_seconds)
        after_total += _effective_build_seconds(after, tick_interval_seconds)
        if technology_id == _NANO_FACTORY:
            before_nanite = target
            after_nanite = target

    saved = before_total - after_total
    if saved <= 0 or before_total <= 0:
        return None

    available = available_resources or {}
    for resource in _RESOURCE_NAMES:
        value = float(available.get(resource, 0.0))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"available_resources[{resource!r}]는 0 이상의 유한한 수여야 합니다.")

    if production_per_hour is None:
        production = _resource_production_from_levels(levels, int(speed), temp_max)
    else:
        production = {}
        for resource in _RESOURCE_NAMES:
            value = float(production_per_hour.get(resource, 0.0))
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"production_per_hour[{resource!r}]는 0 이상의 유한한 수여야 합니다."
                )
            production[resource] = value

    resource_wait_hours = 0.0
    for resource in _RESOURCE_NAMES:
        deficit = max(
            float(robot_cost[resource]) - float(available.get(resource, 0.0)), 0.0
        )
        if deficit == 0:
            continue
        hourly = production[resource]
        if hourly <= 0:
            return None
        resource_wait_hours = max(resource_wait_hours, deficit / hourly)

    resource_wait_seconds = resource_wait_hours * _SECONDS_PER_HOUR
    investment_seconds = effective_robot_build + resource_wait_seconds
    # 같은 계획 믹스가 이어진다는 가정의 시간 절감률(saved / before_total)로 투자시간 회수.
    savings_rate = saved / before_total
    payback_hours = (investment_seconds / savings_rate) / _SECONDS_PER_HOUR
    return RobotPayback(
        target_level=target_level,
        robot_cost=dict(robot_cost),
        robot_build_seconds=robot_build_seconds,
        effective_robot_build_seconds=effective_robot_build,
        resource_wait_seconds=resource_wait_seconds,
        plan_before_seconds=before_total,
        plan_after_seconds=after_total,
        saved_seconds=saved,
        payback_hours=payback_hours,
        pays_back_within_plan=saved >= investment_seconds,
    )


def robot_payback_hours(
    levels: Mapping[int, int],
    planned_builds: Sequence[tuple[int, int]],
    universe_speed: int = 100,
    *,
    tick_interval_seconds: int = _DEFAULT_TICK_SECONDS,
    temp_max: float | None = None,
    available_resources: Mapping[str, float] | None = None,
    production_per_hour: Mapping[str, float] | None = None,
) -> float | None:
    """``robot_payback_analysis``의 회수시간(hour)만 반환하는 편의 함수."""
    analysis = robot_payback_analysis(
        levels,
        planned_builds,
        universe_speed,
        tick_interval_seconds=tick_interval_seconds,
        temp_max=temp_max,
        available_resources=available_resources,
        production_per_hour=production_per_hour,
    )
    return None if analysis is None else analysis.payback_hours


def _resource_values(values: Mapping[str, float] | None) -> dict[str, float]:
    result = dict(_RESOURCE_VALUES)
    if values is not None:
        result.update(values)
    for resource in _RESOURCE_NAMES:
        value = result.get(resource)
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"resource_values[{resource!r}]는 0보다 큰 유한한 수여야 합니다.")
        result[resource] = float(value)
    return result


def _cost_value(cost: Mapping[str, int], values: Mapping[str, float]) -> float:
    return sum(float(cost[resource]) * values[resource] for resource in _RESOURCE_NAMES)


def mine_roi_order(
    levels: Mapping[int, int],
    energy_ok: bool = True,
    *,
    universe_speed: int = 100,
    temp_max: float | None = None,
    energy_surplus: int | None = None,
    production_factor: float = 1.0,
    resource_values: Mapping[str, float] | None = None,
    production_multipliers: Mapping[str, float] | None = None,
) -> list[tuple[int, float]]:
    """다음 메탈/크리스탈/듀테륨 광산을 ROI(1/hour) 내림차순으로 반환한다.

    에너지 적자(``energy_ok=False``)일 때 광산을 추천하지 않는다. ``energy_surplus``를
    주면 다음 레벨의 추가 에너지 소비를 감당하지 못하는 후보도 제외한다. 이로써 ROI가
    좋아 보여도 실제 생산계수가 하락하는 업그레이드를 선행 에너지 건설보다 앞세우지 않는다.

    ``temp_max``는 듀테륨 공식에 필수다. ``production_multipliers``는 서버 기본 생산과
    플라즈마 보너스 뒤에 적용할 자원별 추가 배율(Collector/아이템/행성 위치 등을 호출부가
    알고 있을 때)을 받는다.
    """
    if not energy_ok:
        return []
    speed = _positive_speed(universe_speed, "universe_speed")
    if temp_max is None:
        raise ValueError("광산 ROI에는 듀테륨 온도 보정을 위한 temp_max가 필요합니다.")
    # 입력 온도를 미리 검증한다(듀테륨 레벨 0이어도 다음 레벨 비교에는 필요).
    _temperature_factor(temp_max)
    if (
        not isinstance(production_factor, (int, float))
        or not math.isfinite(production_factor)
        or not 0 < production_factor <= 1
    ):
        raise ValueError("production_factor는 0 초과 1 이하의 유한한 수여야 합니다.")
    if energy_surplus is not None and (
        not isinstance(energy_surplus, int) or energy_surplus < 0
    ):
        raise ValueError("energy_surplus는 0 이상의 정수여야 합니다.")

    values = _resource_values(resource_values)
    multipliers = {resource: 1.0 for resource in _RESOURCE_NAMES}
    if production_multipliers is not None:
        for resource in _RESOURCE_NAMES:
            value = float(production_multipliers.get(resource, 1.0))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"production_multipliers[{resource!r}]는 0보다 큰 유한한 수여야 합니다."
                )
            multipliers[resource] = value

    plasma_level = max(0, int(levels.get(122, 0)))
    items: list[tuple[int, float]] = []
    for technology_id in _MINE_IDS:
        current_level = max(0, int(levels.get(technology_id, 0)))
        next_level = current_level + 1
        if energy_surplus is not None:
            extra_energy = go.mine_energy_consumption(
                technology_id, next_level
            ) - go.mine_energy_consumption(technology_id, current_level)
            if extra_energy > energy_surplus:
                continue

        cost = _object_cost(technology_id, next_level)
        resource = _MINE_RESOURCE[technology_id]
        current_output = _known_mine_output_per_hour(
            technology_id,
            current_level,
            int(speed),
            temp_max,
            float(production_factor),
            plasma_level,
        )
        next_output = _known_mine_output_per_hour(
            technology_id,
            next_level,
            int(speed),
            temp_max,
            float(production_factor),
            plasma_level,
        )
        marginal_output = next_output - current_output
        marginal_output *= multipliers[resource]
        marginal_value_per_hour = marginal_output * values[resource]
        investment_value = _cost_value(cost, values)
        if marginal_value_per_hour <= 0 or investment_value <= 0:
            continue
        items.append((technology_id, marginal_value_per_hour / investment_value))

    items.sort(key=lambda item: (-item[1], item[0]))
    return items


def nanite_investment_recommendation(
    levels: Mapping[int, int],
    *,
    universe_speed: int = 100,
    horizon_hours: float = 168.0,
    queues_known: bool = True,
    building_queue_depth: int = 0,
    building_queue_seconds: int = 0,
    unit_queue_depth: int = 0,
    unit_queue_seconds: int = 0,
    resource_state: Mapping[str, float] | None = None,
    production_per_hour: Mapping[str, float] | None = None,
    ship_utilization: float = 0.20,
    defense_utilization: float = 0.05,
) -> NaniteInvestmentRecommendation:
    """Evaluate the full prerequisite path and next Nanite level.

    Benefits and costs use metal-equivalent values (1/1.5/3).  The bounded
    comparison includes current queue waits, prerequisite and facility costs,
    Nanite's building/unit halving, mine-production delayed while the building
    lane is occupied, unit-lane downtime during the Nanite upgrade, and one
    conservative earlier-reinvestment allowance.  Existing queues are never
    changed; they only affect ``ready_now`` and the scenario timeline.
    """
    speed = int(_positive_speed(universe_speed, "universe_speed"))
    horizon = max(24.0, min(24.0 * 30.0, float(horizon_hours)))
    horizon_seconds = horizon * _SECONDS_PER_HOUR
    building_wait = max(0, int(building_queue_seconds))
    unit_wait = max(0, int(unit_queue_seconds))
    robot = max(0, int(levels.get(go.ROBOT_FACTORY, 0) or 0))
    computer = max(0, int(levels.get(108, 0) or 0))
    nanite = max(0, int(levels.get(_NANO_FACTORY, 0) or 0))
    lab = max(0, int(levels.get(go.RESEARCH_LAB, 0) or 0))
    target_nanite = nanite + 1

    path_cost = {resource: 0 for resource in _RESOURCE_NAMES}
    robot_seconds = 0
    simulated_robot = robot
    for target in range(robot + 1, 11):
        cost = _object_cost(go.ROBOT_FACTORY, target)
        for resource in _RESOURCE_NAMES:
            path_cost[resource] += cost[resource]
        robot_seconds += build_time_seconds(
            go.ROBOT_FACTORY, target, target - 1, nanite, speed
        )
        simulated_robot = target

    computer_seconds = 0
    for target in range(computer + 1, 11):
        cost = _object_cost(108, target)
        for resource in _RESOURCE_NAMES:
            path_cost[resource] += cost[resource]
        computer_seconds += research_time_seconds(108, target, lab, speed)

    nanite_cost = _object_cost(_NANO_FACTORY, target_nanite)
    for resource in _RESOURCE_NAMES:
        path_cost[resource] += nanite_cost[resource]
    final_robot = max(simulated_robot, 10)
    nanite_seconds = build_time_seconds(
        _NANO_FACTORY, target_nanite, final_robot, nanite, speed
    )

    state = resource_state or {}
    temp_max = state.get("temp_max")
    if production_per_hour is not None:
        production = {
            resource: max(0.0, float(production_per_hour.get(resource, 0.0)))
            for resource in _RESOURCE_NAMES
        }
    else:
        production = {}
        for tid in _MINE_IDS:
            resource = _MINE_RESOURCE[tid]
            try:
                production[resource] = _mine_base_production_per_hour(
                    tid,
                    max(0, int(levels.get(tid, 0) or 0)),
                    speed,
                    None if temp_max is None else float(temp_max),
                )
            except (TypeError, ValueError):
                production[resource] = 0.0

    available = {
        resource: max(0.0, float(state.get(resource, 0.0) or 0.0))
        for resource in _RESOURCE_NAMES
    }
    resource_wait_hours = 0.0
    funding_possible = True
    for resource in _RESOURCE_NAMES:
        deficit = max(0.0, path_cost[resource] - available[resource])
        if deficit <= 0:
            continue
        if production.get(resource, 0.0) <= 0:
            funding_possible = False
            resource_wait_hours = horizon
            break
        resource_wait_hours = max(
            resource_wait_hours, deficit / production[resource]
        )
    resource_wait_seconds = resource_wait_hours * _SECONDS_PER_HOUR

    # Robotics buildings can proceed while units finish; Nanite cannot start
    # until both prerequisite lanes and the live unit queue are clear.
    robot_done = building_wait + resource_wait_seconds + robot_seconds
    prerequisites_done = max(robot_done, float(computer_seconds), float(unit_wait))
    facility_done = prerequisites_done + nanite_seconds

    values = _resource_values(None)
    facility_cost_value = _cost_value(path_cost, values)
    production_value_per_hour = sum(
        production.get(resource, 0.0) * values[resource]
        for resource in _RESOURCE_NAMES
    )

    # Compare a transparent two-round mine plan on identical ordering.  This
    # captures both the initial delay and earlier completions after Robotics and
    # Nanite speedups without assuming an infinite-growth future.
    baseline_at = float(building_wait)
    invested_at = float(facility_done)
    simulated_levels = {
        tid: max(0, int(levels.get(tid, 0) or 0)) for tid in _MINE_IDS
    }
    building_gain = 0.0
    delayed_mines = 0.0
    plasma = max(0, int(levels.get(122, 0) or 0))
    if temp_max is not None:
        for _round in range(2):
            for tid in _MINE_IDS:
                current = simulated_levels[tid]
                target = current + 1
                baseline_at += build_time_seconds(
                    tid, target, robot, nanite, speed
                )
                invested_at += build_time_seconds(
                    tid, target, final_robot, target_nanite, speed
                )
                try:
                    before = _known_mine_output_per_hour(
                        tid, current, speed, float(temp_max), 1.0, plasma
                    )
                    after = _known_mine_output_per_hour(
                        tid, target, speed, float(temp_max), 1.0, plasma
                    )
                except (TypeError, ValueError):
                    simulated_levels[tid] = target
                    continue
                marginal = max(0.0, float(after - before)) * values[_MINE_RESOURCE[tid]]
                baseline_value = marginal * max(
                    0.0, horizon_seconds - baseline_at
                ) / _SECONDS_PER_HOUR
                invested_value = marginal * max(
                    0.0, horizon_seconds - invested_at
                ) / _SECONDS_PER_HOUR
                delta = invested_value - baseline_value
                if delta >= 0:
                    building_gain += delta
                else:
                    delayed_mines += -delta
                simulated_levels[tid] = target

    ship_u = max(0.0, min(1.0, float(ship_utilization)))
    defense_u = max(0.0, min(1.0, float(defense_utilization)))
    total_u = ship_u + defense_u
    if total_u > 1.0:
        ship_u /= total_u
        defense_u /= total_u
    # A +1 Nanite level halves future ship and defense work.  Value the saved
    # lane time only at the corresponding share of current production flow.
    ship_saved_hours = horizon * ship_u * 0.5
    defense_saved_hours = horizon * defense_u * 0.5
    ship_gain = production_value_per_hour * ship_u * ship_saved_hours
    defense_gain = production_value_per_hour * defense_u * defense_saved_hours
    shipyard_downtime = (
        production_value_per_hour
        * (ship_u + defense_u)
        * (nanite_seconds / _SECONDS_PER_HOUR)
    )

    gross_direct = building_gain + ship_gain + defense_gain
    reinvestment = gross_direct * 0.10 * max(
        0.0, min(1.0, (horizon_seconds - facility_done) / horizon_seconds)
    )
    total_benefit = gross_direct + reinvestment
    total_cost = facility_cost_value + delayed_mines + shipyard_downtime
    net = total_benefit - total_cost
    benefit_rate = total_benefit / horizon if horizon > 0 else 0.0
    payback = (
        (facility_done / _SECONDS_PER_HOUR) + total_cost / benefit_rate
        if benefit_rate > 0
        else None
    )
    economic = funding_possible and net > 0 and payback is not None and payback <= horizon
    queue_permitted = (
        queues_known and building_queue_depth == 0 and unit_queue_depth == 0
    )
    prerequisites_met = robot >= 10 and computer >= 10
    recommended = queues_known and economic
    ready_now = recommended and prerequisites_met and queue_permitted
    if not queues_known:
        reason = "queue_state_unavailable"
    elif not funding_possible:
        reason = "facility_path_not_fundable_within_horizon"
    elif not economic:
        reason = "bounded_benefit_does_not_cover_cost"
    elif building_queue_depth > 0:
        reason = "building_queue_busy"
    elif prerequisites_met and unit_queue_depth > 0:
        reason = "shipyard_queue_blocks_nanite"
    elif robot < 10:
        reason = "close_robot_factory_10"
    elif computer < 10:
        reason = "close_computer_technology_10"
    else:
        reason = "positive_bounded_payback"

    return NaniteInvestmentRecommendation(
        target_nanite_level=target_nanite,
        recommended=recommended,
        economically_positive=economic,
        ready_now=ready_now,
        reason=reason,
        horizon_hours=round(horizon, 1),
        universe_speed=speed,
        robot_level=robot,
        computer_level=computer,
        nanite_level=nanite,
        queues_known=queues_known,
        building_queue_seconds=building_wait,
        unit_queue_seconds=unit_wait,
        facility_cost=dict(path_cost),
        facility_cost_value=round(facility_cost_value, 1),
        nanite_build_seconds=nanite_seconds,
        prerequisite_seconds=int(prerequisites_done),
        resource_wait_seconds=round(resource_wait_seconds, 1),
        building_speed_benefit=round(building_gain, 1),
        ship_speed_benefit=round(ship_gain, 1),
        defense_speed_benefit=round(defense_gain, 1),
        reinvestment_benefit=round(reinvestment, 1),
        delayed_mine_cost=round(delayed_mines, 1),
        shipyard_downtime_cost=round(shipyard_downtime, 1),
        total_benefit=round(total_benefit, 1),
        total_cost=round(total_cost, 1),
        net_benefit=round(net, 1),
        payback_hours=None if payback is None else round(payback, 1),
    )
