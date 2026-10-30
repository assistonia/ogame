"""식민지 개발(키우기) 루틴 — 다중 행성 순회 빌드.

## 왜 필요한가 (실측 배경)
봇 틱은 홈월드(현재 행성)에서만 건설한다. colony.py 는 식민지를 "따기"(colonize)만
하고 키우지 않아, 딴 식민지는 광산 0·자원 500 그대로 영구 방치된다. 조선소 Lv.4 개방으로
채굴러/하이브리드가 곧 식민을 시작하면 빈 깡통이 양산된다. 이 모듈이 그 "키우기"를 맡는다.

## 한 틱의 동작 (건강도 우선, 노후도 기반 공정 순회)
miner_bot.tick() 이 **홈월드 건설/연구/함선/확장을 모두 마친 뒤** colony_dev_tick() 을
호출한다(raid_tick/colony_tick 과 동일한 후행 훅 패턴). 한 틱에 식민지 **1개만** 다룬다:
  1. db_reader.list_planets_db 로 계정 행성 목록을 읽는다(HTTP 없이 DB 직독).
  2. 식민지(=현재 행성이 아닌 planet_type=1)가 없으면 즉시 None — 부하 거의 없음.
  3. 광산 0·생산률 저하·에너지 적자를 먼저 고르되, 계정별 상태파일에 마지막 방문
     순서를 남겨 오래 기다린 정상 식민지도 굶지 않게 식민지 1개를 고른다.
  4. switch_planet(?cp=)으로 그 식민지로 전환(서버가 users.planet_current 저장).
  5. 식민지의 레벨/자원현황을 planet_id 로 직독해 **홈월드와 같은 계획 계층**
     (personality.plan_primary + miner_bot._queue_actions)으로 저비용 빌드 1~2건.
  6. **finally 로 반드시 홈월드로 복귀** — 전환 후 예외가 나도 다음 틱 홈 작업을 지킨다.

## 빌드 오더 (초기 커리큘럼 재사용)
식민지는 초기라 상각 계산이 필요 없다. 홈월드 초기 커리큘럼을 그대로 재사용한다:
plan_primary 가
  - 에너지 개입(energy.plan_energy): 광산을 올리면 적자가 나므로 **태양광 발전소**를
    먼저 끌어온다.
  - 자원 floor: 성격별 광산 floor 까지 메탈/크리스탈/듀테륨 광산.
  - 저장고: 자원이 창고(초기 10000)에 막히면 해당 저장고.
결과적으로 "태양광 → 메탈/크리스탈 광산 → 저장고" 저비용 오더가 자연히 나온다.

## 에너지: 태양광 발전소 → 위성 전환 (조선소가 생긴 뒤, 홈월드와 동일)
초기엔 조선소가 없어 위성을 못 써 태양광 발전소로만 에너지를 댄다. 하지만 커리큘럼이
식민지에 **조선소를 세우고 나면**(실측: 식민지도 조선소 Lv4까지 성장) 태양광 발전소
다음 레벨은 자원 100만+이 들어 식민지 자체 생산으로는 영영 못 산다 — 그 사이 광산
소비만 늘어 에너지가 −2000대까지 적자로 방치되고 전 광산 생산이 60%대로 깎이는
악순환에 빠진다(홈월드는 위성 86기로 흑자인데 식민지만 방치되던 실측 버그).
그래서 조선소가 있으면(shipyard≥1) 홈월드처럼 `allow_satellite=True` 로 위성 경로를
열고, plan_secondary 가 낸 태양광 위성(212, 1기 2500 자원)을 조선소 큐로 병행 건조해
적자를 싸게 메운다. 위성이 태양광보다 싼 교차점 판정은 energy.plan_energy 가 홈월드와
같은 공식으로 한다(조선소가 없으면 종전대로 태양광 발전소만).

## 서버 요청 최소화
- 한 틱에 식민지 1개, 빌드 최대 2건(MAX_BUILD_PER_COLONY).
- 조회는 전량 DB 직독. 전환/복귀만 HTTP GET 2회. 빌드 POST 는 홈월드와 동일 비용.
- _queue_actions 의 사전 자원체크가 그대로 적용돼 못 살 것은 요청조차 안 보낸다.

## 알려진 한계 (수송 후속 태스크)
식민지는 **자체 생산만으로** 성장한다(홈월드 잉여 자원 수송은 fleet 사용이라 이번 범위
밖 — 후속 태스크로 분리). 초기 식민지는 저비용 광산/태양광이라 자체 자원(시드 500+생산)
으로 충분히 시동이 걸린다.
"""
from __future__ import annotations

import json
import math
import pathlib
from collections import Counter
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from bots.shared import db_reader
from bots.shared import game_objects as go
from bots.shared import planets as planets_mod
from bots.rule_based.personality import plan_primary, plan_secondary
from bots.rule_based import production_capacity
from bots.rule_based.tick_context import build_tick_context
from bots.ai_agents.action_catalog import action_enabled

# 한 틱에 한 식민지에 쌓을 최대 빌드 건수. 홈월드(MAX_BUILD_PER_TICK=3)보다 보수적으로
# 잡아 식민지가 홈월드 서버부하를 넘지 않게 한다(식민지는 초기라 2건이면 충분).
MAX_BUILD_PER_COLONY = 2

# 계정별 식민지 방문 상태. 기존 ``cursor``도 보존해 옛 상태/테스트와 호환하고,
# ``visit_seq``와 행성별 ``last_visited``를 건강도 스케줄러의 논리 시계로 사용한다.
STATE_DIR = pathlib.Path(__file__).resolve().parent / ".colony_dev_state"

# 정상 행성도 이 횟수 이상 기다리면 건강도보다 먼저 가장 오래된 행성을 고른다. 실제
# 임계값은 식민지 수의 2배 이상이라, 장애 식민지를 우선 복구하면서도 유한 시간 안에
# 모든 식민지가 다시 선택된다.
MIN_STARVATION_AGE = 3
STARVATION_ROUNDS = 2

# 에너지 복구 위성은 실제 부족량만큼 계산하되, 한 틱에 과도한 파괴 가능 자산을 쌓지
# 않도록 자원 위험 예산과 절대 상한을 함께 적용한다. 조선소 큐 시간 상한은 최종 실행
# 계층(miner_bot._try_first / production_capacity)이 다시 강제한다.
SATELLITE_RECOVERY_MAX_BATCH = 96
SATELLITE_RESOURCE_RISK_FRACTION = 0.50

# Every colony reaches these balanced production floors before role-specific
# ordering is allowed to dominate.  Energy safety from plan_primary always stays
# ahead of the floors, so these are targets rather than a blind fixed build order.
DEFAULT_MINIMUM_FLOORS: dict[int, int] = {
    go.METAL_MINE: 6,
    go.CRYSTAL_MINE: 5,
    go.DEUTERIUM_SYNTHESIZER: 3,
    go.SOLAR_PLANT: 6,
}

ROLE_TARGET_ORDER: dict[str, tuple[int, ...]] = {
    "balanced": (
        go.METAL_MINE,
        go.CRYSTAL_MINE,
        go.DEUTERIUM_SYNTHESIZER,
        go.SOLAR_PLANT,
    ),
    "mining": (
        go.METAL_MINE,
        go.CRYSTAL_MINE,
        go.DEUTERIUM_SYNTHESIZER,
        go.SOLAR_PLANT,
    ),
    "deuterium": (
        go.DEUTERIUM_SYNTHESIZER,
        go.METAL_MINE,
        go.CRYSTAL_MINE,
        go.SOLAR_PLANT,
    ),
    "solar": (
        go.SOLAR_PLANT,
        go.METAL_MINE,
        go.CRYSTAL_MINE,
        go.DEUTERIUM_SYNTHESIZER,
    ),
    "shipyard": (
        go.SOLAR_PLANT,
        go.METAL_MINE,
        go.CRYSTAL_MINE,
        go.DEUTERIUM_SYNTHESIZER,
        go.SHIPYARD,
    ),
    "research": (
        go.SOLAR_PLANT,
        go.CRYSTAL_MINE,
        go.DEUTERIUM_SYNTHESIZER,
        go.METAL_MINE,
        go.RESEARCH_LAB,
    ),
}


def _cursor_path(email: str) -> pathlib.Path:
    return STATE_DIR / f"{email.replace('/', '_')}.json"


def _read_visit_state(email: str) -> dict[str, Any]:
    state: dict[str, Any] = {"cursor": 0, "visit_seq": 0, "last_visited": {}}
    f = _cursor_path(email)
    if not f.exists():
        return state
    try:
        raw = json.loads(f.read_text())
    except (ValueError, OSError, TypeError):
        return state
    if not isinstance(raw, Mapping):
        return state
    for key in ("cursor", "visit_seq"):
        try:
            state[key] = max(0, int(raw.get(key, 0)))
        except (TypeError, ValueError, OverflowError):
            state[key] = 0
    visits = raw.get("last_visited")
    if isinstance(visits, Mapping):
        clean: dict[str, int] = {}
        for raw_pid, raw_seq in visits.items():
            try:
                clean[str(int(raw_pid))] = max(0, int(raw_seq))
            except (TypeError, ValueError, OverflowError):
                continue
        state["last_visited"] = clean
    return state


def _write_visit_state(email: str, state: Mapping[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(exist_ok=True)
        _cursor_path(email).write_text(
            json.dumps(dict(state), sort_keys=True, separators=(",", ":"))
        )
    except OSError:
        # 상태 기록 실패가 전체 봇 루프를 막아서는 안 된다. 다음 틱에는 결정적
        # health/id 정렬로 안전하게 폴백한다.
        pass


def _advance_cursor(email: str) -> int:
    """레거시 라운드로빈 커서를 증가시키되 새 방문 상태 필드는 보존한다."""
    state = _read_visit_state(email)
    cur = int(state["cursor"])
    state["cursor"] = cur + 1
    _write_visit_state(email, state)
    return cur


def select_colony(colonies: list[dict], cursor: int) -> dict | None:
    """라운드로빈: 커서 위치의 식민지 1개. 빈 목록이면 None. (순수 함수 — 단위검증용)"""
    if not colonies:
        return None
    return colonies[cursor % len(colonies)]


@dataclass(frozen=True)
class ColonyHealth:
    """식민지 선택에 필요한 검증 가능한 최소 건강 스냅샷."""

    planet_id: int
    energy_balance: int | None
    production_factor_pct: int | None
    zero_mines: bool
    critical: bool
    severity: int
    last_visited_seq: int
    age: int


def _production_factor_pct(resource_state: Mapping[str, Any] | None) -> int | None:
    if not isinstance(resource_state, Mapping):
        return None
    raw = resource_state.get("production_factor_pct")
    if raw is not None:
        try:
            return max(0, min(100, int(raw)))
        except (TypeError, ValueError, OverflowError):
            pass
    # 구 스냅샷도 energy_max/energy_used가 있으면 서버와 같은 비율로 추정한다.
    try:
        used = int(resource_state.get("energy_used") or 0)
        maximum = int(resource_state.get("energy_max") or 0)
    except (TypeError, ValueError, OverflowError):
        used = maximum = 0
    if used > 0:
        return max(0, min(100, int(100 * maximum / used)))
    try:
        # 총량 없이 잔액만 음수인 구 테스트/폴백 상태는 정확한 비율은 알 수 없지만,
        # 적어도 throttle 상태라는 사실은 잃지 않는다.
        if int(resource_state.get("energy") or 0) < 0:
            return 99
    except (TypeError, ValueError, OverflowError):
        pass
    return 100 if "energy" in resource_state else None


def assess_colony_health(
    colony: Mapping[str, Any],
    levels: Mapping[int, Any] | None,
    resource_state: Mapping[str, Any] | None,
    *,
    last_visited_seq: int = 0,
    current_seq: int = 0,
) -> ColonyHealth:
    """DB/HTTP 부작용 없는 건강도 계산.

    severity는 유한 범위라 아래 노후도 임계 선택이 반드시 공정성을 회복한다. 광산이
    전부 0인 부트스트랩 상태를 가장 강한 단일 신호로 두고, 실제 생산률 손실과 에너지
    적자 크기를 그 뒤에 합산한다.
    """
    pid = int(colony["id"])
    levels_known = isinstance(levels, Mapping)
    lv = levels if levels_known else {}
    zero_mines = levels_known and all(
        int(lv.get(tid, 0) or 0) <= 0
        for tid in (
            go.METAL_MINE,
            go.CRYSTAL_MINE,
            go.DEUTERIUM_SYNTHESIZER,
        )
    )
    energy_balance: int | None = None
    if isinstance(resource_state, Mapping) and resource_state.get("energy") is not None:
        try:
            energy_balance = int(resource_state["energy"])
        except (TypeError, ValueError, OverflowError):
            energy_balance = None
    production_factor = _production_factor_pct(resource_state)
    energy_deficit = max(0, -(energy_balance or 0))
    throttle_loss = (
        max(0, 100 - production_factor) if production_factor is not None else 0
    )
    severity = (
        # 비노후 상태에서는 광산 0 부트스트랩이 어떤 단일 기존 적자보다 앞선다.
        (6000 if zero_mines else 0)
        + min(3000, throttle_loss * 30)
        + min(2000, energy_deficit)
    )
    last = max(0, int(last_visited_seq))
    age = max(0, int(current_seq) - last)
    return ColonyHealth(
        planet_id=pid,
        energy_balance=energy_balance,
        production_factor_pct=production_factor,
        zero_mines=zero_mines,
        critical=bool(zero_mines or energy_deficit > 0 or throttle_loss > 0),
        severity=severity,
        last_visited_seq=last,
        age=age,
    )


def select_colony_by_health(
    colonies: list[dict],
    health_by_id: Mapping[int, ColonyHealth],
) -> dict | None:
    """위험 식민지 우선 + bounded aging 선택(결정적 planet id tie-break)."""
    if not colonies:
        return None
    threshold = max(MIN_STARVATION_AGE, len(colonies) * STARVATION_ROUNDS)
    overdue = [
        colony
        for colony in colonies
        if health_by_id[int(colony["id"])].age >= threshold
    ]
    if overdue:
        # 임계 노후 행성끼리는 가장 오래 기다린 순. 같은 나이면 더 아픈 행성 우선.
        return min(
            overdue,
            key=lambda colony: (
                -health_by_id[int(colony["id"])].age,
                -health_by_id[int(colony["id"])].severity,
                int(colony["id"]),
            ),
        )
    return min(
        colonies,
        key=lambda colony: (
            -health_by_id[int(colony["id"])].severity,
            -health_by_id[int(colony["id"])].age,
            int(colony["id"]),
        ),
    )


def _select_colony_for_account(
    email: str,
    colonies: list[dict],
) -> tuple[dict | None, ColonyHealth | None]:
    """전체 식민지 건강을 읽고 선택/방문 논리시계를 계정 상태와 함께 전진한다."""
    if not colonies:
        return None, None
    state = _read_visit_state(email)
    current_seq = int(state["visit_seq"]) + 1
    visits = dict(state["last_visited"])
    health_by_id: dict[int, ColonyHealth] = {}
    for colony in colonies:
        pid = int(colony["id"])
        try:
            levels = db_reader.get_levels_db(email, planet_id=pid)
        except Exception:  # noqa: BLE001 - unknown colonies age into a retry
            levels = None
        try:
            resources = db_reader.get_resource_state_db(email, planet_id=pid)
        except Exception:  # noqa: BLE001
            resources = None
        health_by_id[pid] = assess_colony_health(
            colony,
            levels,
            resources,
            last_visited_seq=int(visits.get(str(pid), 0)),
            current_seq=current_seq,
        )
    selected = select_colony_by_health(colonies, health_by_id)
    if selected is None:
        return None, None
    selected_id = int(selected["id"])
    visits[str(selected_id)] = current_seq
    owned = {str(int(colony["id"])) for colony in colonies}
    state.update({
        "cursor": int(state["cursor"]) + 1,
        "visit_seq": current_seq,
        "last_visited": {
            pid: seq for pid, seq in visits.items() if pid in owned
        },
    })
    _write_visit_state(email, state)
    return selected, health_by_id[selected_id]


def colony_build_order(
    personality_key: str,
    levels: dict[int, int],
    tick_index: int,
    *,
    energy: int | None = None,
    resource_state: dict | None = None,
    active_targets: set[int] | None = None,
    email: str | None = None,
    minimum_floors: Mapping[int, int] | None = None,
    role: str = "balanced",
) -> list[tuple]:
    """식민지 초기 빌드 오더(홈월드 커리큘럼 재사용). ("build"/"facility", tid) 리스트.

    조선소가 없으면 allow_satellite=False 로 태양광 발전소(건물)만 에너지 개입에 쓴다.
    조선소가 생기면(shipyard≥1) allow_satellite=True 라 태양광 발전소가 위성보다 비싼
    교차점부터 plan_primary 는 태양광을 빼고 광산/시설만 낸다(위성은 plan_secondary/조선소
    큐 담당 — _build_on_colony 가 병행 건조). 순수 함수(HTTP/DB 없음) — 단위검증용.
    """
    allow_satellite = levels.get(go.SHIPYARD, 0) >= 1
    ctx = build_tick_context(
        personality_key, levels, tick_index,
        energy=energy, resource_state=resource_state, email=email,
        active_targets=active_targets if active_targets is not None else set(),
        allow_satellite=allow_satellite,
    )
    return _order_development_candidates(
        plan_primary(ctx=ctx),
        levels,
        energy=energy,
        active_targets=active_targets if active_targets is not None else set(),
        minimum_floors=minimum_floors,
        role=role,
        allow_satellite=allow_satellite,
    )


def _clean_minimum_floors(
    minimum_floors: Mapping[int, int] | None,
) -> dict[int, int]:
    source = minimum_floors if isinstance(minimum_floors, Mapping) else DEFAULT_MINIMUM_FLOORS
    clean: dict[int, int] = {}
    for raw_tid, raw_level in source.items():
        try:
            tid, level = int(raw_tid), max(0, int(raw_level))
        except (TypeError, ValueError, OverflowError):
            continue
        if tid in DEFAULT_MINIMUM_FLOORS:
            clean[tid] = level
    return clean


def _order_development_candidates(
    candidates: list[tuple],
    levels: Mapping[int, int],
    *,
    energy: int | None,
    active_targets: set[int],
    minimum_floors: Mapping[int, int] | None,
    role: str,
    allow_satellite: bool = False,
) -> list[tuple]:
    """Keep energy safety first, then balanced floors, then role specialization."""
    normalized_role = str(role or "balanced").strip().lower()
    role_order = ROLE_TARGET_ORDER.get(normalized_role, ROLE_TARGET_ORDER["balanced"])
    role_rank = {tid: index for index, tid in enumerate(role_order)}
    floors = _clean_minimum_floors(minimum_floors)

    safety: list[tuple] = []
    for action in candidates:
        if (
            not allow_satellite
            and len(action) >= 2
            and action[1] == go.SOLAR_PLANT
            and (energy is None or energy <= 0)
        ):
            safety.append(action)

    floor_actions = [
        ("build", tid)
        for tid, floor in floors.items()
        if int(levels.get(tid, 0) or 0) < floor and tid not in active_targets
    ]
    floor_actions.sort(
        key=lambda action: (
            int(levels.get(action[1], 0) or 0) / max(1, floors[action[1]]),
            role_rank.get(action[1], len(role_rank)),
            action[1],
        )
    )
    specialized = sorted(
        candidates,
        key=lambda action: (
            role_rank.get(action[1], len(role_rank)) if len(action) >= 2 else len(role_rank),
            candidates.index(action),
        ),
    )
    ordered: list[tuple] = []
    seen: set[tuple] = set()
    for action in [*safety, *floor_actions, *specialized]:
        if action not in seen:
            seen.add(action)
            ordered.append(action)
    return ordered


def adaptive_satellite_batch(
    resource_state: Mapping[str, Any] | None,
    *,
    deficit: int,
    minimum_units: int = 0,
    risk_fraction: float = SATELLITE_RESOURCE_RISK_FRACTION,
    queue_capacity: int | None = None,
    max_batch: int = SATELLITE_RECOVERY_MAX_BATCH,
) -> int:
    """실제 에너지 부족량을 채울 안전한 태양광 위성 배치를 계산한다.

    ``minimum_units``는 다음 광산 업그레이드로 생길 선제 적자를 energy.plan_energy가
    이미 계산한 경우 보존한다. 보유 자원으로 살 수 있는 수량, 이번 행동에 노출할 자원
    비율, 절대 배치 상한, 선택적으로 전달된 조선소 큐 수용량 중 최솟값만 허용한다.
    실가동에서는 이 결과가 miner_bot._try_first를 거치며 동일한 live queue horizon과
    pending-unit 제한을 한 번 더 적용받는다.
    """
    if not isinstance(resource_state, Mapping):
        return 0
    try:
        sat_each = go.solar_satellite_energy_each(int(resource_state["temp_max"]))
        needed = max(
            max(0, int(minimum_units)),
            math.ceil(max(0, int(deficit)) / sat_each) if sat_each > 0 else 0,
        )
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return 0
    if sat_each <= 0 or needed <= 0:
        return 0
    price = go.ship_cost(go.SOLAR_SATELLITE)
    if not price:
        return 0
    affordable_caps: list[int] = []
    for resource, unit_cost in price.items():
        if int(unit_cost) <= 0:
            continue
        try:
            available = max(0, int(resource_state.get(resource, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
        affordable_caps.append(available // int(unit_cost))
    if not affordable_caps:
        return 0
    affordable = min(affordable_caps)
    try:
        fraction = max(0.0, min(1.0, float(risk_fraction)))
    except (TypeError, ValueError, OverflowError):
        fraction = SATELLITE_RESOURCE_RISK_FRACTION
    # 자원이 정확히 1기분뿐이어도 복구가 영구 정지하지 않도록, 양의 위험 예산이면
    # 최소 1기는 허용한다. 0은 명시적인 fail-closed다.
    risk_cap = (
        max(1, int(math.floor(affordable * fraction)))
        if affordable > 0 and fraction > 0
        else 0
    )
    caps = [
        needed,
        affordable,
        risk_cap,
        max(0, int(max_batch)),
    ]
    if queue_capacity is not None:
        try:
            caps.append(max(0, int(queue_capacity)))
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, min(caps))


def _satellite_risk_fraction(policy: Mapping[str, Any] | None) -> float:
    if not isinstance(policy, Mapping):
        return SATELLITE_RESOURCE_RISK_FRACTION
    limits = policy.get("limits")
    if not isinstance(limits, Mapping):
        return SATELLITE_RESOURCE_RISK_FRACTION
    try:
        # AI의 기존 함대 위험 예산을 파괴 가능한 위성 배치에도 보수적으로 재사용한다.
        # 명시적 0은 위성 노출을 원하지 않는 정책이므로 지상 태양광 폴백을 위해
        # 그대로 존중하고, 양수 값에만 75% 절대 상한을 둔다.
        return max(0.0, min(0.75, float(limits["fleet_risk_percent"]) / 100.0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return SATELLITE_RESOURCE_RISK_FRACTION


def _adaptive_satellite_candidates(
    candidates: list[tuple],
    resource_state: Mapping[str, Any] | None,
    *,
    deficit: int,
    policy: Mapping[str, Any] | None,
) -> list[tuple]:
    """에너지 복구용 위성 후보만 고정 x8 대신 동적 배치로 치환한다."""
    original = next(
        (
            action for action in candidates
            if len(action) >= 3
            and action[0] == "ship"
            and int(action[1]) == go.SOLAR_SATELLITE
        ),
        None,
    )
    if original is None:
        return candidates
    batch = adaptive_satellite_batch(
        resource_state,
        deficit=deficit,
        minimum_units=max(0, int(original[2])),
        risk_fraction=_satellite_risk_fraction(policy),
    )
    without_satellite = [
        action
        for action in candidates
        if not (
            len(action) >= 2
            and action[0] == "ship"
            and int(action[1]) == go.SOLAR_SATELLITE
        )
    ]
    return (
        [("ship", go.SOLAR_SATELLITE, batch), *without_satellite]
        if batch > 0
        else without_satellite
    )


def _new_unit_queue_items(before: Any, after: Any) -> list[Any] | None:
    """Return queue rows added between two authoritative snapshots."""
    if (
        before is None
        or after is None
        or before.available is not True
        or after.available is not True
        or before.planet_id != after.planet_id
    ):
        return None

    def identity(item: Any) -> tuple[int, int, int, int]:
        return (
            int(item.object_id),
            int(item.requested_amount),
            int(item.time_start),
            int(item.time_end),
        )

    remaining = Counter(identity(item) for item in before.unit_items)
    added: list[Any] = []
    for item in after.unit_items:
        key = identity(item)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            added.append(item)
    return added


def colony_dev_tick(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    *,
    policy: dict | None = None,
    start_planet_id: int | None = None,
    home_planet_id: int | None = None,
    start_planet: Mapping[str, Any] | int | None = None,
    home_planet: Mapping[str, Any] | int | None = None,
    minimum_floors: Mapping[int, int] | None = None,
    role: str = "balanced",
    structured: bool = False,
) -> str | dict[str, Any] | None:
    """식민지 개발 1건. 로그 문자열 또는 None(행동 없음) 반환.

    이 함수는 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호). 홈월드 복귀는
    _colony_dev_tick_inner 의 finally 가 보장하므로, 여기서 예외를 잡아도 이미 복귀는 끝났다.

    miner_bot 훅 연결(raid_tick/colony_tick 과 동일):
      note = colony_dev.colony_dev_tick(client, email, pkey, tick_index)
      if note: done.append(note)
    """
    outcome = colony_dev_outcome(
        client,
        email,
        personality_key,
        tick_index,
        policy=policy,
        start_planet_id=start_planet_id,
        home_planet_id=home_planet_id,
        start_planet=start_planet,
        home_planet=home_planet,
        minimum_floors=minimum_floors,
        role=role,
    )
    return outcome if structured else _legacy_outcome_note(outcome)


def _planet_identity(value: Mapping[str, Any] | int | None) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("id", value.get("planet_id"))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _legacy_outcome_note(outcome: Mapping[str, Any]) -> str | None:
    note = outcome.get("note")
    if isinstance(note, str) and note:
        return note
    if outcome.get("status") == "error":
        return f"colonydev err {outcome.get('error', outcome.get('reason', 'unknown'))}"
    return None


def colony_dev_outcome(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    *,
    policy: dict | None = None,
    start_planet_id: int | None = None,
    home_planet_id: int | None = None,
    start_planet: Mapping[str, Any] | int | None = None,
    home_planet: Mapping[str, Any] | int | None = None,
    minimum_floors: Mapping[int, int] | None = None,
    role: str = "balanced",
) -> dict[str, Any]:
    """Develop one colony and return a structured outcome/rejection.

    ``start_planet`` is the identity restored in ``finally``. ``home_planet`` is
    the canonical planet excluded from colony selection.  When omitted, both
    preserve the legacy current-planet inference.
    """
    try:
        return _colony_dev_outcome_inner(
            client,
            email,
            personality_key,
            tick_index,
            policy=policy,
            start_planet_id=start_planet_id,
            home_planet_id=home_planet_id,
            start_planet=start_planet,
            home_planet=home_planet,
            minimum_floors=minimum_floors,
            role=role,
        )
    except Exception as exc:  # noqa: BLE001 - loop protection and structured rejection
        return {
            "ok": False,
            "status": "error",
            "reason": "colony_development_error",
            "error": f"{type(exc).__name__}: {exc}",
            "actions": [],
        }


def _colony_dev_tick_inner(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    *,
    policy: dict | None = None,
) -> str | None:
    """Legacy private adapter retained for existing direct callers."""
    return _legacy_outcome_note(
        colony_dev_outcome(
            client, email, personality_key, tick_index, policy=policy
        )
    )


def _colony_dev_outcome_inner(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    *,
    policy: dict | None = None,
    start_planet_id: int | None = None,
    home_planet_id: int | None = None,
    start_planet: Mapping[str, Any] | int | None = None,
    home_planet: Mapping[str, Any] | int | None = None,
    minimum_floors: Mapping[int, int] | None = None,
    role: str = "balanced",
) -> dict[str, Any]:
    planets = db_reader.list_planets_db(email)
    current = next((p for p in planets if p["is_current"]), None)
    supplied_start_id = start_planet_id if start_planet_id is not None else _planet_identity(start_planet)
    supplied_home_id = home_planet_id if home_planet_id is not None else _planet_identity(home_planet)
    restore_id = _planet_identity(supplied_start_id) or (
        int(current["id"]) if current is not None else None
    )
    canonical_home_id = _planet_identity(supplied_home_id) or restore_id
    owned_ids = {int(planet["id"]) for planet in planets}
    if restore_id is None or restore_id not in owned_ids:
        return {
            "ok": False,
            "status": "rejected",
            "reason": "start_planet_unknown",
            "actions": [],
            "start_planet_id": restore_id,
        }
    if canonical_home_id is None or canonical_home_id not in owned_ids:
        return {
            "ok": False,
            "status": "rejected",
            "reason": "home_planet_unknown",
            "actions": [],
            "start_planet_id": restore_id,
            "home_planet_id": canonical_home_id,
        }
    colonies = [p for p in planets if int(p["id"]) != canonical_home_id]
    if not colonies:
        return {
            "ok": True,
            "status": "idle",
            "reason": "no_colonies",
            "actions": [],
            "start_planet_id": restore_id,
            "home_planet_id": canonical_home_id,
        }

    colony, selected_health = _select_colony_for_account(email, colonies)
    if colony is None:  # 논리상 도달 불가(colonies 비지 않음)지만 방어적으로.
        return {
            "ok": True,
            "status": "idle",
            "reason": "no_colony_selected",
            "actions": [],
            "start_planet_id": restore_id,
            "home_planet_id": canonical_home_id,
        }
    cid = colony["id"]
    coord = f"{colony['galaxy']}:{colony['system']}:{colony['position']}"
    outcome: dict[str, Any] = {
        "ok": False,
        "status": "rejected",
        "reason": "planet_switch_failed",
        "planet_id": cid,
        "coordinate": coord,
        "role": str(role or "balanced"),
        "actions": [],
        "start_planet_id": restore_id,
        "home_planet_id": canonical_home_id,
        "restored_planet_id": None,
        "restore_ok": False,
    }
    if selected_health is not None:
        outcome["health"] = {
            "critical": selected_health.critical,
            "zero_mines": selected_health.zero_mines,
            "energy_balance": selected_health.energy_balance,
            "production_factor_pct": selected_health.production_factor_pct,
            "severity": selected_health.severity,
            "age": selected_health.age,
        }
    try:
        if not planets_mod.switch_planet(client, cid):
            outcome["note"] = f"colonydev idle (전환 실패 {coord} pid={cid})"
            return outcome
        built = _build_on_colony(
            client,
            email,
            personality_key,
            tick_index,
            colony,
            coord,
            policy=policy,
            minimum_floors=minimum_floors,
            role=role,
            structured=True,
        )
        outcome.update(built)
        return outcome
    except Exception as exc:  # noqa: BLE001 - finally still restores the start planet
        outcome.update({
            "ok": False,
            "status": "error",
            "reason": "colony_build_error",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return outcome
    finally:
        # Restore the actual starting identity, not a transiently inferred home.
        restored = planets_mod.switch_planet(client, restore_id)
        outcome["restored_planet_id"] = restore_id
        outcome["restore_ok"] = bool(restored)
        if not restored:
            outcome["ok"] = False
            outcome["status"] = "rejected"
            outcome["reason"] = "start_planet_restore_failed"


def _build_on_colony(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    colony: dict,
    coord: str,
    *,
    policy: dict | None = None,
    minimum_floors: Mapping[int, int] | None = None,
    role: str = "balanced",
    structured: bool = False,
) -> str | dict[str, Any] | None:
    """전환된 식민지에서 저비용 빌드 1~2건. planet_id 로 상태를 직독해 계획 계층에 넘긴다."""
    cid = colony["id"]
    try:
        levels = db_reader.get_levels_db(email, planet_id=cid)
    except Exception:  # noqa: BLE001 - 레벨 미상이면 이번 틱은 조용히 대기(홈 복귀는 finally)
        result = {
            "ok": False,
            "status": "rejected",
            "reason": "colony_levels_unknown",
            "actions": [],
            "note": None,
        }
        return result if structured else None

    resource_state: dict | None = None
    energy: int | None = None
    try:
        resource_state = db_reader.get_resource_state_db(email, planet_id=cid)
        energy = resource_state.get("energy")
    except Exception:  # noqa: BLE001 - 자원현황 미상이면 저장고/에너지 판정만 생략
        resource_state = None

    active_targets: set[int] = set()
    try:
        active_targets = db_reader.active_build_targets(email, planet_id=cid)
    except Exception:  # noqa: BLE001
        active_targets = set()

    try:
        account_ship_totals = db_reader.get_account_ship_totals_db(email)
    except Exception:  # noqa: BLE001 - 함선 계획만 fail-closed
        account_ship_totals = None

    # 홈월드와 동일한 컨텍스트/계획 계층을 쓴다(초기 커리큘럼 재사용, 사전체크 포함).
    # 조선소가 있으면 홈월드처럼 위성 경로를 연다(allow_satellite=True): 고레벨 태양광
    # 발전소(자원 100만+)를 식민지 자체 생산으로 못 사 적자가 방치되던 버그를 위성으로 메운다.
    allow_sat = levels.get(go.SHIPYARD, 0) >= 1
    ctx = build_tick_context(
        personality_key, levels, tick_index,
        energy=energy, resource_state=resource_state, email=email,
        active_targets=active_targets, allow_satellite=allow_sat,
        account_ship_totals=account_ship_totals,
        require_account_ship_totals=True,
        ship_strategy=(policy or {}).get("ship_strategy"),
    )
    primary_candidates = plan_primary(ctx=ctx)
    candidates = (
        _order_development_candidates(
            primary_candidates,
            levels,
            energy=energy,
            active_targets=active_targets,
            minimum_floors=minimum_floors,
            role=role,
        )
        if policy is None or action_enabled(policy, "build")
        else []
    )

    # AI 정책은 홈과 같은 우선순위·에너지 목표를 식민지에도 적용한다. 규칙봇은 policy=None
    # 으로 들어와 종전 커리큘럼과 완전히 같은 후보를 유지한다.
    if policy is not None and candidates:
        from bots.ai_agents.policy_engine import apply_build_priority, _primary_safety_actions

        candidates = apply_build_priority(
            candidates,
            policy.get("build_priority", []),
            energy,
            int(policy.get("energy_target", 0)),
            protected_actions=_primary_safety_actions(
                candidates, levels, energy, resource_state
            ),
        )

    # _queue_actions/_fmt_action 재사용(사전 자원체크·큐 생존검증 그대로). miner_bot 이
    # colony_dev 를 import 하므로 순환 회피를 위해 런타임 지연 import.
    from bots.rule_based import miner_bot
    from bots.rule_based.miner_bot import _queue_actions, _fmt_action, _try_first

    # 전환 후 users.planet_current 가 이 식민지이므로, _queue_actions 내부의 email 기반
    # 큐 생존검증/취소분류(latest_queue_canceled 등)도 이 식민지 기준으로 정확히 해석된다.
    # backoff_key 를 행성 스코프로 줘 홈월드/형제 식민지와 자원부족 백오프를 분리한다
    # (자원이 행성별 독립 — 홈월드의 자원부족이 식민지 태양광/광산을 막으면 안 된다).
    acts, _last = _queue_actions(
        client, candidates, MAX_BUILD_PER_COLONY, ctx=ctx,
        backoff_key=f"{email}#colony{cid}",
    )
    done = [_fmt_action(a) for a in acts]
    queued_actions: list[dict[str, Any]] = []
    untracked_action_count = 0
    queued_level_offsets: dict[int, int] = {}
    for action in acts:
        if len(action) < 2 or action[0] not in {"build", "facility"}:
            continue
        object_id = int(action[1])
        queued_level_offsets[object_id] = queued_level_offsets.get(object_id, 0) + 1
        queued_actions.append({
            "object_type": "building" if action[0] == "build" else "facility",
            "object_id": object_id,
            "planet_id": int(cid),
            "target_level": int(levels.get(object_id, 0) or 0)
            + queued_level_offsets[object_id],
        })

    secondary = plan_secondary(ctx=ctx)
    if policy is not None and action_enabled(policy, "research"):
        from bots.ai_agents.policy_engine import apply_research_priority

        research = apply_research_priority(
            [a for a in secondary if a[0] == "research"],
            policy.get("research_priority", []),
            levels,
        )
        if not miner_bot.research_blocked_by_lab(active_targets, acts):
            research_acts, _ = _queue_actions(
                client, research, miner_bot.MAX_RESEARCH_PER_TICK, ctx=ctx,
                backoff_key=f"{email}#colony{cid}",
            )
            done.extend(_fmt_action(a) for a in research_acts)
            for action in research_acts:
                if len(action) < 2 or action[0] != "research":
                    continue
                object_id = int(action[1])
                queued_level_offsets[object_id] = (
                    queued_level_offsets.get(object_id, 0) + 1
                )
                queued_actions.append({
                    "object_type": "research",
                    "object_id": object_id,
                    "planet_id": int(cid),
                    "target_level": int(levels.get(object_id, 0) or 0)
                    + queued_level_offsets[object_id],
                })

    # 식민지 조선소도 홈과 같은 동적 함대 전략을 집행한다. 계정 전체 보유량을 기준으로
    # 구성 부족을 계산하므로 여러 식민지가 같은 고정 상한을 중복 생산하지 않는다.
    ship_candidates: list[tuple] = []
    if policy is None:
        if allow_sat:
            ship_candidates = [a for a in secondary if a[0] == "ship"]
    elif action_enabled(policy, "ship"):
        from bots.ai_agents.policy_engine import apply_ship_priority
        ship_candidates = apply_ship_priority(
            [a for a in secondary if a[0] == "ship"],
            policy.get("ship_priority", []),
            levels,
            resource_state=resource_state,
            energy=energy,
            energy_target=int(policy.get("energy_target", 0)),
            allow_policy_extras=policy.get("autonomy") == "full",
            account_ship_totals=account_ship_totals,
            enforce_max_total_on_shared=policy.get("autonomy") != "full",
        )
    # plan_secondary의 기존 에너지 개입 후보가 고정 최대 8기로 잘려 있던 부분만
    # 실제 deficit/온도/자원 위험예산 기반 수량으로 교체한다. 일반 성장 목표로 나온
    # 위성 후보는 건드리지 않아 명시적 정책/부트스트랩 권한을 보존한다.
    if ship_candidates and energy is not None:
        from bots.rule_based import energy as energy_rule

        energy_plan = energy_rule.plan_energy(
            levels,
            energy,
            resource_state,
            primary_candidates,
            allow_satellite=allow_sat,
        )
        if energy_plan.satellite_action is not None:
            policy_target = 0
            if isinstance(policy, Mapping):
                try:
                    policy_target = max(0, int(policy.get("energy_target", 0)))
                except (TypeError, ValueError, OverflowError):
                    policy_target = 0
            target_deficit = (
                max(0, policy_target - int(energy))
                if energy is not None else 0
            )
            ship_candidates = _adaptive_satellite_candidates(
                ship_candidates,
                resource_state,
                deficit=max(energy_plan.deficit, target_deficit),
                policy=policy,
            )
    if ship_candidates:
        sat_act, _sat_msg = _try_first(client, ship_candidates)
        if sat_act is not None:
            done.append(_fmt_action(sat_act))
            if len(sat_act) >= 3 and sat_act[0] == "ship":
                object_id = int(sat_act[1])
                amount = max(1, int(sat_act[2]))
                queued_actions.append({
                    "object_type": "ship",
                    "object_id": object_id,
                    "planet_id": int(cid),
                    "target_total": int(levels.get(object_id, 0) or 0) + amount,
                })

    # 식민지도 홈과 같은 방어 판단을 받는다. AI는 생산량/저장자원 관측에서 직접 고른
    # defense_strategy를, 규칙봇은 성격별 동적 방어 알고리즘을 사용한다.
    from bots.rule_based import miner_defense, turtle

    defense_note = None
    defense_queue_before = (
        production_capacity.production_queue_snapshot(email, planet_id=int(cid))
        if structured
        else None
    )
    if policy is not None and action_enabled(policy, "defense"):
        defense_note = turtle.defense_tick(
            client,
            email,
            levels,
            energy=energy,
            defense_ratio=policy.get("defense_ratio"),
            strategy=policy.get("defense_strategy"),
        )
    elif policy is None:
        if personality_key == "turtle":
            defense_note = turtle.defense_tick(client, email, levels, energy=energy)
        elif miner_defense.MINER_BASE_DEFENSE_ENABLED:
            defense_note = miner_defense.base_defense_tick(
                client, email, levels, personality_key, energy=energy
            )
    defense_queue_after = (
        production_capacity.production_queue_snapshot(email, planet_id=int(cid))
        if structured
        else None
    )
    if defense_note:
        done.append(defense_note)
        added_items = _new_unit_queue_items(
            defense_queue_before, defense_queue_after
        )
        if added_items is None:
            untracked_action_count += 1
        else:
            for item in added_items:
                object_id = int(item.object_id)
                if object_id not in go.DEFENSES:
                    continue
                amount = max(1, int(item.requested_amount))
                queued_actions.append({
                    "object_type": "defense",
                    "object_id": object_id,
                    "planet_id": int(cid),
                    "target_total": int(levels.get(object_id, 0) or 0) + amount,
                })

    if done:
        note = f"colonydev [{coord}] {' + '.join(done)} E={energy}"
        result = {
            "ok": True,
            "status": "executed",
            "reason": "development_actions_queued",
            "actions": done,
            "queued_actions": queued_actions,
            "untracked_action_count": untracked_action_count,
            "note": note,
            "energy": energy,
        }
        return result if structured else note
    result = {
        "ok": True,
        "status": "idle",
        "reason": "no_affordable_or_available_actions",
        "actions": [],
        "note": None,
        "energy": energy,
    }
    return result if structured else None
