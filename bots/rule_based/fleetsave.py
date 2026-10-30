"""플릿세이브(fleet save) — 주기장에 방치되는 함대를 저위험 왕복 미션으로 대피.

## 왜 (배경 — 전투 시뮬 스위치온의 선행조건)
전투 시뮬레이터가 켜지면 레이더 함대가 커지고, 그 순간 **주기장(홈월드)에 파킹된 함대**가
서로의 최대 먹잇감이 된다(OGame 에서 함대 손실의 대부분은 파킹 중 피격). 함대 성장 온
전에 플릿세이브가 먼저 있어야 한다. 이 모듈이 그 대피 루틴을 맡는다.

## 핵심 원리
비행 중(in-flight)인 함대는 **공격 대상이 될 수 없다**(서버는 도착한 파킹 함대만 전투에
넣는다). 따라서 "위험 잉여 함대"를 되돌아오는 미션으로 계속 띄워 두면, 공격이 도착하는
순간 함대가 하늘에 있어 살아남는다. 봇은 매 틱(≈4분) 다시 평가해 재세이브한다.

## 미션 선택 (ogamex 소스 실측)
  - Transport(3)는 **왕복 미션**(hasReturnMission=true): 도착지에 자원을 내리고 출발지로
    자동 귀환한다. 자기 행성/달 또는 (휴가 아닌) 남의 행성으로 보낼 수 있다.
  - Deployment(4)는 **편도**(hasReturnMission=false): 도착지에 함대가 눌러앉아 다시 노출
    되므로 세이브가 아니다. 그래서 쓰지 않는다.
  → 세이브 = **화물 0 의 수송 왕복**. 목적지:
      1순위: 자기 식민지(list_planets_db 의 비현재 행성) — 가장 안전(자기 소유, 0 배달).
      2순위: 명시적으로 허용한 비활성 봇 행성 — OGameX TransportMission 은
             **0자원이어도 상대에게 TransportReceived 메시지를 생성**하므로 기본
             allowlist 는 비워 둔다. 단일행성 계정은 허용된 목적지가 없으면 스킵한다.

## 귀환 타이밍 (속도)
느린 속도(FLEETSAVE_SPEED=10%)로 보내 **체공 시간을 최대화**(=노출 최소화)하고 연료도
아낀다. 목적지를 가까운 곳으로 잡아 왕복이 지나치게 길어지지 않게 한다. 정밀한 "다음 틱에
딱 맞춰 귀환"은 서버 fleetSpeed·함선별 추진속도로 duration 을 역산해야 하는데(후속 정밀화),
느린 속도+근거리 목적지로 실용적 근사를 쓴다. 파킹된 채로 있는 시간이 짧을수록 좋다.

## 슬롯 경쟁
함대 슬롯 상한 = 1 + computer_technology(+General 클래스 시 +2, 여기선 보수적으로 무시).
사용 중 = 진행 중(processed=0) 미션 수(미사일 type10 제외 — 서버 getFleetSlotsInUse 동일).
슬롯이 꽉 차면 이번 틱은 세이브를 쉰다. 원정(explorer)·약탈(raider)이 이미 슬롯을 쓰면
그 뒤 잔여 슬롯으로만 세이브 → 정체성 미션과 경쟁하지 않는다(호출 순서로도 보장).
계정당 동시 세이브는 1개로 제한한다. 매 틱 새로 건조된 소량 함선을 또 띄우면 이전
세이브가 귀환하기 전에 슬롯이 세이브로 채워져, 다음 틱 원정/약탈 슬롯을 역으로 굶길 수
있기 때문이다. 이미 떠 있는 세이브가 있으면 새 함선은 그 귀환 뒤 한 번에 합류시킨다.

## 안전장치
  - 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호).
  - explorer는 다음 원정 1회 최소 편성을 주기장에 남기고, 그 예약을 넘는 고가 함대만
    세이브한다. 플릿세이브가 원정 핵심 함대를 전량 선점하지 않는다.
  - 파킹 함대는 **틱 시작 스냅샷이 아니라 재조회(get_levels_db)** 로 읽어, 이번 틱에 이미
    약탈/원정으로 떠난 함선과 이미 세이브 중인 함선을 자동 제외한다(중복 세이브 방지).
  - 외부 목적지는 FOREIGN_TARGET_ALLOWLIST 좌표와 일치하는 비활성 계정만
    허용. 사람 계정 시스템(USER_EXCLUDE_SYSTEMS)은 allowlist 여부와 관계없이 제외.
"""
from __future__ import annotations

from bots.shared import fleet
from bots.shared import db_reader
import time

# ---------------------------------------------------------------------------
# 세이브 대상 함선 가치표 (metal+crystal+deuterium 건조가, ogamex *ShipObjects.php 실측)
# ---------------------------------------------------------------------------
# 이동 가능한 전투/화물 함선만. 태양광위성(212)·크롤러(217)는 **이동 불가**(파킹 고정)라
# 세이브 불가 — 항상 노출된다(레이더의 위성 사냥 표적). 식민선(208)·정찰기(210)·
# 재활용선(209)은 각 모듈(colony/raid/harvest)이 관리하므로 세이브 대상에서 뺀다.
SHIP_VALUE: dict[int, int] = {
    202: 4000,      # small_cargo   2000+2000
    203: 12000,     # large_cargo   6000+6000
    204: 4000,      # light_fighter 3000+1000
    205: 10000,     # heavy_fighter 6000+4000
    206: 29000,     # cruiser       20000+7000+2000
    207: 60000,     # battleship    45000+15000
    211: 90000,     # bomber        50000+25000+15000
    213: 125000,    # destroyer     60000+50000+15000
    214: 10000000,  # deathstar     5M+4M+1M
    215: 85000,     # battlecruiser 30000+40000+15000
    218: 160000,    # reaper        85000+55000+20000
    219: 31000,     # pathfinder    8000+15000+8000
}
SAVABLE_SHIPS: tuple[int, ...] = tuple(SHIP_VALUE.keys())

COMPUTER_TECHNOLOGY = 108

# 세이브 파견 속도(1..10, 1=10%). 느릴수록 체공↑·연료↓. dry-run 으로 왕복시간 확인 후 조정.
FLEETSAVE_SPEED = 1

# 반복 틱에서 새로 건조된 함선을 별도 미션으로 계속 쪼개 보내지 않는다. 정체성 임무
# (원정/약탈/식민)가 다음 틱에도 쓸 슬롯을 보존하기 위한 하드 상한.
MAX_ACTIVE_FLEETSAVES = 1

# 목적지 탐색 범위(이웃 시스템 ±). 식민지 없는 계정의 폴백 스캔에만 쓴다(HTTP 절약 위해 좁게).
FLEETSAVE_SCAN_SPAN = 1

# 적응형 모드에서 최근 실제 피격을 위협으로 기억하는 시간. 상시 비행 대신 공격 징후가
# 있을 때만 대피하되, 한 번 맞은 계정은 잠시 더 보수적으로 움직인다.
ADAPTIVE_THREAT_WINDOW_SEC = 6 * 3600

# OGameX UniverseConstants::MAX_SYSTEM_COUNT. 시스템 도넛 거리/경계 스캔에 쓴다.
MAX_SYSTEM_COUNT = 499

# 사람(사용자) 계정 보호: 이 (galaxy, system) 는 세이브 목적지에서도 제외.
USER_EXCLUDE_SYSTEMS: set[tuple[int, int]] = {(1, 44)}

# 외부 0자원 수송은 상대 inbox에도 메시지를 생성한다. 좌표 소유자가
# 봇이고 이 용도를 허용했음을 확인한 경우에만 ``coord: player_id``를 추가한다.
# 좌표가 다른 계정으로 넘어가면 player_id 불일치로 자동 차단된다. 빈 기본값은
# 단일행성 계정의 플릿세이브를 건너뛰어 외부 계정/메시지 DB에 부작용을 남기지 않는다.
FOREIGN_TARGET_ALLOWLIST: dict[tuple[int, int, int], int] = {}


# ---------------------------------------------------------------------------
# 순수 계산 (단위검증 대상)
# ---------------------------------------------------------------------------
def fleet_value(ships: dict[int, int]) -> int:
    """함선 dict({ship_id: 수량})의 총 건조가치(metal+crystal+deut 합). 미등록 함선은 0."""
    return sum(SHIP_VALUE.get(int(tid), 0) * int(n) for tid, n in ships.items())


def savable_ships(levels: dict[int, int]) -> dict[int, int]:
    """levels 에서 세이브 가능한(이동 가능 전투/화물) 함선만 뽑아 {ship_id: 수량}."""
    return {tid: levels.get(tid, 0) for tid in SAVABLE_SHIPS if levels.get(tid, 0) > 0}


def identity_reserve(
    personality_key: str, levels: dict[int, int]
) -> dict[int, int]:
    """성격별로 다음 정체성 행동에 남길 최소 함대.

    현재 함대를 실제로 쓰는 후행 정체성 중 explorer만 예약이 필요하다. raider는 같은
    틱에서 raid가 먼저 실행된 뒤 남은 함대를 세이브하므로 별도 고정 예약을 두지 않는다.
    """
    if personality_key != "explorer":
        return {}
    # 지연 import로 explorer의 원정 편성 정책을 단일 진실 원천으로 유지한다.
    from bots.rule_based import explorer

    return explorer.expedition_identity_reserve(levels)


def ships_after_identity_reserve(
    ships: dict[int, int], personality_key: str, levels: dict[int, int]
) -> tuple[dict[int, int], dict[int, int]]:
    """세이브할 함대와 주기장에 예약할 함대를 분리한다."""
    wanted = identity_reserve(personality_key, levels)
    to_save: dict[int, int] = {}
    reserved: dict[int, int] = {}
    for tid, raw_amount in ships.items():
        amount = max(0, int(raw_amount))
        keep = min(amount, max(0, int(wanted.get(tid, 0))))
        if keep:
            reserved[tid] = keep
        if amount > keep:
            to_save[tid] = amount - keep
    return to_save, reserved


def fleet_slots_max(levels: dict[int, int]) -> int:
    """함대 슬롯 상한 = 1 + computer_technology. (General +2 보너스는 보수적으로 무시.)"""
    return 1 + levels.get(COMPUTER_TECHNOLOGY, 0)


def _system_distance(a: int, b: int) -> int:
    """OGameX 도넛 시스템에서의 최단 인덱스 거리."""
    direct = abs(a - b)
    return min(direct, MAX_SYSTEM_COUNT - direct)


def _coordinate_distance(candidate: dict, own: dict) -> int:
    """OGameX FleetMissionService 기본 거리 공식(빈/비활성 시스템 보정 전)."""
    dg = abs(int(candidate["galaxy"]) - int(own["galaxy"]))
    if dg:
        # 은하 수는 서버 설정값이라 여기서 은하 도넛 보정은 할 수 없다.
        return dg * 20_000
    ds = _system_distance(int(candidate["system"]), int(own["system"]))
    if ds:
        return ds * 5 * 19 + 2_700
    dp = abs(int(candidate["position"]) - int(own["position"]))
    if dp:
        return dp * 5 + 1_000
    return 5


def nearest_colony(colonies: list[dict], own: dict) -> dict | None:
    """자기 식민지 중 현재 행성(own)에서 OGameX 기본 거리가 가장 짧은 것.

    colonies/own 은 list_planets_db 항목 형식({id,galaxy,system,position,...}).
    빈 목록이면 None. (순수 함수 — 단위검증용)
    """
    if not colonies:
        return None

    return min(
        colonies,
        key=lambda c: (
            _coordinate_distance(c, own),
            int(c["galaxy"]), int(c["system"]), int(c["position"]),
        ),
    )


# ---------------------------------------------------------------------------
# 슬롯 사용량 (DB 직독)
# ---------------------------------------------------------------------------
def active_slots_in_use(email: str) -> int:
    """진행 중(processed=0, 미사일 type10 제외) 자기 함대 미션 수 = 슬롯 사용량.

    서버 getFleetSlotsInUse 복제(processed=1 인 취소/완료는 자동 제외). 조회 실패 시 큰 값을
    돌려 이번 틱 세이브를 보수적으로 건너뛰게 한다(과파견 방지).
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm "
                "JOIN users u ON u.id = fm.user_id "
                "WHERE u.email = %s AND fm.processed = 0 AND fm.mission_type != 10",
                (email,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 - 조회 실패 시 슬롯 만원으로 간주(세이브 스킵)
        return 10 ** 6


def active_fleetsaves_in_use(email: str) -> int:
    """진행 중인 Transport(3) 플릿세이브 수.

    이 시뮬레이션에서 봇의 Transport는 모두 화물 0 자기 식민지 세이브이고 외부
    allowlist 기본값도 비어 있다. 조회 실패 시 큰 값을 돌려 추가 파견을 fail-closed로
    막는다. 귀환 중인 processed=0 행도 포함해 왕복이 완전히 끝날 때까지 1건으로 센다.
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm "
                "JOIN users u ON u.id = fm.user_id "
                "WHERE u.email = %s AND fm.processed = 0 AND fm.mission_type = 3",
                (email,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 - 조회 실패 시 기존 세이브가 있다고 간주
        return 10 ** 6


def incoming_hostile_attacks(email: str) -> int:
    """현재 내 행성으로 오는 공격 미션 수. 조회 실패는 위협 있음으로 닫는다."""
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) n FROM fleet_missions fm "
                "JOIN planets p ON p.id=fm.planet_id_to "
                "JOIN users u ON u.id=p.user_id "
                "WHERE u.email=%s AND fm.user_id<>u.id "
                "AND fm.processed=0 AND fm.canceled=0 AND fm.mission_type=1",
                (email,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 - 위협 조회 실패 때 함대를 노출하지 않는다.
        return 1


def recent_hostile_attack(email: str, *, now: float | None = None) -> bool:
    """최근 방어자 전투보고가 있으면 True. 손실 0 정찰성 전투도 공격 시도로 센다."""
    try:
        from bots.rule_based import threat

        current = time.time() if now is None else float(now)
        return bool(threat._fetch_defender_reports(  # noqa: SLF001 - 공용 DB 경계 재사용
            email, current - ADAPTIVE_THREAT_WINDOW_SEC
        ))
    except Exception:  # noqa: BLE001 - DB 실패는 incoming 조회가 이미 fail-closed 처리
        return False


def adaptive_fleetsave_needed(email: str) -> bool:
    """상시 플릿세이브 대신 실제 현재/최근 위협에 반응한다."""
    return incoming_hostile_attacks(email) > 0 or recent_hostile_attack(email)


# ---------------------------------------------------------------------------
# 목적지 선정
# ---------------------------------------------------------------------------
def _scan_foreign_target(client, own: dict) -> tuple[int, int, int] | None:
    """명시 허용된 이웃 봇 좌표 중 화물0 수송을 받을 비활성 행성 1개.

    allowlist 가 비었으면 HTTP 스캔도 하지 않는다. 사람 시스템·자기 좌표·활성/
    휴가/관리자/밴 계정은 허용 목록에 있어도 하드 제외한다. 스캔 실패나 비정상 행은
    조용히 건너뛴다. 장기비활성 우선, 동률이면 도넛 근거리 우선.
    """
    if not FOREIGN_TARGET_ALLOWLIST:
        return None

    galaxy, system = own["galaxy"], own["system"]
    own_coord = (own["galaxy"], own["system"], own["position"])
    best: tuple[tuple[int, int, int], tuple[int, int]] | None = None
    systems = {
        ((int(system) - 1 + offset) % MAX_SYSTEM_COUNT) + 1
        for offset in range(-FLEETSAVE_SCAN_SPAN, FLEETSAVE_SCAN_SPAN + 1)
    }
    for s in sorted(systems, key=lambda item: (_system_distance(item, int(system)), item)):
        if (galaxy, s) in USER_EXCLUDE_SYSTEMS:
            continue
        try:
            rows = fleet.scan_galaxy(client, galaxy, s)
        except Exception:  # noqa: BLE001 - 시스템 스캔 실패는 건너뛴다
            continue
        for row in rows:
            try:
                coord = (
                    int(row["galaxy"]), int(row["system"]), int(row["position"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if coord[0] != galaxy or coord[1] != s or not 1 <= coord[2] <= 15:
                continue  # 요청한 시스템 밖/비정상 응답을 신뢰하지 않는다.
            if coord == own_coord or coord not in FOREIGN_TARGET_ALLOWLIST:
                continue
            try:
                if int(row["player_id"]) != int(FOREIGN_TARGET_ALLOWLIST[coord]):
                    continue  # 좌표 소유자가 바뀌면 기존 동의를 재사용하지 않는다.
            except (KeyError, TypeError, ValueError):
                continue
            if (coord[0], coord[1]) in USER_EXCLUDE_SYSTEMS:
                continue
            if row.get("vacation") or row.get("admin") or row.get("banned"):
                continue  # 휴가/관리자/밴 계정은 수송 불가·부적절
            if not (row.get("inactive") or row.get("long_inactive")):
                continue  # 명시 허용 좌표여도 활성 플레이어에게는 보내지 않는다.
            missions = row.get("available_missions") or []
            if fleet.MISSION_TRANSPORT not in missions:
                continue
            rank = 2 if row.get("long_inactive") else 1
            score = (rank, -_system_distance(s, int(system)))
            if best is None or score > best[1]:
                best = (coord, score)
    return best[0] if best else None


def pick_target(client, email: str, own: dict, colonies: list[dict]):
    """세이브 목적지 (coord, is_own) 를 고른다. 없으면 (None, False).

    1순위: 가장 가까운 자기 식민지(is_own=True). 2순위: allowlist의 비활성
    이웃 행성(is_own=False). FOREIGN_TARGET_ALLOWLIST 기본값이 비어 있으므로 평소에는
    식민지 없이 외부 수송을 하지 않는다. email 인자는 호출 호환성을 위해 유지한다.
    """
    col = nearest_colony(colonies, own)
    if col is not None:
        return (col["galaxy"], col["system"], col["position"]), True
    tgt = _scan_foreign_target(client, own)
    return (tgt, False) if tgt is not None else (None, False)


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def fleetsave_tick(
    client, email: str, levels: dict[int, int], personality_key: str, min_value: int,
    *, mode: str = "always",
    max_active_fleetsaves: int = MAX_ACTIVE_FLEETSAVES,
    fleet_slot_cap: int | None = None,
) -> str | None:
    """위험 잉여 함대를 저위험 왕복 수송으로 대피. 로그 문자열 또는 None.

    levels 는 miner_bot 스냅샷이지만, 이 함수는 파킹 함대를 **재조회**해 정확도를 높인다
    (이번 틱 약탈/원정으로 떠난 함선·이미 세이브 중인 함선 자동 제외). 어떤 경우에도 예외를
    올리지 않는다(루프 보호).

    min_value: 이 성격의 세이브 가치 임계(personality.FLEETSAVE_MIN_VALUE). 파킹 함대 가치가
    이 값 미만이면 세이브하지 않는다(레이더=낮게 상시, 채굴러/터틀=높게 사실상 off).

    mode="counter" 는 적 공격이 실제로 접근 중일 때 함대를 행성에 남겨 방어 매복에 쓰고,
    공격이 끝난 뒤에는 adaptive와 같이 최근 위협에 반응해 생존 함대를 대피시킨다.
    """
    try:
        return _fleetsave_tick_inner(
            client, email, levels, personality_key, min_value, mode=mode,
            max_active_fleetsaves=max_active_fleetsaves,
            fleet_slot_cap=fleet_slot_cap,
        )
    except Exception as exc:  # noqa: BLE001 - 세이브 버그가 빌드틱/루프를 깨지 않게
        return f"fleetsave err {type(exc).__name__}: {exc}"


def _fleetsave_tick_inner(
    client, email: str, levels: dict[int, int], personality_key: str, min_value: int,
    *, mode: str = "always",
    max_active_fleetsaves: int = MAX_ACTIVE_FLEETSAVES,
    fleet_slot_cap: int | None = None,
) -> str | None:
    if mode == "off":
        return None
    if mode == "counter":
        incoming = incoming_hostile_attacks(email)
        if incoming > 0:
            return f"fleetsave COUNTER HOLD (접근 공격 {incoming}건, 방어함대 유지)"
        mode = "adaptive"
    if mode == "adaptive" and not adaptive_fleetsave_needed(email):
        return None
    # 1) 파킹 함대 재조회(정확도). 실패 시 스냅샷으로 폴백.
    try:
        parked = db_reader.get_levels_db(email)
    except Exception:  # noqa: BLE001
        parked = levels
    parked_ships = savable_ships(parked)
    if not parked_ships:
        return None
    ships, reserved = ships_after_identity_reserve(
        parked_ships, personality_key, parked
    )
    if not ships:
        return None  # 정체성 최소 편성뿐이면 세이브가 이를 선점하지 않는다.
    value = fleet_value(ships)
    if value < min_value:
        return None  # 세이브할 가치 있는 파킹 함대 없음(대다수 틱)

    # 2) 기존 세이브가 왕복 중이면 새 소량 함선을 별도 미션으로 쪼개지 않는다.
    #    그렇지 않으면 4분마다 세이브가 누적돼 다음 원정/약탈 슬롯을 굶길 수 있다.
    configured_max_saves = max(0, int(max_active_fleetsaves))
    if (
        configured_max_saves > 0
        and active_fleetsaves_in_use(email) >= configured_max_saves
    ):
        return None

    # 3) 슬롯: 꽉 차 있으면(원정/약탈이 이미 씀) 이번 틱은 쉰다.
    slot_cap = (
        fleet_slots_max(parked)
        if fleet_slot_cap is None
        else max(0, int(fleet_slot_cap))
    )
    if active_slots_in_use(email) >= slot_cap:
        return None

    # 4) 자기 행성 목록 → 홈(현재)·식민지.
    try:
        planet_rows = db_reader.list_planets_db(email)
    except Exception:  # noqa: BLE001
        planet_rows = []
    own = next((p for p in planet_rows if p["is_current"]), None)
    if own is None:
        return None  # 홈 좌표 미상 → 목적지 계산 불가
    colonies = [p for p in planet_rows if not p["is_current"]]

    target, is_own = pick_target(client, email, own, colonies)
    if target is None:
        return None  # 목적지 없음(식민지 없고 이웃 점유행성도 못 찾음) → 이번 틱 스킵

    # 5) 화물 0 수송 왕복 파견(느린 속도로 체공 최대화).
    res = fleet.transport(
        client, target, ships, resources=(0, 0, 0), speed=FLEETSAVE_SPEED,
    )
    coord = f"{target[0]}:{target[1]}:{target[2]}"
    if res.get("ok"):
        crew = ", ".join(f"{_ship_name(t)}{n}" for t, n in ships.items())
        dest = "식민지" if is_own else "이웃"
        reserve_note = (
            f", 원정예약~{fleet_value(reserved)}" if reserved else ""
        )
        return (
            f"fleetsave SEND {coord} ({dest}, {crew}, 가치~{value}{reserve_note}, "
            f"{FLEETSAVE_SPEED*10}%)"
        )
    return f"fleetsave idle ({res.get('message', 'send failed')})"


def _ship_name(tid: int) -> str:
    from bots.rule_based.personality import tech_label  # 지연 import(순환 방지)
    return tech_label(tid)
