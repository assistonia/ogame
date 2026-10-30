"""에너지 관리 규칙 — 선제(예방) + 태양광 위성 경로.

## 배경 (실측)
OGameX 는 광산 생산을 floor(에너지생산/에너지소비 × 100)% 로 깎는다
(app/Services/PlanetService.php:2188 getResourceProductionFactor). 광산을 올릴수록
소비가 복리로 늘어 에너지 적자가 나면 전 광산의 생산 팩터가 함께 떨어진다(실측:
100/105 행성 적자, 평균 팩터 67%). 기존 규칙(personality.plan_primary)은 "에너지<0 →
태양광 강제 1순위"라는 **사후 대응**뿐이라, 광산 업글로 적자가 난 다음에야 반응한다.

이 모듈은 두 가지를 더한다.
  1) 선제(예방): 이번 틱 예정된 광산 업글 이후의 에너지 수지를 미리 계산해서, 음수로
     전환되면 아직 적자가 아니어도 태양광(또는 위성)을 먼저 후보로 올린다.
  2) 태양광 위성 경로(중기): 태양광 발전소 다음 레벨 비용이 비싸지는 교차점부터,
     같은 에너지를 더 싸게 주는 태양광 위성(함선 212) 건조로 전환한다. 교차점은
     하드코딩하지 않고 실제 비용·에너지 공식으로 매 판단 시 계산한다.

## 태양광 vs 위성 (어느 큐에 들어가나)
  - 태양광 발전소(4): 건물 큐 → primary(personality.plan_primary)가 처리.
  - 태양광 위성(212): 조선소 큐(함선) → secondary(personality.plan_secondary)가 처리.
둘은 서버에서 별개 큐라 병행 가능하다. 이 모듈은 "이번 틱 에너지 개입이 필요한가,
필요하면 태양광이냐 위성이냐, 위성이면 몇 기냐"를 EnergyPlan 으로 돌려주고, 실제
큐잉은 personality.plan_primary/plan_secondary 가 한다.

## allow_satellite 스위치 (하위호환)
plan_primary 는 규칙봇(miner_bot)과 AI(policy_engine) 양쪽이 호출한다. policy_engine 은
plan_secondary 에 에너지 정보를 넘기지 않으므로 위성을 건조할 경로가 없다. 그래서
allow_satellite=False(기본) 면 위성을 절대 고르지 않고 항상 태양광(건물)으로만 처리한다
— policy_engine 경로는 기존 "에너지<0 → 태양광" 동작 + 선제 예방만 얻고, 위성 경로는
allow_satellite=True 를 주는 miner_bot 경로에서만 활성화된다.
"""
from __future__ import annotations

import math
from typing import NamedTuple

from bots.shared import game_objects as go

# 광산 technologyId (에너지 소비 대상). 예방 규칙은 이 중 이번 틱 예정 업글을 본다.
MINE_IDS: tuple[int, int, int] = (
    go.METAL_MINE,
    go.CRYSTAL_MINE,
    go.DEUTERIUM_SYNTHESIZER,
)

# 위성 배치 상한: 한 틱에 큐잉할 태양광 위성 최대 기수. 서버가 자원 한도로 다시 깎으므로
# (UnitQueueService::add 가 max_build_amount 로 clamp) 경제 폭주 방지용 소프트 상한이다.
SAT_MAX_BATCH = 8


class EnergyPlan(NamedTuple):
    """이번 틱 에너지 개입 계획.

    need              : 개입 필요 여부(현재 적자이거나, 예정 광산 업글로 적자 전환).
    use_satellite     : True 면 위성(secondary), False 면 태양광 발전소(primary).
    solar_action      : 태양광이 선택됐을 때 ("build", 4). 아니면 None.
    satellite_action  : 위성이 선택됐을 때 ("ship", 212, N). 아니면 None.
    deficit           : 커버해야 할 적자 규모(양수, 로그/배치 산정용).
    """
    need: bool
    use_satellite: bool
    solar_action: tuple | None
    satellite_action: tuple | None
    deficit: int


_NO_NEED = EnergyPlan(False, False, None, None, 0)


def _planned_mine(planned_primary: list[tuple]) -> int | None:
    """이번 틱 primary 후보 중 가장 우선인 광산 업글의 technologyId. 없으면 None.

    planned_primary 는 plan_primary 가 계산한 후보 목록(("build"/"facility", tid) …).
    앞에서부터 처음 만나는 광산 build 가 이번 틱 실제로 시도될 광산 업글이다.
    """
    for action in planned_primary:
        if action[0] == "build" and action[1] in MINE_IDS:
            return action[1]
    return None


def plan_energy(
    levels: dict[int, int],
    energy: int | None,
    resource_state: dict | None,
    planned_primary: list[tuple],
    allow_satellite: bool = True,
) -> EnergyPlan:
    """이번 틱 에너지 개입(태양광/위성)을 결정해 EnergyPlan 으로 반환.

    필요 판정:
      - 반응: 현재 energy < 0.
      - 선제: 이번 틱 예정 광산 업글(planned_primary 의 첫 광산)을 반영한 예상 에너지 < 0.
    둘 중 하나라도면 need=True, deficit = 더 깊은 쪽의 |음수|.

    태양광 vs 위성(교차점): 태양광 다음 레벨의 (metal+crystal+deut) 비용 합계가, 그 레벨이
    주는 에너지 증가분과 같은 에너지를 내는 위성 N기의 비용 합계보다 크면 위성으로 전환한다
    (N = ceil(태양광증가분 / 위성1기생산)). 위성은 조선소(21)≥1 이고 위성 1기 생산량>0
    (행성이 너무 춥지 않음)일 때만 후보다. allow_satellite=False 면 항상 태양광.

    energy 를 모르면(None) 개입하지 않는다(예상 계산 불가).
    """
    if energy is None:
        return _NO_NEED

    # 1) 필요 판정 (반응 + 선제)
    projected = energy
    mine = _planned_mine(planned_primary)
    if mine is not None:
        lv = levels.get(mine, 0)
        delta_c = go.mine_energy_consumption(mine, lv + 1) - go.mine_energy_consumption(mine, lv)
        projected = energy - delta_c

    worst = min(energy, projected)
    if worst >= 0:
        return _NO_NEED
    deficit = -worst

    # 2) 태양광 다음 레벨의 비용·에너지 증가분
    solar_lv = levels.get(go.SOLAR_PLANT, 0)
    solar_gain = go.solar_plant_energy(solar_lv + 1) - go.solar_plant_energy(solar_lv)
    solar_cost = go.object_cost(go.SOLAR_PLANT, solar_lv + 1)
    solar_cost_sum = sum(solar_cost.values()) if solar_cost else math.inf

    # 3) 위성 교차점 판정
    use_sat = False
    sat_each = 0
    if allow_satellite and resource_state and levels.get(go.SHIPYARD, 0) >= 1:
        temp_max = resource_state.get("temp_max")
        if temp_max is not None:
            sat_each = go.solar_satellite_energy_each(temp_max)
            sat_price = go.ship_cost(go.SOLAR_SATELLITE)
            if sat_each > 0 and solar_gain > 0 and sat_price is not None:
                sat_cost_sum = sum(sat_price.values())
                n_equiv = math.ceil(solar_gain / sat_each)  # 태양광 증가분과 동일 에너지를 낼 위성 수
                if n_equiv * sat_cost_sum < solar_cost_sum:
                    use_sat = True

    if use_sat:
        batch = max(1, min(math.ceil(deficit / sat_each), SAT_MAX_BATCH))
        return EnergyPlan(True, True, None, ("ship", go.SOLAR_SATELLITE, batch), deficit)

    return EnergyPlan(True, False, ("build", go.SOLAR_PLANT), None, deficit)
