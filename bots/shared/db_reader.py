"""OGameX 봇 상태 조회 — 읽기 전용 DB 직독 (HTML 파싱 대체).

client.get_status() / get_levels() 는 봇당 매 틱 2~5회 HTTP 요청(overview + 각
인게임 페이지 렌더)을 일으켜 바퀴 무게의 대부분을 차지한다. 이 모듈은 **조회만**
MariaDB 직독으로 대체해 그 부하를 없앤다. 행동(건설/연구/함선 POST)은 HTTP로
유지하고, 도착 함대가 있을 때만 조건부 overview 요청으로 지연 정산한다.

## 지연 정산(lazy settlement)과 조건부 overview guard
OGameX 는 자원 누적·건물 완성 정산을 **인증된 요청이 들어올 때** 처리한다:
  ogamex/app/Http/Middleware/GlobalGame.php 가 `$player->update()` 와
  `$player->planets->current()->update()` 를 호출 → updateBuildingQueue +
  updateResources + updateUnitQueue + 생산/저장 스탯 갱신.
이 미들웨어는 `globalgame` 그룹에 속하고, 봇의 액션 POST 경로
(/resources/add-buildrequest 등, routes/web.php:67 그룹)가 모두 이 그룹 안에 있다.
일반 액션 POST는 현재 행성을 정산하므로 조회용 overview는 필요 없다. 다만 유휴 틱이나
지속 세션에서도 도착 함대 정산은 놓치면 안 된다. `has_due_fleet_missions_db()`가 계정의
도착·귀환·홀딩 완료 함대를 읽기 전용으로 감지하고, `run_universe.py`가 due일 때만
overview를 한 번 요청한다. DB 감지 실패나 GET 실패는 행동 후보를 막지 않는 best-effort다.

DB 값은 "마지막 정산 시점"의 것이라 다음 두 보정을 적용한다(서버 공식 그대로 복제):
  1) 자원: metal + metal_production/3600 × (now − time_last_update), 저장소(metal_max)
     상한. energy = energy_max − energy_used (시간 무관).
     근거: ogamex/app/Services/PlanetService.php updateResourcesUntil(), energy().
  2) 건물/연구 레벨: 아직 미정산인 완료 큐 항목을 레벨에 가산.
     retrieveFinished 조건(BuildingQueueService.php:35): building=1, processed=0,
     canceled=0, time_end ≤ now → object_level_target 을 반영.

## 알려진 한계
- 함선/방어 수량(unit_queues)은 미정산 진행분을 반영하지 않는다(정산분만). 봇이 매 틱
  건조 POST 를 하면 다음 틱엔 반영되므로 최대 1틱 지연. 봇 판단(보유 여부)에는 영향 미미.
- 현재 행성이 아닌 행성의 레벨/자원도 같은 공식으로 추정하지만, 그 행성에 대한 활성
  건물 큐 완성만 보정된다(정상).
"""
from __future__ import annotations

import os
import threading
import time

import pymysql

from bots.shared.game_objects import ID_TO_NAME, RESEARCH, SHIPS, SOLAR_SATELLITE

# 읽기 전용 계정(SELECT 권한만). 실수로도 쓰기가 불가능하도록 별도 유저를 쓴다.
DB_HOST = os.environ.get("OGAME_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("OGAME_DB_PORT", "3306"))
DB_USER = os.environ.get("OGAME_DB_USER", "botread")
DB_PASSWORD = os.environ.get("OGAME_DB_PASSWORD", "botread")
DB_NAME = os.environ.get("OGAME_DB_NAME", "laravel")

# 현재 run_universe 워치독은 서버 여유를 둔 ThreadPoolExecutor(--parallel 6)로 호출한다.
# pymysql 커넥션은 스레드 간 공유가 안전하지 않으므로 스레드별 커넥션을 재사용한다
# (재로그인 비용 절약과 같은 취지). 커넥션이 끊기면 다음 호출에서 재연결한다.
_local = threading.local()


def _connect() -> "pymysql.connections.Connection":
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        connect_timeout=5,
        read_timeout=5,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _conn() -> "pymysql.connections.Connection":
    """스레드-로컬 커넥션 확보(끊겼으면 재연결)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = _connect()
        return conn
    try:
        conn.ping(reconnect=False)
    except Exception:  # noqa: BLE001 - 끊긴 커넥션이면 재연결(실패 시 예외 → 호출부 HTTP 폴백)
        conn = _local.conn = _connect()
    return conn


# --- 지연 정산이 필요한 함대 감지 -----------------------------------------

def has_due_fleet_missions_db(email: str, now_ts: int | None = None) -> bool:
    """Return whether an authenticated request has fleet work to settle.

    This is a read-only equivalent of
    ``FleetMissionService::getArrivedMissionsByPlanetIds()``.  OGameX processes
    missions lazily inside ``GlobalGame``; the persistent bot sessions otherwise
    have no reason to render ``/overview`` on an idle tick.

    The two due-time branches intentionally mirror the server's hold semantics:

    * outbound ACS Defend (type 5) is due at physical arrival, represented by
      ``time_arrival - time_holding``;
    * every other mission with a hold is due after
      ``time_arrival + time_holding``.

    Missions touching *any* planet owned by the account are included, matching
    ``PlayerService::updateFleetMissions()``.  Canceled missions are not filtered
    here because the server query also relies on ``processed`` rather than a
    separate canceled predicate.
    """
    current_time = int(time.time()) if now_ts is None else int(now_ts)
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM fleet_missions fm "
            "JOIN planets p ON (p.id = fm.planet_id_from OR p.id = fm.planet_id_to) "
            "JOIN users u ON u.id = p.user_id "
            "WHERE u.email = %s AND fm.processed = 0 AND ("
            "(fm.mission_type = 5 AND fm.parent_id IS NULL "
            "AND fm.time_arrival - COALESCE(fm.time_holding, 0) <= %s) "
            "OR ((fm.mission_type <> 5 OR fm.parent_id IS NOT NULL) "
            "AND fm.time_arrival + COALESCE(fm.time_holding, 0) <= %s)"
            ") LIMIT 1"
            ") AS has_due",
            (email, current_time, current_time),
        )
        row = cur.fetchone()
    return bool(int((row or {}).get("has_due") or 0))


# --- 자원 현황 -------------------------------------------------------------

_STATUS_COLS = (
    "id, metal, crystal, deuterium, "
    "metal_production, crystal_production, deuterium_production, "
    "metal_max, crystal_max, deuterium_max, "
    "energy_max, energy_used, temp_max, time_last_update"
)


def _resolve_planet_row(cur, email: str, planet_id: int | None, cols: str) -> dict | None:
    """email(현재 행성) 또는 planet_id 로 planets 행 1건을 읽어 반환.

    cols 의 컬럼명은 planets 를 alias `p` 로 참조한다고 가정한다(JOIN 시 `id`
    모호성 회피). 호출부는 p. 접두 없이 컬럼명만 넘기면 된다.
    """
    select = ", ".join(f"p.{c.strip()}" for c in cols.split(","))
    if planet_id is not None:
        cur.execute(f"SELECT {select} FROM planets p WHERE p.id = %s", (planet_id,))
        return cur.fetchone()
    cur.execute(
        f"SELECT {select} FROM planets p "
        "JOIN users u ON u.planet_current = p.id "
        "WHERE u.email = %s",
        (email,),
    )
    return cur.fetchone()


def _estimate_resource(current: float, production: float, mx: float, elapsed_h: float) -> int:
    """서버 updateResourcesUntil() 공식 복제: 저장소 상한 내에서 생산분 가산.

    서버는 current < max 일 때만 생산분을 더하고, max 를 넘지 않게 자른다.
    """
    if current < mx:
        current = current + production * elapsed_h
        if current > mx:
            current = mx
    return int(current)


def get_status_db(email: str, planet_id: int | None = None) -> dict:
    """현재(또는 지정) 행성의 자원 현황. client.get_status() 와 동일 형식.

    반환: {"metal": int, "crystal": int, "deuterium": int, "energy": int}
    (get_status() 의 page_title 은 조회부에서 아무도 쓰지 않으므로 제외.)
    """
    conn = _conn()
    with conn.cursor() as cur:
        row = _resolve_planet_row(cur, email, planet_id, _STATUS_COLS)
    if row is None:
        raise RuntimeError(f"planet not found for email={email!r} planet_id={planet_id!r}")

    now = int(time.time())
    elapsed_h = max(0, now - int(row["time_last_update"])) / 3600.0
    return {
        "metal": _estimate_resource(
            row["metal"], row["metal_production"], row["metal_max"], elapsed_h
        ),
        "crystal": _estimate_resource(
            row["crystal"], row["crystal_production"], row["crystal_max"], elapsed_h
        ),
        "deuterium": _estimate_resource(
            row["deuterium"], row["deuterium_production"], row["deuterium_max"], elapsed_h
        ),
        "energy": int(row["energy_max"] - row["energy_used"]),
    }


# 자원 현황만 필요한 turtle.read_resources 호환용 별칭.
get_resources_db = get_status_db


def get_resource_state_db(email: str, planet_id: int | None = None) -> dict:
    """현재(또는 지정) 행성의 자원 현황 + **저장 용량 상한**을 함께 반환.

    get_status_db 는 자원/에너지만 준다. 저장고 전략(plan_primary)은 자원이 창고
    용량(planets.*_max)에 얼마나 근접했는지 판단해야 하므로, 여기서 상한도 같이 읽는다.

    반환: {"planet_id",                              # 명시적 배치 액션의 대상 행성
           "metal","crystal","deuterium","energy",  # 현재값(생산 보정 포함)
           "metal_max","crystal_max","deuterium_max",  # 저장 용량 상한
           "temp_max"}                                  # 행성 최고온도(태양광 위성 효율)
    갓 시드된(미정산) 행성은 *_max 가 0 일 수 있다 — 호출부는 0 이면 "용량 미상"으로
    다뤄 저장고 판정을 건너뛴다(0 나눗셈/오판 방지). temp_max 는 에너지 규칙(energy.py)이
    태양광 위성 1기당 생산량(floor((temp_max+140)/6))을 구하는 데 쓴다.
    """
    conn = _conn()
    with conn.cursor() as cur:
        row = _resolve_planet_row(cur, email, planet_id, _STATUS_COLS)
    if row is None:
        raise RuntimeError(f"planet not found for email={email!r} planet_id={planet_id!r}")

    now = int(time.time())
    elapsed_h = max(0, now - int(row["time_last_update"])) / 3600.0
    return {
        # 배치 경로는 users.planet_current 를 바꾸지 않고 행성을 명시한다.
        # 이미 상태 스냅샷에 포함된 id를 돌려주면 별도 DB/overview
        # 조회 없이 이 요청의 정확한 대상을 재사용할 수 있다.
        "planet_id": int(row["id"]),
        "metal": _estimate_resource(
            row["metal"], row["metal_production"], row["metal_max"], elapsed_h
        ),
        "crystal": _estimate_resource(
            row["crystal"], row["crystal_production"], row["crystal_max"], elapsed_h
        ),
        "deuterium": _estimate_resource(
            row["deuterium"], row["deuterium_production"], row["deuterium_max"], elapsed_h
        ),
        "energy": int(row["energy_max"] - row["energy_used"]),
        "metal_max": int(row["metal_max"]),
        "crystal_max": int(row["crystal_max"]),
        "deuterium_max": int(row["deuterium_max"]),
        "temp_max": int(row["temp_max"]) if row["temp_max"] is not None else 0,
        # 계정 전체 LLM 관측에서 에너지 적자가 실제 생산을 얼마나 깎는지 0과 결측을
        # 구분해 보여준다. 기존 소비자는 추가 키를 무시하므로 하위호환이다.
        "energy_max": int(row["energy_max"]),
        "energy_used": int(row["energy_used"]),
        "production_factor_pct": (
            100
            if int(row["energy_used"]) <= 0
            else max(
                0,
                min(100, int(100 * int(row["energy_max"]) / int(row["energy_used"]))),
            )
        ),
    }


# --- 시간당 생산량 (일일 생산 가치 산정) -----------------------------------

_PRODUCTION_COLS = "metal_production, crystal_production, deuterium_production"


def get_production_per_hour_db(
    email: str, planet_id: int | None = None
) -> dict[str, float]:
    """현재(또는 지정) 행성의 시간당 자원 생산량(서버 정산값)을 반환한다.

    planets.{metal,crystal,deuterium}_production 은 서버가 마지막 정산 때 계산한 시간당
    생산량으로, 우주 배속·플라즈마·클래스 보너스가 이미 반영돼 있다(get_status_db 가 자원
    추정에 쓰는 값과 동일한 컬럼). 광산 레벨 공식으로 재계산하면 배속/보너스를 추측해야
    하므로, 이 정산값을 그대로 읽어 일일 생산 가치를 정확히 산정한다.

    반환: {"metal","crystal","deuterium"} (시간당, float). 채굴러 최소 방어선
    (rule_based.miner_defense)이 "약탈 가치 = 일일 생산 가치" 대비 방어 투자를 맞추는 데 쓴다.
    """
    conn = _conn()
    with conn.cursor() as cur:
        row = _resolve_planet_row(cur, email, planet_id, _PRODUCTION_COLS)
    if row is None:
        raise RuntimeError(f"planet not found for email={email!r} planet_id={planet_id!r}")
    return {
        "metal": float(row["metal_production"] or 0.0),
        "crystal": float(row["crystal_production"] or 0.0),
        "deuterium": float(row["deuterium_production"] or 0.0),
    }


# --- 행성 목록 (다중 행성 지원) --------------------------------------------

def list_planets_db(email: str) -> list[dict]:
    """계정의 행성 목록(달 제외)을 DB 에서 읽어 반환. 식민지 개발 순회용.

    planets_mod.list_planets(client) 는 overview HTML 을 한 번 더 받아야 하지만,
    이 함수는 액션 POST 로 이미 정산되는 DB 만 읽어 HTTP 를 늘리지 않는다.
    planet_type=1(행성)만 본다 — 달(planet_type=3)은 자원 생산이 없어 개발 대상이 아니다.

    각 항목(dict):
      id         : planet_id (int) — switch_planet / ?cp= 에 사용
      name       : 행성 이름 (str)
      galaxy, system, position : 좌표 (int). position 은 planets.planet 컬럼.
      is_current : 이 행성이 현재 선택된 행성(users.planet_current)인지 (bool)

    홈월드(현재 행성)와 식민지를 구분하는 서버 플래그는 없다(둘 다 planet_type=1).
    호출부는 is_current 로 "지금 봇이 서 있는 행성"을 홈 앵커로 삼고 나머지를 식민지로 본다.
    id 오름차순 정렬(식민 순서가 결정적이도록).
    """
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.name, p.galaxy, p.system, p.planet AS position, "
            "(p.id = u.planet_current) AS is_current "
            "FROM planets p JOIN users u ON u.id = p.user_id "
            "WHERE u.email = %s AND p.planet_type = 1 "
            "ORDER BY p.id",
            (email,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "name": r["name"] or "",
            "galaxy": int(r["galaxy"]),
            "system": int(r["system"]),
            "position": int(r["position"]),
            "is_current": bool(r["is_current"]),
        }
        for r in rows
    ]


# --- 큐 항목 생존 확인 (성공 판정 강화용) ----------------------------------

def latest_queue_canceled(
    email: str, object_id: int, kind: str, planet_id: int | None = None
) -> int | None:
    """방금 add 한 큐 항목이 서버에서 취소됐는지 확인한다.

    OGameX 의 건설/연구 add 는 자원을 검사하지 않고 항목을 큐에 넣은 뒤(같은 요청 안에서)
    start() 로 '시작'을 시도한다 — 시작 시 자원(창고 용량 상한 포함) 부족이면 그 항목만
    canceled=1 로 조용히 취소한다. 따라서 HTTP success 만으로는 실제 건설을 보장할 수 없다.
    이 함수로 add 직후 **내 항목(가장 최근 row)** 의 canceled 플래그를 재조회해 진짜 성공을
    판정한다.

    매칭: planet_id(현재 행성) + object_id 로 가장 최근(id 최대) row 를 본다. add 는 항상
    새 row 를 넣으므로 그게 방금 내가 넣은 항목이다.
    kind: "building"(건설/시설, building_queues) 또는 "research"(연구, research_queues).

    반환: 취소됨=1, 살아있음(대기/진행/완료)=0, 해당 row 없음=None.
    """
    conn = _conn()
    with conn.cursor() as cur:
        if kind == "research":
            # 연구는 유저 단위 — 유저의 어느 행성에서 넣었든 가장 최근 항목을 본다.
            if planet_id is not None:
                cur.execute(
                    "SELECT canceled FROM research_queues "
                    "WHERE planet_id = %s AND object_id = %s ORDER BY id DESC LIMIT 1",
                    (planet_id, object_id),
                )
            else:
                cur.execute(
                    "SELECT rq.canceled FROM research_queues rq "
                    "JOIN planets p ON p.id = rq.planet_id "
                    "JOIN users u ON u.id = p.user_id "
                    "WHERE u.email = %s AND rq.object_id = %s "
                    "ORDER BY rq.id DESC LIMIT 1",
                    (email, object_id),
                )
        else:
            if planet_id is not None:
                cur.execute(
                    "SELECT canceled FROM building_queues "
                    "WHERE planet_id = %s AND object_id = %s ORDER BY id DESC LIMIT 1",
                    (planet_id, object_id),
                )
            else:
                cur.execute(
                    "SELECT bq.canceled FROM building_queues bq "
                    "JOIN planets p ON p.id = bq.planet_id "
                    "JOIN users u ON u.planet_current = p.id "
                    "WHERE u.email = %s AND bq.object_id = %s "
                    "ORDER BY bq.id DESC LIMIT 1",
                    (email, object_id),
                )
        row = cur.fetchone()
    if row is None:
        return None
    return int(row["canceled"])


def active_build_targets(email: str, planet_id: int | None = None) -> set[int]:
    """현재 행성의 건설 큐에 살아있는(canceled=0, processed=0) 항목의 object_id 집합.

    저장고 중복 큐잉 방지에 쓴다(이미 짓는 중인 저장고를 매 틱 또 넣지 않도록).
    """
    conn = _conn()
    with conn.cursor() as cur:
        if planet_id is not None:
            cur.execute(
                "SELECT DISTINCT object_id FROM building_queues "
                "WHERE planet_id = %s AND canceled = 0 AND processed = 0",
                (planet_id,),
            )
        else:
            cur.execute(
                "SELECT DISTINCT bq.object_id FROM building_queues bq "
                "JOIN planets p ON p.id = bq.planet_id "
                "JOIN users u ON u.planet_current = p.id "
                "WHERE u.email = %s AND bq.canceled = 0 AND bq.processed = 0",
                (email,),
            )
        return {int(r["object_id"]) for r in cur.fetchall()}


# --- 레벨/수량 조회 --------------------------------------------------------

# planets 테이블에서 읽을 컬럼: 건물·시설·함선·방어(연구는 users_tech). machine_name 이
# 곧 컬럼명이므로 ID_TO_NAME 에서 연구를 뺀 나머지 이름들이 그대로 컬럼이다.
_PLANET_LEVEL_NAMES = tuple(
    name for tid, name in ID_TO_NAME.items() if tid not in RESEARCH
)
_RESEARCH_NAMES = tuple(RESEARCH.values())
_NAME_TO_ID = {name: tid for tid, name in ID_TO_NAME.items()}


def get_levels_db(email: str, planet_id: int | None = None) -> dict[int, int]:
    """건물/시설/연구/함선/방어 레벨·수량을 technologyId → 값 dict 로 반환.

    client.get_levels() 와 동일 형식(technologyId 키). 지연 정산 보정 포함.
    """
    conn = _conn()
    with conn.cursor() as cur:
        # 1) 행성 행(건물/시설/함선/방어) — user_id 도 함께 읽어 연구/큐 조회에 쓴다.
        cols = "id, user_id, " + ", ".join(_PLANET_LEVEL_NAMES)
        row = _resolve_planet_row(cur, email, planet_id, cols)
        if row is None:
            raise RuntimeError(f"planet not found for email={email!r} planet_id={planet_id!r}")
        pid = row["id"]
        uid = row["user_id"]

        levels: dict[int, int] = {}
        for name in _PLANET_LEVEL_NAMES:
            levels[_NAME_TO_ID[name]] = int(row[name])

        # 2) 연구 레벨(users_tech, 유저 단위).
        cur.execute(
            "SELECT " + ", ".join(_RESEARCH_NAMES) + " FROM users_tech WHERE user_id = %s",
            (uid,),
        )
        tech = cur.fetchone()
        if tech:
            for name in _RESEARCH_NAMES:
                levels[_NAME_TO_ID[name]] = int(tech[name])

        now = int(time.time())
        # 3) 미정산 완료 건물/시설 큐 → 레벨 가산(retrieveFinished 조건 그대로).
        cur.execute(
            "SELECT object_id, object_level_target FROM building_queues "
            "WHERE planet_id = %s AND building = 1 AND processed = 0 AND canceled = 0 "
            "AND time_end <= %s",
            (pid, now),
        )
        for q in cur.fetchall():
            oid = int(q["object_id"])
            levels[oid] = max(levels.get(oid, 0), int(q["object_level_target"]))

        # 4) 미정산 완료 연구 큐 → 레벨 가산. 연구는 유저 단위이므로 유저의 모든 행성에서
        #    진행 중인 활성 연구를 본다(현재 행성이 아닌 곳에서 연구 중일 수 있다).
        cur.execute(
            "SELECT rq.object_id, rq.object_level_target FROM research_queues rq "
            "JOIN planets p ON p.id = rq.planet_id "
            "WHERE p.user_id = %s AND rq.building = 1 AND rq.processed = 0 "
            "AND rq.canceled = 0 AND rq.time_end <= %s",
            (uid, now),
        )
        for q in cur.fetchall():
            oid = int(q["object_id"])
            levels[oid] = max(levels.get(oid, 0), int(q["object_level_target"]))

    return levels


def get_account_ship_totals_db(email: str) -> dict[int, int]:
    """계정 전체의 실제/약정 함선 수량을 한 스냅샷으로 합산한다.

    ``get_levels_db``의 함선 수량은 현재 행성 주기장만 나타낸다. 성장 커리큘럼과 AI
    ``ship_priority.max_total``은 계정 단위 상한이므로 다음 세 위치를 모두 더해야 한다.

    * 소유한 모든 행성/달의 주기장
    * 아직 처리되지 않은 비행 함대(``fleet_missions``)
    * 모든 행성의 미완료 조선소 큐

    unit queue의 ``object_amount_progress``만큼은 이미 행성 주기장에 지급됐으므로 남은
    ``object_amount - object_amount_progress``만 더한다. 세 원천은 하나의 SELECT 문에
    넣어 같은 DB 스냅샷에서 읽는다. 귀환 정산 경계에서 행성과 비행 함대를 서로 다른
    시점에 읽어 함선을 누락하는 일을 피하기 위해서다.

    조회 실패나 계정/행성 부재는 예외로 알린다. 호출부는 이를 함선 건조만 fail-closed로
    멈추는 신호로 쓰며, 건설/연구 등 나머지 성장 틱은 계속 진행한다.
    """
    ship_ids = tuple(SHIPS)
    # 태양광 위성은 비행할 수 없어 fleet_missions 컬럼이 없다. 현재 스키마에는 나머지
    # 함선(클래스 함선 포함)이 모두 machine_name 컬럼으로 존재한다.
    fleet_ship_ids = tuple(tid for tid in ship_ids if tid != SOLAR_SATELLITE)

    parked_columns = ", ".join(
        f"COALESCE(SUM(p.{SHIPS[tid]}),0) AS parked_{tid}" for tid in ship_ids
    )
    flying_columns = ", ".join(
        f"COALESCE(SUM(fm.{SHIPS[tid]}),0) AS flying_{tid}"
        for tid in fleet_ship_ids
    )
    queued_columns = ", ".join(
        "COALESCE(SUM(CASE WHEN uq.object_id="
        f"{tid} THEN GREATEST(uq.object_amount-uq.object_amount_progress,0) "
        f"ELSE 0 END),0) AS queued_{tid}"
        for tid in ship_ids
    )
    total_columns = []
    for tid in ship_ids:
        parts = [f"parked.parked_{tid}", f"queued.queued_{tid}"]
        if tid in fleet_ship_ids:
            parts.insert(1, f"flying.flying_{tid}")
        total_columns.append(" + ".join(parts) + f" AS total_{tid}")

    sql = (
        "SELECT parked.planet_count, " + ", ".join(total_columns) + " FROM "
        "(SELECT COUNT(p.id) AS planet_count, " + parked_columns + " "
        " FROM planets p JOIN users u ON u.id=p.user_id WHERE u.email=%s) parked "
        "CROSS JOIN "
        "(SELECT " + flying_columns + " FROM fleet_missions fm "
        " JOIN users u ON u.id=fm.user_id WHERE u.email=%s "
        " AND fm.processed=0 AND fm.canceled=0) flying "
        "CROSS JOIN "
        "(SELECT " + queued_columns + " FROM unit_queues uq "
        " JOIN planets p ON p.id=uq.planet_id JOIN users u ON u.id=p.user_id "
        " WHERE u.email=%s AND uq.processed=0) queued"
    )

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(sql, (email, email, email))
        row = cur.fetchone()
    if row is None or int(row.get("planet_count") or 0) < 1:
        raise RuntimeError(f"account ships not found for email={email!r}")
    return {
        tid: max(0, int(row.get(f"total_{tid}") or 0))
        for tid in ship_ids
    }


if __name__ == "__main__":
    import sys

    e = sys.argv[1] if len(sys.argv) > 1 else "bot001@bots.local"
    print("status:", get_status_db(e))
    lv = get_levels_db(e)
    from bots.shared.game_objects import name_of

    print("levels:", {name_of(k): v for k, v in sorted(lv.items()) if v})
