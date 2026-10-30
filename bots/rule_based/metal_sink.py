"""보수적인 금속 과잉 해소 의사결정기(부작용 없음).

이 모듈은 자원을 직접 쓰거나 HTTP/DB를 호출하지 않는다. 호출부가 이미 모은 스냅샷을
``plan_metal_sink``에 넘기면, 이번 틱에 허용할 행동 한 건 또는 명시적인 hold 사유를
돌려준다. 실제 통합 전 로그/드라이런으로 분포를 관측할 수 있도록 계획 계층을 분리했다.

금속 sink는 성장 커리큘럼의 대체물이 아니라, 다음 조건을 모두 만족한 뒤 남는 금속만
다룬다.

* 현재 홈월드이고 에너지 적자가 아니며 식민 행동이 대기 중이지 않을 것.
* 건설/연구 큐가 봇의 틱당 안전 깊이 이내이고, 함선·방어가 공유하는 unit queue는 빌 것.
* 호출부가 다음 건물·연구·식민·연료 비용을 합산한 ``reserve``를 제공할 것. 이 모듈은
  다시 25% 안전 여유와 절대 하한을 적용한다.
* 금속의 안전 잉여가 크리스탈 잉여의 2.5배 이상이거나 금속 창고가 85% 이상 찰 것.

행동 우선순위는 다음과 같다.

1. turtle은 기존 ``turtle.defense_tick``이 전담하므로 이 모듈이 전혀 건드리지 않는다.
2. 그 밖의 성격은 로켓런처 5기까지만 먼저 갖춰 최소 총알받이를 만든다. explorer는 이미
   공용 후행 파이프라인에서 turtle 방어를 쓰므로 이 단계도 생략한다.
3. 안전한 플릿세이브 목적지와 빈 슬롯이 있고, 완성 직후 플릿세이브 임계도 넘길 수 있을
   때만 대형 화물선을 소량 만든다. 총 보유량은 행성 주기장뿐 아니라 비행·unit queue까지
   포함한 값을 호출부가 넘겨야 하므로, 세이브 중 수량이 0처럼 보여 과잉 생산되지 않는다.
4. 대형화물 목표를 채웠거나 안전하게 세이브할 수 없으면 로켓런처 총 20기까지만 추가한다.

대형 화물선은 유용한 수송 인프라이지만 6000M+6000C라 크리스탈을 같이 먹는다. 한 틱에
각 잉여의 25%만 쓸 수 있게 하고, 로켓런처도 동일하게 금속 잉여의 25%만 쓴다. 따라서
이 모듈은 무한 방어를 만드는 두 번째 turtle이 아니며, 광산·에너지·식민·플릿세이브를
굶기지 않는 제한된 overflow valve다.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Mapping

from bots.rule_based.personality import PERSONALITIES, prereqs_met
from bots.shared import game_objects as go


# 서버 GameObjects 소스의 canonical id를 기존 통합 매핑에서 가져온다.
LARGE_CARGO = go.id_of("large_cargo")
ROCKET_LAUNCHER = go.id_of("rocket_launcher")

# OGameX CivilShipObjects.php / DefenseObjects.php의 고정 단가.
UNIT_COST: dict[int, tuple[int, int, int]] = {
    LARGE_CARGO: (6_000, 6_000, 0),
    ROCKET_LAUNCHER: (2_000, 0, 0),
}
LARGE_CARGO_VALUE = sum(UNIT_COST[LARGE_CARGO])

# reserve는 호출부가 계산하지만, 잘못 작게 전달돼도 다음 성장 행동을 굶기지 않을 하한.
MIN_RESERVE: dict[str, int] = {
    "metal": 25_000,
    "crystal": 15_000,
    "deuterium": 10_000,
}
RESERVE_MULTIPLIER = 1.25

# 금속 과잉 트리거. 창고 85%는 storage.py(95%)보다 먼저 제한적으로 흡수한다.
MIN_METAL_SURPLUS = 20_000
METAL_TO_CRYSTAL_SURPLUS_RATIO = 2.5
METAL_CAP_TRIGGER = 0.85

# 한 틱에 잉여의 일부만 사용한다. 큐가 무제한이어도 큰 주문을 만들지 않는다.
MAX_SURPLUS_SPEND_FRACTION = 0.25
MAX_CARGO_BATCH = 4
MAX_DEFENSE_BATCH = 5

# 최소 방어까지만 우선하고, 그 뒤에도 낮은 상한까지만 금속 overflow를 흡수한다.
MIN_ROCKET_TARGET = 5
MAX_ROCKET_TARGET = 20

# 기존 miner_bot의 틱당 큐잉 정책과 같은 안전 깊이. 누적 큐가 더 깊으면 미래 비용이
# 불명확하므로 sink가 자원을 쓰지 않는다. unit queue는 함선/방어 공용이라 반드시 0.
MAX_BUILD_QUEUE_DEPTH = 3
MAX_RESEARCH_QUEUE_DEPTH = 2

# 행성 수에 비례한 공용 수송 인프라 목표. explorer 기본 목표(10)와 비슷하되 12기 상한.
MIN_LARGE_CARGO_TARGET = 8
LARGE_CARGO_PER_PLANET = 4
MAX_LARGE_CARGO_TARGET = 12

_RESOURCE_KEYS = ("metal", "crystal", "deuterium")


@dataclass(frozen=True)
class MetalSinkDecision:
    """순수 planner 결과.

    ``kind``는 ``hold``/``ship``/``defense`` 중 하나다. action인 경우 ``technology_id``와
    ``amount``가 채워지고, ``spend_*``는 주문 전체의 정확한 비용이다. 호출부는 처음에는
    ``format_decision``만 로그하고, 충분히 관측한 뒤 ``ship``은
    ``client.build_ships``, ``defense``는 ``turtle.build_defense``로 실행할 수 있다.
    """

    kind: Literal["hold", "ship", "defense"]
    reason: str
    technology_id: int | None = None
    amount: int = 0
    spend_metal: int = 0
    spend_crystal: int = 0
    spend_deuterium: int = 0
    metal_surplus: int = 0
    crystal_surplus: int = 0
    deuterium_surplus: int = 0

    @property
    def action(self) -> tuple[str, int, int] | None:
        """통합용 행동 튜플. hold면 None."""
        if self.kind == "hold" or self.technology_id is None or self.amount < 1:
            return None
        return self.kind, self.technology_id, self.amount

    @property
    def spend(self) -> dict[str, int]:
        """주문 총비용을 자원 dict로 반환(호출자가 바꿔도 frozen 결과는 불변)."""
        return {
            "metal": self.spend_metal,
            "crystal": self.spend_crystal,
            "deuterium": self.spend_deuterium,
        }


def _hold(
    reason: str,
    surplus: tuple[int, int, int] = (0, 0, 0),
) -> MetalSinkDecision:
    return MetalSinkDecision(
        "hold", reason,
        metal_surplus=surplus[0],
        crystal_surplus=surplus[1],
        deuterium_surplus=surplus[2],
    )


def _decision(
    kind: Literal["ship", "defense"],
    reason: str,
    technology_id: int,
    amount: int,
    surplus: tuple[int, int, int],
) -> MetalSinkDecision:
    m, c, d = UNIT_COST[technology_id]
    return MetalSinkDecision(
        kind,
        reason,
        technology_id,
        amount,
        m * amount,
        c * amount,
        d * amount,
        surplus[0],
        surplus[1],
        surplus[2],
    )


def _read_nonnegative(mapping: Mapping, keys: tuple[str, ...]) -> dict[str, int] | None:
    """필수 자원 키를 정수로 읽는다. 누락/음수/비수치면 fail-closed(None)."""
    if not isinstance(mapping, Mapping):
        return None
    out: dict[str, int] = {}
    for key in keys:
        if key not in mapping:
            return None
        try:
            value = int(mapping[key])
        except (TypeError, ValueError, OverflowError):
            return None
        if value < 0:
            return None
        out[key] = value
    return out


def _safe_reserve(reserve: Mapping[str, int]) -> dict[str, int]:
    """호출부의 성장 약정에 25% 여유와 절대 하한을 적용한다."""
    return {
        key: max(MIN_RESERVE[key], int(math.ceil(reserve[key] * RESERVE_MULTIPLIER)))
        for key in _RESOURCE_KEYS
    }


def _affordable(
    technology_id: int,
    metal_budget: int,
    crystal_budget: int,
    deuterium_budget: int,
) -> int:
    """각 자원 budget 안에서 살 수 있는 최대 수량."""
    limits: list[int] = []
    for cost, budget in zip(
        UNIT_COST[technology_id],
        (metal_budget, crystal_budget, deuterium_budget),
    ):
        if cost > 0:
            limits.append(max(0, budget) // cost)
    return min(limits) if limits else 0


def _large_cargo_target(planet_count: int) -> int:
    return min(
        MAX_LARGE_CARGO_TARGET,
        max(MIN_LARGE_CARGO_TARGET, planet_count * LARGE_CARGO_PER_PLANET),
    )


def _metal_is_overabundant(
    resource_state: Mapping[str, int],
    surplus: tuple[int, int, int],
) -> bool:
    sm, sc, _sd = surplus
    if sm < MIN_METAL_SURPLUS:
        return False

    ratio_trigger = sm >= METAL_TO_CRYSTAL_SURPLUS_RATIO * max(sc, 1)
    try:
        metal_cap = int(resource_state.get("metal_max") or 0)
    except (TypeError, ValueError, OverflowError):
        metal_cap = 0
    capacity_trigger = metal_cap > 0 and int(resource_state["metal"]) >= metal_cap * METAL_CAP_TRIGGER
    return bool(ratio_trigger or capacity_trigger)


def _plan_rocket(
    levels: Mapping[int, int],
    current: int,
    target: int,
    metal_budget: int,
    surplus: tuple[int, int, int],
    reason: str,
) -> MetalSinkDecision | None:
    if current >= target or not _prereqs_met_safe(ROCKET_LAUNCHER, levels):
        return None
    amount = min(
        target - current,
        MAX_DEFENSE_BATCH,
        _affordable(ROCKET_LAUNCHER, metal_budget, 0, 0),
    )
    if amount < 1:
        return None
    return _decision("defense", reason, ROCKET_LAUNCHER, amount, surplus)


def _prereqs_met_safe(technology_id: int, levels: Mapping[int, int]) -> bool:
    """기존 선행조건 판정을 재사용하되 불완전 스냅샷은 실행 대신 False로 닫는다."""
    if not isinstance(levels, Mapping):
        return False
    try:
        return prereqs_met(technology_id, levels)
    except (TypeError, ValueError, AttributeError):
        return False


def plan_metal_sink(
    levels: Mapping[int, int],
    resource_state: Mapping[str, int],
    reserve: Mapping[str, int] | None,
    *,
    personality_key: str,
    energy: int | None,
    on_homeworld: bool,
    colony_action_pending: bool,
    building_queue_depth: int | None,
    research_queue_depth: int | None,
    unit_queue_depth: int | None,
    fleet_slots_free: int | None,
    safe_fleetsave_target: bool,
    parked_savable_value: int | None,
    fleetsave_min_value: int | None,
    total_large_cargo: int | None,
    planet_count: int,
) -> MetalSinkDecision:
    """현재 스냅샷으로 금속 sink 행동 한 건을 계획한다.

    모든 외부 상태는 호출부가 명시한다. 특히 ``total_large_cargo``는 현재 행성 수량이
    아니라 **모든 행성 + 비행 중 + 미처리 unit queue**를 합친 총량이어야 한다.
    ``parked_savable_value``는 sink 주문이 완성되는 행성의 플릿세이브 가능 함대 가치,
    ``fleetsave_min_value``는 성격별 실제 임계다. 둘 중 하나라도 모르면 대형 화물선 분기만
    fail-closed하며, 고정 방어인 로켓런처는 여전히 계획할 수 있다.

    ``reserve`` 계약: 다음 건물·연구·식민선/식민 시드·예상 연료 등 아직 현재 자원에서
    차감되지 않은 약정의 자원별 합. 이미 차감된 unit queue 비용은 다시 넣지 않는다.
    """
    if personality_key not in PERSONALITIES:
        return _hold("personality-unknown")
    if personality_key == "turtle":
        return _hold("turtle-owns-defense-sink")
    if not on_homeworld:
        return _hold("colony-scope-disabled")
    if colony_action_pending:
        return _hold("colony-action-reserved")
    if energy is None or not isinstance(energy, int):
        return _hold("energy-unknown")
    if energy < 0:
        return _hold("energy-deficit")

    # 큐 상태가 없거나 비정상이면 실행하지 않는다. unit queue는 함선/방어 공용이다.
    queue_values = (building_queue_depth, research_queue_depth, unit_queue_depth)
    if any(v is None or not isinstance(v, int) or v < 0 for v in queue_values):
        return _hold("queue-state-unknown")
    assert building_queue_depth is not None
    assert research_queue_depth is not None
    assert unit_queue_depth is not None
    if unit_queue_depth > 0:
        return _hold("unit-queue-busy")
    if building_queue_depth > MAX_BUILD_QUEUE_DEPTH:
        return _hold("building-queue-pressure")
    if research_queue_depth > MAX_RESEARCH_QUEUE_DEPTH:
        return _hold("research-queue-pressure")

    resources = _read_nonnegative(resource_state, _RESOURCE_KEYS)
    reserves = _read_nonnegative(reserve, _RESOURCE_KEYS) if reserve is not None else None
    if resources is None:
        return _hold("resource-state-invalid")
    if reserves is None:
        return _hold("reserve-unknown")

    protected = _safe_reserve(reserves)
    surplus = tuple(max(0, resources[k] - protected[k]) for k in _RESOURCE_KEYS)
    if not _metal_is_overabundant(resource_state, surplus):
        return _hold("metal-not-overabundant", surplus)

    sm, sc, sd = surplus
    # 잉여의 25%만 이번 틱 예산으로 개방한다. 최소 방어도 이 예산 밖으로 나가지 않는다.
    sink_m = int(sm * MAX_SURPLUS_SPEND_FRACTION)
    sink_c = int(sc * MAX_SURPLUS_SPEND_FRACTION)
    sink_d = int(sd * MAX_SURPLUS_SPEND_FRACTION)

    try:
        rocket_count = max(0, int(levels.get(ROCKET_LAUNCHER, 0)))
    except (TypeError, ValueError, OverflowError):
        return _hold("unit-count-invalid", surplus)

    # explorer는 post_actions에서 기존 turtle 방어를 별도로 실행하므로 중복 방어 금지.
    allow_defense = personality_key != "explorer"
    if allow_defense:
        minimum = _plan_rocket(
            levels, rocket_count, MIN_ROCKET_TARGET, sink_m, surplus, "minimum-defense",
        )
        if minimum is not None:
            return minimum

    # 유용한 수송 인프라. 안전 정보가 완전할 때만 이 분기를 연다.
    cargo_inputs = (
        fleet_slots_free,
        parked_savable_value,
        fleetsave_min_value,
        total_large_cargo,
        planet_count,
    )
    cargo_state_known = all(isinstance(v, int) and v >= 0 for v in cargo_inputs)
    if (
        safe_fleetsave_target
        and cargo_state_known
        and fleet_slots_free is not None and fleet_slots_free >= 1
        and planet_count >= 1
        and _prereqs_met_safe(LARGE_CARGO, levels)
    ):
        assert parked_savable_value is not None
        assert fleetsave_min_value is not None
        assert total_large_cargo is not None
        target = _large_cargo_target(max(1, planet_count))
        need = max(0, target - total_large_cargo)
        affordable = _affordable(LARGE_CARGO, sink_m, sink_c, sink_d)

        # 완성 직후 성격별 세이브 임계를 한 번에 넘을 최소 배치. 임계 미달 상태로 주기장에
        # 새 화물선을 쌓지 않는다. 이미 임계를 넘었으면 1기부터 허용한다.
        threshold_gap = max(0, fleetsave_min_value - parked_savable_value)
        minimum_safe_batch = max(1, int(math.ceil(threshold_gap / LARGE_CARGO_VALUE)))
        amount = min(need, affordable, MAX_CARGO_BATCH)
        if amount >= minimum_safe_batch:
            return _decision("ship", "transport-capacity", LARGE_CARGO, amount, surplus)

    # 안전한 화물선 분기가 닫혔어도 고정 방어는 슬롯을 쓰지 않는다. 다만 총 20기 상한.
    if allow_defense:
        overflow_defense = _plan_rocket(
            levels, rocket_count, MAX_ROCKET_TARGET, sink_m, surplus, "bounded-metal-defense",
        )
        if overflow_defense is not None:
            return overflow_defense

    return _hold("no-safe-sink-candidate", surplus)


def format_decision(decision: MetalSinkDecision) -> str:
    """드라이런/로그용 한 줄. 이 함수도 부작용이 없다."""
    if decision.action is None:
        return (
            f"metal-sink HOLD {decision.reason} "
            f"surplus={decision.metal_surplus}/{decision.crystal_surplus}/{decision.deuterium_surplus}"
        )
    return (
        f"metal-sink PLAN {decision.kind} {go.name_of(decision.technology_id or 0)}"
        f"({decision.technology_id})x{decision.amount} reason={decision.reason} "
        f"spend={decision.spend_metal}/{decision.spend_crystal}/{decision.spend_deuterium} "
        f"surplus={decision.metal_surplus}/{decision.crystal_surplus}/{decision.deuterium_surplus}"
    )
