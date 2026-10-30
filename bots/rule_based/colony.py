"""식민지 확장 규칙 로직.

miner_bot.tick() 이 건설/연구/함선 건조를 마친 뒤 `colony_tick()` 을 호출해
**한 틱에 식민 관련 행동 1건**을 얹는다(raider.raid_tick 패턴과 동일).

식민 체인(틱당 1건, 조건 미충족이면 조용히 None):
  1. 행성 수가 전략 상한(MAX_PLANETS, 호출별 하향 가능)에 도달 → 스킵.
  2. 다음 행성을 늘리려면 천체물리(astrophysics) 레벨이 충분해야 한다.
     부족하면 선행 연구(energy→impulse→espionage) 후 천체물리를 연구 큐가 빌 때만 1건.
  3. 천체물리 충족 + 식민선 없음 → 조선소 준비되면 식민선 1대 건조.
  4. 식민선 보유 → 자기 시스템 ±SCAN_SPAN 에서 빈 슬롯을 찾아 식민 파견.
  5. 식민 미션 진행 중이면 스킵(중복 파견 방지). 진행 상태는 .colony_state 에 기록.

서버에서 확인한 규칙(근거):
  - 최대 행성 수 = 1 + round(astro/2)
    (PlayerService::getMaxPlanetAmount, ResearchObjects astrophysics MAX_COLONIES).
    정수 astro 에서 round(astro/2)==(astro+1)//2 (PHP round=half-away-from-zero).
    도착 시 `planetCount()+1 > max_planets` 이면 실패(ColonisationMission::processArrival).
  - astrophysics(124) 선행: research_lab≥3, impulse_drive≥3, espionage_technology≥4.
    연쇄로 impulse_drive≥3 은 energy_technology≥1·research_lab≥2, espionage≥4 는 lab≥3.
  - colony_ship(208) 선행: shipyard≥4, impulse_drive≥3 (CivilShipObjects).
  - 슬롯 4~12 는 astro 무관 식민 가능, 3/13 은 astro≥4, 2/14 astro≥6, 1/15 astro≥8
    (PlayerService::canColonizePosition) → 필드 수 유리한 4~12 만 노린다.

시설(연구소/조선소) 자체 건설은 miner_bot 담당이라 여기서 하지 않는다.
선행 시설이 부족하면 이 틱은 조용히 대기하고, miner 가 시설을 올리면 다음 틱 진행된다.

안전장치:
  - 사람 계정 시스템 [1:44] 은 식민 대상에서 완전 제외(USER_EXCLUDE_SYSTEMS).
  - colony_tick 은 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호).
  - 식민 미션 진행 중(fleet_events) + 상태파일 쿨다운으로 중복 파견을 막는다.

부하 원칙:
  - 행성 목록, 연구/unit 큐, 진행 중 식민 미션, 빈 슬롯은 읽기 전용 DB를 우선한다.
  - DB 장애 때만 기존 overview/research/eventlist/galaxy HTTP를 안전 폴백으로 쓴다.
  - DB와 HTTP가 모두 실패하면 "비어 있음"으로 낙관하지 않고 행동을 막는다(fail-closed).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time
from collections.abc import Mapping, Sequence
from typing import Any

from bots.shared import db_reader
from bots.shared import fleet
from bots.shared import planets as planets_mod
from bots.rule_based import fleetsave
from bots.rule_based import colonization_planner
from bots.shared.game_objects import (
    ASTROPHYSICS,          # 124
    COLONY_SHIP,           # 208
    ENERGY_TECHNOLOGY,     # 113
    RESEARCH_LAB,          # 31
    SHIPYARD,              # 21
)

IMPULSE_DRIVE = 117
ESPIONAGE_TECHNOLOGY = 106

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
STATE_DIR = pathlib.Path(__file__).resolve().parent / ".colony_state"

# 정책 호환용 상수. 실제 행성 수 상한은 천체물리학 공식만 사용한다.
MAX_PLANETS = 99

# 기존 API 호환용 값. 실제 후보는 현재 은하의 1..499 시스템 전체에서 고른다.
SCAN_SPAN = 499
MAX_SYSTEM = 499

# 별도 금지 구역을 두지 않는다. 점유 좌표는 출발 직전에 별도로 차단한다.
USER_EXCLUDE_SYSTEMS: set[tuple[int, int]] = set()

# 필드 수가 유리한 중앙을 먼저 보되, 천체물리가 허용하면 가장자리까지 모두 사용한다.
PREFERRED_POSITIONS = (8, 9, 7, 10, 6, 11, 5, 12, 4, 13, 3, 14, 2, 15, 1)

# 같은 좌표 재파견 쿨다운(초). 미션 조회 실패 시의 백업 중복방지.
COLONIZE_COOLDOWN_SEC = 1800

# OGameX CharacterClass enum / CharacterClassService fleet-slot bonus.
GENERAL_CLASS_ID = 2
GENERAL_FLEET_SLOT_BONUS = 2


# ---------------------------------------------------------------------------
# 상한/선행 연구 계산
# ---------------------------------------------------------------------------
def max_planets_for_astro(astro: int) -> int:
    """천체물리 레벨에서 허용되는 최대 행성 수 = 1 + round(astro/2)."""
    return 1 + (astro + 1) // 2


def astro_needed_for(target_planets: int) -> int:
    """target_planets 개를 가지려면 필요한 최소 천체물리 레벨.

    1 + (a+1)//2 >= target  <=>  a >= 2*(target-1) - 1  (하한 0).
    """
    return max(0, 2 * (target_planets - 1) - 1)


def next_research_step(levels: dict[int, int], astro_target: int) -> int | None:
    """천체물리 astro_target 달성까지의 다음 연구 1건(선행조건 만족한 것만).

    선행조건이 아직 안 갖춰진(연구소 레벨 부족 등) 단계는 건너뛰고 None 을 줄 수 있다.
    그 경우 miner_bot 이 시설을 올리면 다음 틱에 진행된다.
    """
    lab = levels.get(RESEARCH_LAB, 0)
    energy = levels.get(ENERGY_TECHNOLOGY, 0)
    impulse = levels.get(IMPULSE_DRIVE, 0)
    espionage = levels.get(ESPIONAGE_TECHNOLOGY, 0)
    astro = levels.get(ASTROPHYSICS, 0)

    if energy < 1 and lab >= 1:
        return ENERGY_TECHNOLOGY
    if impulse < 3 and energy >= 1 and lab >= 2:
        return IMPULSE_DRIVE
    if espionage < 4 and lab >= 3:
        return ESPIONAGE_TECHNOLOGY
    if astro < astro_target and lab >= 3 and impulse >= 3 and espionage >= 4:
        return ASTROPHYSICS
    return None


# ---------------------------------------------------------------------------
# 연구 큐 상태
# ---------------------------------------------------------------------------
def research_in_progress(client, email: str | None = None) -> bool:
    """유저 연구 큐 또는 연구소 공사가 진행 중인지. DB 우선, /research는 장애 폴백.

    연구 큐는 행성이 아니라 유저 단위로 공유되므로 유저의 모든 행성 큐를 센다. 대기 항목도
    미래 자원 약정이므로 ``processed=0 AND canceled=0``이면 busy다. OGameX는 연구소(31)
    업그레이드 중 연구 요청도 취소시키므로, 어느 행성에서든 활성 연구소 건설 큐가 있으면
    함께 busy로 본다. DB 조회가 실패할 때만 기존 ``researchCountdown`` HTML 판정을 쓰며,
    그것도 실패하면 True로 닫아 중복 연구를 보내지 않는다.
    """
    if email:
        try:
            conn = db_reader._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ("
                    "EXISTS(SELECT 1 FROM research_queues rq "
                    " JOIN planets rp ON rp.id = rq.planet_id "
                    " WHERE rp.user_id = u.id AND rq.processed = 0 AND rq.canceled = 0) "
                    "OR EXISTS(SELECT 1 FROM building_queues bq "
                    " JOIN planets bp ON bp.id = bq.planet_id "
                    " WHERE bp.user_id = u.id AND bq.object_id = %s "
                    " AND bq.processed = 0 AND bq.canceled = 0)) n "
                    "FROM users u WHERE u.email = %s",
                    (RESEARCH_LAB, email),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("research queue count missing")
            return int(row["n"]) > 0
        except Exception:  # noqa: BLE001 - DB 장애 때만 기존 HTTP 폴백
            pass
    try:
        html = client.session.get(f"{client.base_url}/research").text
    except Exception:  # noqa: BLE001
        return True
    return "researchCountdown" in html


def unit_queue_in_progress(email: str, planet_id: int | None = None) -> bool:
    """현재 또는 명시한 행성의 함선/방어 공용 unit queue가 차 있으면 True.

    식민선은 raid의 리사이클러 건조나 miner의 정체성 함선과 같은 큐를 쓴다. 큐가 하나라도
    있으면 식민선을 덧붙이지 않는다. 별도 저비용 HTTP 판정이 없으므로 DB 장애 시 True로
    닫는다.
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            if planet_id is None:
                cur.execute(
                    "SELECT COUNT(*) n FROM unit_queues uq "
                    "JOIN planets p ON p.id = uq.planet_id "
                    "JOIN users u ON u.planet_current = p.id "
                    "WHERE u.email = %s AND uq.processed = 0",
                    (email,),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) n FROM unit_queues uq "
                    "JOIN planets p ON p.id = uq.planet_id "
                    "JOIN users u ON u.id = p.user_id "
                    "WHERE u.email = %s AND p.id = %s AND uq.processed = 0",
                    (email, int(planet_id)),
                )
            row = cur.fetchone()
        return True if row is None else int(row["n"]) > 0
    except Exception:  # noqa: BLE001 - 불명 큐에 식민선을 덧붙이지 않음
        return True


# ---------------------------------------------------------------------------
# 상태 파일 (식민 파견 쿨다운)
# ---------------------------------------------------------------------------
def _state_path(email: str) -> pathlib.Path:
    return STATE_DIR / f"{email.replace('/', '_')}.json"


def _load_state(email: str) -> dict:
    f = _state_path(email)
    if f.exists():
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def _save_state(email: str, state: dict) -> None:
    try:
        STATE_DIR.mkdir(exist_ok=True)
        _state_path(email).write_text(json.dumps(state))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 식민 미션 진행 여부
# ---------------------------------------------------------------------------
def colonisation_in_flight(client, email: str | None = None) -> bool:
    """진행 중(귀환 아님) 식민 미션 여부. DB 우선, eventlist 장애 폴백.

    ``parent_id IS NULL``은 출발 미션만 세어 귀환 레그를 제외한다. DB/HTTP 모두 실패하면
    이미 식민선이 날아간 것으로 간주(True)해 중복 파견을 막는다.
    """
    if email:
        try:
            conn = db_reader._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) n FROM fleet_missions fm "
                    "JOIN users u ON u.id = fm.user_id "
                    "WHERE u.email = %s AND fm.processed = 0 AND fm.canceled = 0 "
                    "AND fm.mission_type = %s AND fm.parent_id IS NULL",
                    (email, fleet.MISSION_COLONISATION),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("colonisation count missing")
            return int(row["n"]) > 0
        except Exception:  # noqa: BLE001 - DB 장애 때만 eventlist 폴백
            pass
    try:
        ev = fleet.fleet_events(client)
    except Exception:  # noqa: BLE001
        return True
    for e in ev.get("events", []):
        if e.get("return_flight"):
            continue
        if e.get("mission_type") == fleet.MISSION_COLONISATION:
            return True
    return False


# ---------------------------------------------------------------------------
# 빈 슬롯 탐색
# ---------------------------------------------------------------------------
def find_empty_slot(
    client, galaxy: int, system: int, *, astro: int = 0
) -> tuple[int, int, int] | None:
    """galaxy:system 에서 현재 천체물리로 식민 가능한 빈 좌표를 하나 고른다.

    DB의 planets(planet_type=1)를 우선 읽고, DB 장애 때만 scan_galaxy HTTP로 폴백한다.
    둘 다 '점유된' 행성만 주므로 4~12 중 없는 위치가 빈 슬롯이다. 필드 수 유리한 중앙
    위치를 우선한다. DB와 HTTP가 모두 실패하면 None.
    """
    if (galaxy, system) in USER_EXCLUDE_SYSTEMS:
        return None
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT planet AS position FROM planets "
                "WHERE galaxy = %s AND system = %s AND planet_type = 1",
                (galaxy, system),
            )
            occupied = {int(r["position"]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - DB 장애 때만 기존 은하 HTTP 폴백
        try:
            rows = fleet.scan_galaxy(client, galaxy, system)
        except Exception:  # noqa: BLE001
            return None
        occupied = {r.get("position") for r in rows}
    for pos in PREFERRED_POSITIONS:
        if astro < astro_needed_for_position(pos):
            continue
        if pos not in occupied:
            return (galaxy, system, pos)
    return None


def find_empty_slot_across_systems(
    client,
    galaxy: int,
    systems,
    *,
    astro: int = 0,
    blocked: set[tuple[int, int, int]] | None = None,
) -> tuple[int, int, int] | None:
    """은하 점유 현황을 DB 한 번으로 읽고 시스템 순서에서 첫 빈 좌표를 고른다.

    DB 장애 시에는 첫 후보 시스템만 기존 HTTP 폴백으로 확인한다. 장애 중 499개
    시스템을 연속 조회하지 않고 다음 틱에 재시도하는 fail-closed 동작이다.
    """
    ordered_systems = [
        int(system) for system in systems
        if 1 <= int(system) <= MAX_SYSTEM
        and (galaxy, int(system)) not in USER_EXCLUDE_SYSTEMS
    ]
    if not ordered_systems:
        return None
    blocked = blocked or set()
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT system, planet FROM planets "
                "WHERE galaxy = %s AND system BETWEEN 1 AND %s "
                "AND planet_type = 1",
                (galaxy, MAX_SYSTEM),
            )
            rows = cur.fetchall()
        occupied: dict[int, set[int]] = {}
        for row in rows:
            occupied.setdefault(int(row["system"]), set()).add(int(row["planet"]))
    except Exception:  # noqa: BLE001 - 장애 중 HTTP 폭주를 막아 첫 후보만 확인
        slot = find_empty_slot(client, galaxy, ordered_systems[0], astro=astro)
        return None if slot in blocked else slot

    allowed_positions = [
        pos for pos in PREFERRED_POSITIONS
        if astro >= astro_needed_for_position(pos)
    ]
    for system in ordered_systems:
        used = occupied.get(system, set())
        for position in allowed_positions:
            slot = (galaxy, system, position)
            if position not in used and slot not in blocked:
                return slot
    return None


def target_occupancy(
    client, target: Mapping[str, Any] | Sequence[Any]
) -> bool | None:
    """Return True/False for one exact coordinate, or None when it cannot be checked.

    This is stricter than ``find_empty_slot``: structured commands never replace
    an occupied target with a nearby slot.  The same DB-first, HTTP-fallback and
    fail-closed rules are used immediately before dispatch.
    """
    try:
        exact = colonization_planner.normalize_target(target)
    except ValueError:
        return None
    galaxy, system, position = (
        exact["galaxy"], exact["system"], exact["position"]
    )
    if (galaxy, system) in USER_EXCLUDE_SYSTEMS:
        return True
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n FROM planets WHERE galaxy = %s AND system = %s "
                "AND planet = %s AND planet_type = 1",
                (galaxy, system, position),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("target occupancy missing")
        return int(row["n"]) > 0
    except Exception:  # noqa: BLE001 - DB 장애 때만 galaxy HTTP 폴백
        try:
            rows = fleet.scan_galaxy(client, galaxy, system)
        except Exception:  # noqa: BLE001
            return None
        return any(int(row.get("position") or 0) == position for row in rows)


def _own_system(client, planets: list[dict]) -> tuple[int, int] | None:
    """현재 행성의 (galaxy, system). 목록의 current 행성 우선, 없으면 첫 행성."""
    if not planets:
        return None
    # DB 형식은 is_current, 기존 overview 폴백 형식은 current를 쓴다.
    cur = next(
        (p for p in planets if p.get("is_current") or p.get("current")),
        planets[0],
    )
    return (cur["galaxy"], cur["system"])


def _distributed_system_order(email: str, planet_count: int):
    """계정·확장 차수별 시작점을 달리해 1..499 전체를 빠짐없이 순회한다."""
    digest = hashlib.sha256(f"{email}:{planet_count}".encode("utf-8")).digest()
    start = int.from_bytes(digest[:4], "big") % MAX_SYSTEM
    # MAX_SYSTEM=499는 소수라 1..498 중 어느 보폭도 전체 시스템을 한 번씩 돈다.
    step = int.from_bytes(digest[4:8], "big") % (MAX_SYSTEM - 1) + 1
    for index in range(MAX_SYSTEM):
        yield (start + index * step) % MAX_SYSTEM + 1


def _list_planets(client, email: str) -> list[dict]:
    """행성 목록 DB 우선. DB가 실패/빈 결과일 때만 기존 overview HTML 폴백."""
    try:
        rows = db_reader.list_planets_db(email)
        if rows:
            return rows
    except Exception:  # noqa: BLE001 - DB 장애 때만 overview 폴백
        pass
    return planets_mod.list_planets(client)


def fleet_slot_available(
    email: str,
    levels: dict[int, int],
    *,
    reserve_slots: int = 0,
) -> bool:
    """식민 미션 1건을 보낸 뒤에도 reserve_slots를 남길 수 있는가.

    fleetsave의 서버 동일 슬롯 SQL/기본 상한을 재사용하고, 실제 클래스가 조회될 때는
    General +2를 더한다. 클래스 조회 실패는 +0의 보수 폴백이다. DB 장애면
    active_slots_in_use가 큰 sentinel을 반환하므로 자동 fail-closed다. raider는
    1슬롯을 raid/spy용으로 남겨 식민 확장이 정체성 미션을 봉쇄하지 않게 한다.
    """
    try:
        reserve = max(0, int(reserve_slots))
        maximum = fleetsave.fleet_slots_max(levels) + general_fleet_slot_bonus(email)
        used = fleetsave.active_slots_in_use(email)
        return used + 1 + reserve <= maximum
    except Exception:  # noqa: BLE001
        return False


def general_fleet_slot_bonus(email: str) -> int:
    """Return the real General fleet-slot bonus when account class is available."""
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute("SELECT character_class FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
        return (
            GENERAL_FLEET_SLOT_BONUS
            if row and int(row.get("character_class") or 0) == GENERAL_CLASS_ID
            else 0
        )
    except Exception:  # noqa: BLE001 - unknown class must not create imaginary slots
        return 0


def astro_needed_for_position(position: int) -> int:
    """Minimum astrophysics for edge positions; central 4..12 need no extra level."""
    pos = int(position)
    if pos in (1, 15):
        return 8
    if pos in (2, 14):
        return 6
    if pos in (3, 13):
        return 4
    return 0


def colonization_prerequisite_candidates(
    levels: Mapping[int, int],
    *,
    planet_count: int = 1,
    target_planets: int | None = None,
    target_position: int | None = None,
) -> list[tuple]:
    """Return the next queue-executor candidate for a colonization command.

    The result uses the existing candidate vocabulary: ``("research", tid)``
    or ``("ship", COLONY_SHIP, 1)``.  It does not execute either queue.  A
    blocked facility chain returns an empty list until the normal building
    planner supplies the facility.
    """
    desired_planets = (
        max(1, int(target_planets))
        if target_planets is not None
        else max(1, int(planet_count)) + 1
    )
    astro_target = astro_needed_for(desired_planets)
    if target_position is not None:
        astro_target = max(astro_target, astro_needed_for_position(target_position))
    if int(levels.get(ASTROPHYSICS, 0) or 0) < astro_target:
        step = next_research_step(dict(levels), astro_target)
        return [("research", step)] if step is not None else []
    if int(levels.get(COLONY_SHIP, 0) or 0) > 0:
        return []
    if (
        int(levels.get(SHIPYARD, 0) or 0) >= 4
        and int(levels.get(IMPULSE_DRIVE, 0) or 0) >= 3
    ):
        return [("ship", COLONY_SHIP, 1)]
    return []


def colonization_prerequisite_details(
    levels: Mapping[int, int],
    *,
    planet_count: int = 1,
) -> list[dict[str, Any]]:
    """Expose the complete costed chain for reservation/UI while candidates stay executable."""
    return colonization_planner.prerequisite_chain(
        levels, target_planets=max(1, int(planet_count)) + 1
    )


def _structured_outcome(
    plan: Mapping[str, Any] | None,
    status: str,
    reason: str,
    *,
    ok: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    source = plan if isinstance(plan, Mapping) else {}
    outcome = {
        "ok": ok,
        "status": status,
        "reason": reason,
        "command_id": source.get("command_id"),
        "phase": source.get("phase"),
        "exact_target": source.get("exact_target"),
        "role": source.get("role"),
    }
    outcome.update(extra)
    return outcome


def prepare_colonization(
    client,
    email: str,
    levels: Mapping[int, int],
    plan: Mapping[str, Any],
    *,
    max_planets: int = MAX_PLANETS,
    reserve_fleet_slots: int = 0,
    enforce_strategy_cap: bool = True,
    cooldown_sec: int = COLONIZE_COOLDOWN_SEC,
    check_target: bool = True,
) -> dict[str, Any]:
    """Validate and resume an exact structured plan without executing queue work.

    Missing research or a colony ship is returned as ``prerequisite_candidates``
    for the upstream arbiter.  This function never calls ``client.research`` or
    ``client.build_ships``.
    """
    if not isinstance(plan, Mapping):
        return _structured_outcome(None, "rejected", "invalid_plan")
    try:
        exact = colonization_planner.normalize_target(plan.get("exact_target"))
    except (TypeError, ValueError):
        return _structured_outcome(plan, "rejected", "invalid_exact_target")
    if not plan.get("command_id"):
        return _structured_outcome(plan, "rejected", "missing_command_id")
    expected_id = colonization_planner.deterministic_command_id(
        email, exact, str(plan.get("goal") or "colonize")
    )
    if plan.get("command_id") != expected_id:
        return _structured_outcome(
            plan, "rejected", "command_id_mismatch", expected_command_id=expected_id
        )
    if (exact["galaxy"], exact["system"]) in USER_EXCLUDE_SYSTEMS:
        return _structured_outcome(plan, "rejected", "target_excluded")

    planets = _list_planets(client, email)
    if not planets:
        return _structured_outcome(plan, "rejected", "planet_state_unknown")
    count = len(planets)
    # max_planets/enforce_strategy_cap은 호출 호환성만 유지한다. 실제 행성 수는 아래의
    # 천체물리 요구 레벨과 서버 도착 검증만으로 제한한다.
    _ = max_planets, enforce_strategy_cap

    astro_target = max(
        astro_needed_for(count + 1), astro_needed_for_position(exact["position"])
    )
    astro = int(levels.get(ASTROPHYSICS, 0) or 0)
    candidates = colonization_prerequisite_candidates(
        levels,
        planet_count=count,
        target_position=exact["position"],
    )
    if astro < astro_target:
        if not candidates:
            return _structured_outcome(
                plan,
                "waiting",
                "research_facility_prerequisite_missing",
                prerequisite_candidates=[],
                astrophysics_target=astro_target,
            )
        if research_in_progress(client, email):
            return _structured_outcome(
                plan,
                "waiting",
                "account_research_queue_busy",
                prerequisite_candidates=candidates,
                astrophysics_target=astro_target,
            )
        return _structured_outcome(
            plan,
            "waiting",
            "research_prerequisite",
            prerequisite_candidates=candidates,
            astrophysics_target=astro_target,
        )

    source_planet_id = plan.get("source_planet_id")
    if source_planet_id is None and isinstance(plan.get("source"), Mapping):
        source_planet_id = plan["source"].get("id")
    if int(levels.get(COLONY_SHIP, 0) or 0) <= 0:
        if not candidates:
            return _structured_outcome(
                plan,
                "waiting",
                "colony_ship_facility_prerequisite_missing",
                prerequisite_candidates=[],
            )
        if unit_queue_in_progress(email, planet_id=source_planet_id):
            return _structured_outcome(
                plan,
                "waiting",
                "planet_unit_queue_busy",
                prerequisite_candidates=candidates,
                source_planet_id=source_planet_id,
            )
        return _structured_outcome(
            plan,
            "waiting",
            "colony_ship_prerequisite",
            prerequisite_candidates=candidates,
            source_planet_id=source_planet_id,
        )

    if colonisation_in_flight(client, email):
        return _structured_outcome(plan, "waiting", "colonization_in_flight")
    if not fleet_slot_available(email, dict(levels), reserve_slots=reserve_fleet_slots):
        return _structured_outcome(plan, "waiting", "fleet_slot_unavailable")
    if check_target:
        occupied = target_occupancy(client, exact)
        if occupied is None:
            return _structured_outcome(plan, "rejected", "target_state_unknown")
        if occupied:
            return _structured_outcome(plan, "rejected", "target_occupied")

    key = f"{exact['galaxy']}:{exact['system']}:{exact['position']}"
    try:
        last_attempt = float(_load_state(email).get(key, 0.0))
        cooldown = max(0, int(cooldown_sec))
    except (TypeError, ValueError, OverflowError):
        return _structured_outcome(plan, "rejected", "invalid_cooldown")
    if time.time() - last_attempt < cooldown:
        return _structured_outcome(plan, "waiting", "target_cooldown")
    return _structured_outcome(
        plan,
        "ready",
        "ready_to_dispatch",
        ok=True,
        exact_target=exact,
        prerequisite_candidates=[],
        transfer_request=plan.get("transfer_request"),
    )


def execute_colonization_plan(
    client,
    email: str,
    levels: Mapping[int, int],
    plan: Mapping[str, Any],
    **prepare_options: Any,
) -> dict[str, Any]:
    """Dispatch one exact prepared command and return an explicit outcome dict."""
    options = dict(prepare_options)
    options["check_target"] = False
    prepared = prepare_colonization(client, email, levels, plan, **options)
    if prepared.get("status") != "ready":
        return prepared
    exact = prepared["exact_target"]

    # Last possible occupied-target check.  No nearby substitution is allowed.
    occupied = target_occupancy(client, exact)
    if occupied is None:
        return _structured_outcome(plan, "rejected", "target_state_unknown")
    if occupied:
        return _structured_outcome(plan, "rejected", "target_occupied")
    try:
        response = fleet.colonize(
            client, (exact["galaxy"], exact["system"], exact["position"])
        )
    except Exception as exc:  # noqa: BLE001 - structured callers receive, not raise
        return _structured_outcome(
            plan, "rejected", "dispatch_error", error=f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(response, Mapping) or not response.get("ok"):
        reason = "dispatch_rejected"
        if isinstance(response, Mapping):
            reason = str(response.get("error") or response.get("message") or reason)
        return _structured_outcome(
            plan, "rejected", reason, fleet_response=dict(response or {})
        )

    key = f"{exact['galaxy']}:{exact['system']}:{exact['position']}"
    state = _load_state(email)
    state[key] = time.time()
    _save_state(email, state)
    return _structured_outcome(
        plan,
        "dispatched",
        "colonization_dispatched",
        ok=True,
        exact_target=exact,
        fleet_response=dict(response),
        transfer_request=plan.get("transfer_request"),
    )


prerequisite_candidates = colonization_prerequisite_candidates
prepare_structured_colonization = prepare_colonization
execute_structured_colonization = execute_colonization_plan


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def colony_tick(
    client,
    email: str,
    levels: dict[int, int],
    *,
    max_planets: int = MAX_PLANETS,
    reserve_fleet_slots: int = 0,
    enforce_strategy_cap: bool = True,
    scan_span: int = SCAN_SPAN,
    cooldown_sec: int = COLONIZE_COOLDOWN_SEC,
) -> str | None:
    """식민 확장 행동 1건. 로그 문자열 또는 None(행동 없음) 반환.

    levels 는 miner_bot 이 이미 조회한 건물/시설/연구/함선 수량 dict.
    이 함수는 어떤 경우에도 예외를 올리지 않는다(루프 보호).

    miner_bot 훅 연결 시 주의(raid_tick 과 동일 시그니처):
      note = colony.colony_tick(client, email, levels)
      if note: done.append(note)
    """
    try:
        return _colony_tick_inner(
            client,
            email,
            levels,
            max_planets=max_planets,
            reserve_fleet_slots=reserve_fleet_slots,
            enforce_strategy_cap=enforce_strategy_cap,
            scan_span=scan_span,
            cooldown_sec=cooldown_sec,
        )
    except Exception as exc:  # noqa: BLE001 - 식민 버그가 빌드틱/루프를 깨지 않게
        return f"colony err {type(exc).__name__}: {exc}"


def _colony_tick_inner(
    client,
    email: str,
    levels: dict[int, int],
    *,
    max_planets: int = MAX_PLANETS,
    reserve_fleet_slots: int = 0,
    enforce_strategy_cap: bool = True,
    scan_span: int = SCAN_SPAN,
    cooldown_sec: int = COLONIZE_COOLDOWN_SEC,
) -> str | None:
    planets = _list_planets(client, email)
    if not planets:
        return "colony idle (행성 목록 조회 실패)"  # 수 미상에서 확장하지 않음
    count = len(planets)

    # max_planets/enforce_strategy_cap은 호출 호환성만 유지한다. 인공 행성 상한은 없고
    # 다음 행성에 필요한 천체물리 레벨만 아래에서 검사한다.
    _ = max_planets, enforce_strategy_cap

    target = count + 1
    astro = levels.get(ASTROPHYSICS, 0)
    needed = astro_needed_for(target)

    # 2) 천체물리가 부족하면: 선행 연구 → 천체물리 (연구 큐가 빌 때만 1건)
    if astro < needed:
        step = next_research_step(levels, astro_target=needed)
        if step is None:
            return None  # 선행 시설(연구소 등) 부족 → miner 가 올릴 때까지 대기
        if research_in_progress(client, email):
            return None  # 큐가 차 있으면 이번 틱은 양보
        res = client.research(step)
        if _ok(res):
            return f"colony RESEARCH {_name(step)}({step}) → astro {needed} 목표"
        return None  # 자원 부족/큐 참 등 → 다음 틱 재시도

    # 여기부터 astro 는 다음 행성을 받기에 충분하다.

    # 이미 식민 미션이 날아가는 중이면 중복 파견 방지
    if colonisation_in_flight(client, email):
        return "colony idle (식민 미션 진행중)"

    # 3) 식민선이 없으면 건조 (조선소4·impulse3 필요; impulse 는 astro 선행이라 이미 충족)
    have_colony = levels.get(COLONY_SHIP, 0)
    if have_colony <= 0:
        if levels.get(SHIPYARD, 0) >= 4 and levels.get(IMPULSE_DRIVE, 0) >= 3:
            if unit_queue_in_progress(email):
                return None  # raid 리사이클러/정체성 함선 큐에 식민선을 덧붙이지 않음
            res = client.build_ships(COLONY_SHIP, 1)
            if _ok(res):
                return "colony BUILD 식민선(208)x1"
        return None  # 조선소 미달 → miner 가 올릴 때까지 대기

    # 4) 식민선 보유 → 슬롯을 먼저 확인한 뒤 빈 슬롯 탐색/HTTP 파견.
    #    슬롯이 없으면 은하 스캔조차 하지 않아 raid/spy와 부하를 동시에 보호한다.
    if not fleet_slot_available(email, levels, reserve_slots=reserve_fleet_slots):
        return "colony idle (함대 슬롯 보류)"

    own = _own_system(client, planets)
    if own is None:
        return "colony idle (자기 시스템 조회 실패)"
    galaxy, system = own

    now = time.time()
    state = _load_state(email)
    _ = scan_span  # 이전 근거리 스캔 설정은 API 호환성만 유지한다.
    configured_cooldown = max(0, int(cooldown_sec))
    blocked: set[tuple[int, int, int]] = set()
    for key, attempted_at in state.items():
        if now - float(attempted_at) >= configured_cooldown:
            continue
        try:
            blocked.add(tuple(int(part) for part in key.split(":")))
        except (TypeError, ValueError):
            continue
    slot = find_empty_slot_across_systems(
        client,
        galaxy,
        _distributed_system_order(email, count),
        astro=astro,
        blocked=blocked,
    )
    if slot is None:
        return "colony idle (빈 슬롯 없음)"
    key = f"{slot[0]}:{slot[1]}:{slot[2]}"
    # 요청 전에 기록해 겹친 틱과 응답 해석 오류가 같은 좌표를 연속 파견하지 않게 한다.
    state[key] = now
    _save_state(email, state)
    res = fleet.colonize(client, slot)
    if res.get("ok"):
        return f"colony COLONIZE {key} (식민선 파견)"
    return f"colony idle (식민 거부 {key})"


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _ok(res) -> bool:
    """add-buildrequest 계열 응답 성공 판정 (miner_bot._is_success 와 동일 규칙)."""
    if not isinstance(res, dict):
        return False
    return res.get("status") == "success" or res.get("success") is True


_NAMES = {
    ENERGY_TECHNOLOGY: "energy_technology",
    IMPULSE_DRIVE: "impulse_drive",
    ESPIONAGE_TECHNOLOGY: "espionage_technology",
    ASTROPHYSICS: "astrophysics",
}


def _name(tid: int) -> str:
    return _NAMES.get(tid, str(tid))
