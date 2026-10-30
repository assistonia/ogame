"""탐험가(explorer) 규칙봇의 원정(Expedition, 미션타입 15) 행동.

miner_bot.tick() 이 자원/시설/연구/함선 건설을 마친 뒤, explorer 성격 봇에 한해
`expedition_tick()` 을 호출해 **한 틱에 원정 1건**을 (조건 충족 시) 파견한다. 나머지
성격 봇은 이 모듈을 전혀 타지 않는다.

## 왜 (배경 — docs/CURRICULUM_AUDIT.md §D)
explorer 12명의 정체성(원정 파밍)이 통째로 미구현이었다: 원정 미션 전 기간 0건. 커리큘럼은
이미 파스파인더(219)와 선행연구(하이퍼2·조선소5·연소6·실드4)를 목표로 두고 있어 실제로
파스파인더가 건조되고 있으나(실측: 다수 explorer 가 파스파인더 1~2기 보유), **함대를
원정으로 내보내는 루틴만 없었다**. explorer 는 방어 0·소비처 0 로 자원만 쌓아 피격 1위였다.
이 모듈이 그 빠진 루틴 하나를 채운다(커리큘럼 자체는 이미 원정을 준비시키므로 무변경).

## 서버 원정 사양 (ogamex 소스 실측 — FULLY FUNCTIONAL 로 확인됨)
  - 미션타입 15(ExpeditionMission). 반환 미션 있음(함대가 되돌아온다). 도착+대기시간 경과 후
    결과를 굴려(자원/함선/암흑물질 획득, 해적·외계인 전투, 지연/가속, 공허, 블랙홀 손실 등)
    **실제로 계정에 지급**한다. GainItems 만 스텁(가중치에도 없어 발생 안 함).
  - 대상 좌표: 자기 (galaxy, system, **16**) — position 16 은 원정 전용 슬롯(빈 우주).
  - 선행: astrophysics ≥ 1. 대기시간(holdingtime) 은 1..astro 시간(정수).
  - 원정 슬롯 상한 = floor(sqrt(astro)) + Discoverer(+2). 일반 함대 슬롯(1+computer)도 함께 쓴다.
  - Discoverer 클래스(explorer 전원 배포): 원정 획득 +50%, 전투 조우 확률 -50%, 슬롯 +2.

## 안전장치
  - expedition_tick 은 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호).
  - 함대 전량 원정 금지: 위험예산과 남은 슬롯 수로 균등 분할하고, 파스파인더는 슬롯당 1척.
  - 원정 슬롯 초과 파견 금지: ``processed=0``인 실제 진행 중 원정 수만 세고, 슬롯 상한
    미만일 때만 새로 보낸다(과거 완료 행 때문에 영구 3/3이 되는 회귀 방지).
  - 원정 함대를 못 꾸리면(초기·함대 빈약) None 을 반환 → 호출부가 식민 등으로 폴백.
"""
from __future__ import annotations

import math

from bots.shared import fleet
from bots.shared import db_reader
from bots.shared import planets as planets_mod
from bots.shared.game_objects import (
    ASTROPHYSICS,       # 124
    SMALL_CARGO,        # 202
    LIGHT_FIGHTER,      # 204
)

# game_objects 에 개별 상수가 없는 함선 id (SHIPS 매핑엔 있음). fleet.SHIP_* 와 동일.
LARGE_CARGO = 203
HEAVY_FIGHTER = 205
PATHFINDER = 219

# 원정 전용 슬롯(빈 우주). ogamex UniverseConstants::EXPEDITION_POSITION.
EXPEDITION_POSITION = 16

# 대기시간(시간). 1..astro 범위 하한. 짧게 잡아 회전율을 높인다.
HOLDING_HOURS = 1

# Discoverer 클래스가 주는 원정 슬롯 보너스(explorer 전원 배포). 서버 getExpeditionSlotsBonus.
DISCOVERER_EXP_SLOTS = 2

# OGameX CharacterClassService의 Discoverer ID. 규칙 탐험가는 보통 3이지만 AI 계정 등
# 클래스가 아직 배정되지 않은 경우가 있으므로 실제 users.character_class를 확인한다.
DISCOVERER_CLASS_ID = 3
GENERAL_CLASS_ID = 2
GENERAL_FLEET_SLOTS = 2

# 진행 중 원정 수가 이 소프트 상한 이상이면 이번 틱은 새로 안 보낸다(과요청 방지).
# 실제 상한은 min(이 값, 서버 슬롯). 배포는 팀리드가 하므로 폭주 방지용 보수적 상한.
SOFT_MAX_EXPEDITIONS = 3

# 구 외부 참조 호환 상수. 실제 상한은 risk_fraction과 남은 슬롯 균등분배가 정한다.
MAX_FLEET_FRACTION = 0.5

# 레거시 최소 편성. 실제 파견은 아래 build_expedition_fleet()이 보유 화물과 남은
# 원정 슬롯 수를 보고 적응적으로 정한다. 외부 코드/관측 지표 호환을 위해 이름은 유지한다.
EXP_TEMPLATE: dict[int, int] = {
    LARGE_CARGO: 3,     # 203 대형화물 — 획득 자원 적재 주력
    SMALL_CARGO: 2,     # 202 소형화물 — 보조 적재
    LIGHT_FIGHTER: 5,   # 204 전투기   — 전투 조우 호위
}

# 파스파인더는 원정 특화 함선(획득·전투 보너스). 있으면 정예 소수만(비율 미적용).
PATHFINDER_PER_EXP = 1
AGGRESSIVE_FLEET_VALUE_FRACTION = 0.35
AGGRESSIVE_TOTAL_EXPOSURE_FRACTION = 0.70
CARGO_CAPACITY = {SMALL_CARGO: 5_000, LARGE_CARGO: 25_000}
COMBAT_TIER_ORDER = (218, 213, 211, 215, 207, 206, HEAVY_FIGHTER, LIGHT_FIGHTER)
SHIP_VALUE = {202: 4000, 203: 12000, 204: 4000, 205: 10000, 206: 29000,
              207: 60000, 211: 90000, 213: 125000, 215: 70000, 218: 160000,
              PATHFINDER: 31000}


def expedition_fleet_value(ships: dict[int, int]) -> int:
    return sum(SHIP_VALUE.get(tid, 0) * max(0, int(amount)) for tid, amount in ships.items())


def expedition_cargo_capacity(ships: dict[int, int]) -> int:
    return sum(CARGO_CAPACITY.get(tid, 0) * max(0, int(amount))
               for tid, amount in ships.items())

# 원정을 보낼 최소 함대 규모(총 함선 수). 이보다 적으면 원정 가치가 없어 폴백.
MIN_EXP_SHIPS = 3

# 하위호환용 최소 식별자 재고. 실제 플릿세이브 예약량은 아래
# expedition_identity_reserve()가 현재 화물선·원정 슬롯에 맞춰 동적으로 계산한다.
EXPEDITION_IDENTITY_RESERVE: dict[int, int] = {
    LARGE_CARGO: 4,
    SMALL_CARGO: 2,
    LIGHT_FIGHTER: 2,
    PATHFINDER: 1,
}

# 피격 1위 보완용: explorer 최소 방어 비율(잉여의 이 비율만 방어로). miner_bot 훅이
# turtle.defense_tick 에 넘겨 쓴다(원정과 별개 큐라 병행). 낮게 잡아 경제를 거의 안 침범.
DEFENSE_RATIO = 0.1


def expedition_slots(
    levels: dict[int, int], discoverer_bonus: int = DISCOVERER_EXP_SLOTS
) -> int:
    """이 계정의 원정 슬롯 상한 = floor(sqrt(astro)) + Discoverer(+2).

    서버 PlayerService::getExpeditionSlotsMax 복제(추가 setting 보너스는 0 가정).
    astro=0 이면 원정 자체가 불가(호출부가 astro≥1 을 먼저 확인).
    """
    astro = levels.get(ASTROPHYSICS, 0)
    return int(math.isqrt(max(0, astro))) + max(0, int(discoverer_bonus))


def expedition_identity_reserve(levels: dict[int, int]) -> dict[int, int]:
    """원정 회전용으로 플릿세이브에서 제외할 주기장 함대.

    화물선은 원정 보상의 직접 상한이므로 현재 행성의 수량 전체를 예약한다.
    원정 후 남은 화물선을 다른 식민지로 세이브하면 다음 빈 슬롯이 다시
    소형 편성으로 굳어지기 때문이다. PF는 슬롯당 1기, PF가 부족한
    초기 계정은 나머지 슬롯에 호위 1기씩을 예약한다.
    """
    reserve: dict[int, int] = {}
    for tid in (LARGE_CARGO, SMALL_CARGO):
        have = max(0, int(levels.get(tid, 0)))
        if have:
            reserve[tid] = have

    slots = max(1, expedition_slots(levels))
    pathfinders = min(max(0, int(levels.get(PATHFINDER, 0))), slots)
    if pathfinders:
        reserve[PATHFINDER] = pathfinders

    escorts_needed = max(0, slots - pathfinders)
    for tid in COMBAT_TIER_ORDER:
        if escorts_needed <= 0:
            break
        keep = min(max(0, int(levels.get(tid, 0))), escorts_needed)
        if keep:
            reserve[tid] = keep
            escorts_needed -= keep
    return reserve


def expedition_fleet_ready(
    ships: dict[int, int], *, minimum_ships: int = MIN_EXP_SHIPS
) -> bool:
    """원정 최소 조건: 3척 이상이며 자원을 실을 화물선이 하나 이상.

    서버는 비정찰 함선 한 척만 있어도 원정을 허용한다. PF를 건조할 수 없는
    General/Collector도 화물선만으로 폴백할 수 있어야 하므로 전투함/PF는 필수가 아니다.
    """
    total = sum(max(0, int(n)) for n in ships.values())
    cargo = ships.get(LARGE_CARGO, 0) + ships.get(SMALL_CARGO, 0)
    return total >= max(1, int(minimum_ships)) and cargo > 0


def build_expedition_fleet(
    levels: dict[int, int], *, aggressive: bool = False, slots_to_preserve: int = 0,
    risk_fraction: float = AGGRESSIVE_FLEET_VALUE_FRACTION,
    value_budget: int | None = None,
    minimum_ships: int = MIN_EXP_SHIPS,
) -> dict[int, int]:
    """이번 원정에 태울 함대 {ship_id: amount} 를 보유량 기준으로 구성.

    ``slots_to_preserve``는 이번 건 뒤에도 채워야 할 원정 슬롯 수다. 현재 주기장 함대와
    남은 총 노출예산을 ``slots_to_preserve + 1``로 나눠 첫 원정이 화물을 독식하지 않게
    한다. aggressive 여부와 무관하게 같은 적응 편성을 쓰며 차이는 호출부의 위험비율뿐이다.

    - PF는 있으면 정확히 1척. 보상 배수는 존재 여부만 보므로 추가 PF는 싣지 않는다.
    - PF가 없으면 General/Collector도 정상 폴백한다. 보유 전투함은 최대 1척만 호위로 쓰고,
      전투함이 없어도 서버가 허용하는 화물 원정은 막지 않는다.
    - 나머지 예산은 비용 대비 적재효율이 높은 대형화물, 소형화물 순으로 채운다.
    """
    ships: dict[int, int] = {}
    required_ships = max(1, int(minimum_ships))
    remaining_launches = max(1, int(slots_to_preserve) + 1)
    available_value = expedition_fleet_value(levels)
    configured_risk = min(1.0, max(0.0, float(risk_fraction)))
    risk_cap = int(available_value * configured_risk)

    # 실제 원정에 쓸 후보만 균등분배의 분모로 삼는다. PF가 있는데 보내지 않을 수백 척의
    # 전투함까지 포함하면 앞 슬롯의 화물예산이 부풀고 마지막 슬롯만 작아진다.
    dispatchable_value = (
        max(0, int(levels.get(LARGE_CARGO, 0))) * SHIP_VALUE[LARGE_CARGO]
        + max(0, int(levels.get(SMALL_CARGO, 0))) * SHIP_VALUE[SMALL_CARGO]
    )
    support_slots = remaining_launches
    pathfinders = min(max(0, int(levels.get(PATHFINDER, 0))), support_slots)
    dispatchable_value += pathfinders * SHIP_VALUE[PATHFINDER]
    support_slots -= pathfinders
    for tid in COMBAT_TIER_ORDER:
        if support_slots <= 0:
            break
        escorts = min(max(0, int(levels.get(tid, 0))), support_slots)
        dispatchable_value += escorts * SHIP_VALUE[tid]
        support_slots -= escorts

    fair_fleet_cap = dispatchable_value // remaining_launches
    budget = min(risk_cap, fair_fleet_cap)
    if value_budget is not None:
        budget = min(budget, max(0, int(value_budget)) // remaining_launches)

    def minimum_cargo_value(amount: int) -> int | None:
        """현재 화물선으로 ``amount``척을 꾸리는 최소 건조가치."""
        needed = max(0, int(amount))
        total = 0
        for tid in (SMALL_CARGO, LARGE_CARGO):
            take = min(max(0, int(levels.get(tid, 0))), needed)
            total += take * SHIP_VALUE[tid]
            needed -= take
        return total if needed == 0 else None

    # PF 보너스는 존재 여부만 본다. 미래 슬롯용 수량을 별도로 잠그지 않아, 가진 PF를
    # 현재부터 한 척씩 배분하면 N척/N슬롯일 때 자연스럽게 모든 슬롯에 정확히 한 척씩 간다.
    two_cargo_value = minimum_cargo_value(max(0, required_ships - 1))
    if (
        levels.get(PATHFINDER, 0) > 0
        and two_cargo_value is not None
        and SHIP_VALUE[PATHFINDER] + two_cargo_value <= budget
    ):
        ships[PATHFINDER] = PATHFINDER_PER_EXP
        budget -= SHIP_VALUE[PATHFINDER]

    # PF가 호위를 겸하면 별도 전투함은 보내지 않는다. PF가 없는 클래스만 보유 전투함 중
    # 한 척을 붙이며, 함대가 커져도 호위 수량을 비례 증대하지 않는다.
    if PATHFINDER not in ships:
        for tid in COMBAT_TIER_ORDER:
            if (
                levels.get(tid, 0) > 0
                and two_cargo_value is not None
                and SHIP_VALUE.get(tid, 0) + two_cargo_value <= budget
            ):
                ships[tid] = 1
                budget -= SHIP_VALUE[tid]
                break

    # 지원함을 포함해 최소 3척이 되도록 싼 화물선부터 먼저 확보한다. 그 뒤에는 적재/비용
    # 효율이 높은 대형화물을 우선해 남은 예산을 채운다.
    minimum_cargo_ships = max(0, required_ships - sum(ships.values()))
    for tid in (SMALL_CARGO, LARGE_CARGO):
        if minimum_cargo_ships <= 0:
            break
        amount = min(
            max(0, int(levels.get(tid, 0))),
            minimum_cargo_ships,
            max(0, budget // SHIP_VALUE[tid]),
        )
        if amount:
            ships[tid] = ships.get(tid, 0) + amount
            budget -= amount * SHIP_VALUE[tid]
            minimum_cargo_ships -= amount

    # 고정 적재 상한은 두지 않는다. 현재 주기장에 있는 화물과 선택한 위험예산을 남은
    # 원정 슬롯에 균등 분배하며, 제국이 화물선을 더 만들면 편성도 그대로 계속 커진다.
    for tid in (LARGE_CARGO, SMALL_CARGO):
        available = max(0, int(levels.get(tid, 0))) - ships.get(tid, 0)
        amount = min(available, max(0, budget // SHIP_VALUE[tid]))
        if amount:
            ships[tid] = ships.get(tid, 0) + amount
            budget -= amount * SHIP_VALUE[tid]

    return ships


_MISSION_SHIP_COLUMNS = {
    202: "small_cargo", 203: "large_cargo", 204: "light_fighter",
    205: "heavy_fighter", 206: "cruiser", 207: "battle_ship",
    211: "bomber", 213: "destroyer", 215: "battlecruiser",
    218: "reaper", 219: "pathfinder",
}


def _mission_state(email: str) -> tuple[int, int, int] | None:
    """(진행 원정 수, 전체 진행 함대 수, 진행 원정 가치). 실패는 fail-closed."""
    try:
        terms = " + ".join(
            f"COALESCE(fm.{column},0)*{SHIP_VALUE[tid]}"
            for tid, column in _MISSION_SHIP_COLUMNS.items()
        )
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(fm.mission_type=%s) expeditions, COUNT(*) active, "
                f"COALESCE(SUM(CASE WHEN fm.mission_type=%s THEN {terms} ELSE 0 END),0) value "
                "FROM fleet_missions fm JOIN users u ON u.id=fm.user_id "
                "WHERE u.email=%s AND fm.processed=0 AND fm.mission_type<>10",
                (fleet.MISSION_EXPEDITION, fleet.MISSION_EXPEDITION, email),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (int(row.get("expeditions") or 0), int(row.get("active") or 0),
                int(row.get("value") or 0))
    except Exception:  # noqa: BLE001
        return None


def _active_expeditions(client, email: str) -> int:
    """진행 중인 원정 미션 수. DB 직독(정확) 우선, 실패 시 HTTP 폴백.

    ## 왜 DB 직독인가 (실측)
    fleet_events(HTTP eventlist)는 대기(holding) 원정 하나를 **여러 행**으로 렌더한다
    (출발 → 대기해제 → 귀환의 3단계가 각각 event row). 이걸 세면 원정 1건이 3으로
    과집계돼 슬롯을 조기에 소진한다. fleet_missions 테이블은 완료 행도 보존한다. 서버
    ``FleetMissionService::getActiveFleetMissionsSentByCurrentPlayer``와 정확히 같게
    ``processed=0 AND mission_type=15``를 센다. 출발편이 완료되면 귀환편(type 15,
    parent_id 있음)이 processed=0이 되므로 parent_id로 제한하면 귀환 중 슬롯을 놓친다.
    (db_reader._conn 의 읽기전용 스레드-로컬 커넥션 재사용 — raider.read_spy_ships 와 동일 패턴.)

    DB 조회 실패 시 HTTP eventlist 의 type-15 행 수로 폴백한다(과집계 → 보수적 = 덜 보냄).
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm "
                "JOIN users u ON u.id = fm.user_id "
                "WHERE u.email = %s AND fm.processed = 0 AND fm.mission_type = %s",
                (email, fleet.MISSION_EXPEDITION),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 - DB 실패 시 HTTP 폴백(과집계 감수)
        try:
            ev = fleet.fleet_events(client)
        except Exception:  # noqa: BLE001 - 두 조회 모두 실패하면 슬롯 만원으로 간주
            return SOFT_MAX_EXPEDITIONS
        return sum(
            1 for e in ev.get("events", [])
            if e.get("mission_type") == fleet.MISSION_EXPEDITION
        )


def _discoverer_slot_bonus(email: str) -> int:
    """실제 계정 클래스에 따른 원정 슬롯 보너스. 조회 실패는 0으로 보수 처리."""
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT character_class FROM users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
        return (
            DISCOVERER_EXP_SLOTS
            if row and int(row.get("character_class") or 0) == DISCOVERER_CLASS_ID
            else 0
        )
    except Exception:  # noqa: BLE001 - 보너스를 과대평가하지 않도록 0
        return 0


def _general_fleet_slot_bonus(email: str) -> int:
    """서버 General 클래스의 일반 함대 슬롯 +2를 반환한다."""
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute("SELECT character_class FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
        return (
            GENERAL_FLEET_SLOTS
            if row and int(row.get("character_class") or 0) == GENERAL_CLASS_ID
            else 0
        )
    except Exception:  # noqa: BLE001 - 과대 슬롯 계산보다 보수 폴백
        return 0


def fleet_slots(levels: dict[int, int], email: str) -> int:
    """서버 일반 함대 슬롯 = 1 + 컴퓨터 공학 + General 보너스."""
    return (
        1
        + max(0, int(levels.get(108, 0) or 0))
        + _general_fleet_slot_bonus(email)
    )


def expedition_raid_reservation(
    email: str,
    levels: dict[int, int],
    *,
    aggressive: bool = False,
    use_server_cap: bool = False,
    risk_fraction: float = AGGRESSIVE_FLEET_VALUE_FRACTION,
    total_exposure_fraction: float = AGGRESSIVE_TOTAL_EXPOSURE_FRACTION,
) -> tuple[int, dict[int, int]]:
    """약탈보다 먼저 확보할 ``(함대 슬롯, 함선)``을 계산한다.

    원정 슬롯이 덜 찬 동안 약탈기가 일반 함대 슬롯이나 화물선을 먼저 가져가면 다음
    원정이 영구적으로 작은 편성이 된다. 현재 진행 원정과 실제 계정 함대가치를 기준으로
    남은 원정을 순차 모의편성해, *실제로 채울 수 있는 원정 수*와 그때 필요한 함선만
    예약한다. 원정 함대를 전혀 꾸릴 수 없거나 DB 상태를 모르면 예약하지 않아 다른 양수
    EV 행동까지 굶기는 일을 피한다.
    """
    if int(levels.get(ASTROPHYSICS, 0) or 0) < 1:
        return 0, {}

    state = _mission_state(email)
    if state is None:
        return 0, {}
    active, _all_active, active_value = state
    server_cap = expedition_slots(levels, _discoverer_slot_bonus(email))
    expedition_cap = (
        server_cap
        if aggressive or use_server_cap
        else min(SOFT_MAX_EXPEDITIONS, server_cap)
    )
    fleet_slot_cap = fleet_slots(levels, email)
    target_active = min(expedition_cap, fleet_slot_cap)
    launches_left = max(0, target_active - active)
    if launches_left <= 0:
        return 0, {}

    configured_risk = min(1.0, max(0.0, float(risk_fraction)))
    configured_total = min(1.0, max(0.0, float(total_exposure_fraction)))
    value_budget: int | None = None
    try:
        account_levels = db_reader.get_account_ship_totals_db(email)
    except Exception:  # noqa: BLE001 - 예약 실패는 유휴 슬롯 방지를 위해 fail-open
        if aggressive:
            return 0, {}
    else:
        total_value = expedition_fleet_value(account_levels)
        value_budget = max(0, int(total_value * configured_total) - active_value)

    available = {int(tid): max(0, int(amount)) for tid, amount in levels.items()}
    reserved: dict[int, int] = {}
    planned = 0
    minimum_ships = 1 if aggressive else MIN_EXP_SHIPS
    while planned < launches_left:
        remaining = launches_left - planned
        ships = build_expedition_fleet(
            available,
            aggressive=aggressive,
            slots_to_preserve=remaining - 1,
            risk_fraction=configured_risk,
            value_budget=value_budget,
            minimum_ships=minimum_ships,
        )
        if not expedition_fleet_ready(ships, minimum_ships=minimum_ships):
            break
        fleet_value = expedition_fleet_value(ships)
        for tid, amount in ships.items():
            available[tid] = max(0, available.get(tid, 0) - amount)
            reserved[tid] = reserved.get(tid, 0) + amount
        if value_budget is not None:
            value_budget = max(0, value_budget - fleet_value)
        planned += 1
    return planned, reserved


def _own_system(client) -> tuple[int, int] | None:
    """현재 행성의 (galaxy, system). 파싱 실패 시 None."""
    pl = planets_mod.list_planets(client)
    if not pl:
        return None
    cur = next((p for p in pl if p.get("current")), pl[0])
    return (cur["galaxy"], cur["system"])


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def expedition_tick(
    client, email: str, levels: dict[int, int], *, aggressive: bool = False,
    use_server_cap: bool = False,
    risk_fraction: float = AGGRESSIVE_FLEET_VALUE_FRACTION,
    total_exposure_fraction: float = AGGRESSIVE_TOTAL_EXPOSURE_FRACTION,
    dispatch_state: dict | None = None,
) -> str | None:
    """explorer 봇의 원정 행동 1건. 로그 문자열 또는 None(원정 안 함) 반환.

    levels 는 miner_bot 이 이미 조회한 건물/시설/연구/함선 수량 dict(함선 id 포함).
    이 함수는 어떤 경우에도 예외를 올리지 않는다(루프 보호). None 을 반환하면 호출부가
    식민 등 다른 행동으로 폴백할 수 있다.
    """
    try:
        return _expedition_tick_inner(
            client,
            email,
            levels,
            aggressive=aggressive,
            use_server_cap=use_server_cap,
            risk_fraction=risk_fraction,
            total_exposure_fraction=total_exposure_fraction,
            dispatch_state=dispatch_state,
        )
    except Exception as exc:  # noqa: BLE001 - 원정 버그가 빌드틱/루프를 깨지 않게
        return f"expedition err {type(exc).__name__}: {exc}"


def _expedition_tick_inner(
    client, email: str, levels: dict[int, int], *, aggressive: bool = False,
    use_server_cap: bool = False,
    risk_fraction: float = AGGRESSIVE_FLEET_VALUE_FRACTION,
    total_exposure_fraction: float = AGGRESSIVE_TOTAL_EXPOSURE_FRACTION,
    dispatch_state: dict | None = None,
) -> str | None:
    if dispatch_state is not None:
        dispatch_state.clear()
        dispatch_state.update({"sent": False, "ships": {}})
    # 1) 선행: 천체물리 ≥ 1(원정 언락). 조선소·연소는 함선 보유로 이미 검증됨.
    astro = levels.get(ASTROPHYSICS, 0)
    if astro < 1:
        return None  # 원정 불가 → 폴백(식민 연구가 astro 를 올릴 때까지 대기)

    # 2/3) Named aggressive profile uses the actual server cap, never the soft cap.
    server_cap = expedition_slots(levels, _discoverer_slot_bonus(email))
    slot_cap = server_cap if (aggressive or use_server_cap) else min(SOFT_MAX_EXPEDITIONS, server_cap)
    state = _mission_state(email)
    tag = "[EXP:AGGRESSIVE] " if aggressive else ""
    if state is None:
        if aggressive:
            return f"{tag}expedition idle (DB state unavailable)"
        # 구 호출부/테스트와 DB 장애 폴백: 진행 원정 수만이라도 보수적으로 복구한다.
        active_fallback = _active_expeditions(client, email)
        state = (active_fallback, active_fallback, 0)
    active, all_active, active_value = state
    fleet_slot_cap = fleet_slots(levels, email)
    if active >= slot_cap:
        return f"{tag}expedition idle (진행중 {active}/{slot_cap})"
    if all_active >= fleet_slot_cap:
        return f"{tag}expedition idle (fleet slots {all_active}/{fleet_slot_cap})"

    value_budget = None
    configured_risk = min(1.0, max(0.0, float(risk_fraction)))
    configured_total = min(1.0, max(0.0, float(total_exposure_fraction)))
    actual_risk = configured_risk
    try:
        account_levels = db_reader.get_account_ship_totals_db(email)
    except Exception:  # noqa: BLE001 - AI full autonomy는 총자산 불명 시 fail-closed
        if aggressive:
            return f"{tag}expedition idle (DB fleet value unavailable)"
    else:
        total_value = expedition_fleet_value(account_levels)
        total_cap = int(total_value * configured_total)
        value_budget = max(0, total_cap - active_value)
        remaining_launches = max(1, slot_cap - active)
        fair_budget = value_budget // remaining_launches
        actual_risk = min(
            configured_risk,
            fair_budget / total_value if total_value else 0.0,
        )
    minimum_ships = 1 if aggressive else MIN_EXP_SHIPS
    ships = build_expedition_fleet(
        levels, aggressive=aggressive, slots_to_preserve=max(0, slot_cap - active - 1),
        risk_fraction=configured_risk, value_budget=value_budget,
        minimum_ships=minimum_ships,
    )
    if not expedition_fleet_ready(ships, minimum_ships=minimum_ships):
        return None

    # 4) 대상: 자기 시스템의 원정 슬롯(g:s:16).
    own = _own_system(client)
    if own is None:
        return "expedition idle (자기 시스템 조회 실패)"
    galaxy, system = own
    target = (galaxy, system, EXPEDITION_POSITION)

    # 대기시간은 1..astro 시간. 하한 1 로 회전율 확보.
    holding = max(1, min(HOLDING_HOURS, astro))

    res = fleet.send_fleet(
        client, fleet.MISSION_EXPEDITION, target, ships,
        speed=10, holding_hours=holding,
    )
    if res.get("ok"):
        if dispatch_state is not None:
            dispatch_state.update({"sent": True, "ships": dict(ships)})
        crew = ", ".join(
            f"{_ship_name(tid)}{amt}" for tid, amt in ships.items()
        )
        return (f"{tag}expedition SEND {galaxy}:{system}:{EXPEDITION_POSITION} "
                f"({crew}, value={expedition_fleet_value(ships)}, "
                f"risk={actual_risk:.0%}, hold {holding}h, "
                f"{active + 1}/{slot_cap})")
    # check-target 거부/자원부족/슬롯초과 등 → 다음 틱 재시도(자기 교정).
    return f"expedition idle ({res.get('message', 'send failed')})"


def _ship_name(tid: int) -> str:
    from bots.rule_based.personality import tech_label  # 지연 import(순환 방지)
    return tech_label(tid)
