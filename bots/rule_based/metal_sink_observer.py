"""금속 sink 저빈도 shadow 관측기.

``metal_sink.plan_metal_sink``는 안전한 순수 planner지만, 실제 집행에 필요한 상태를 기존
틱 컨텍스트가 모두 가지고 있지는 않다. 이 모듈은 읽기 전용 DB에서 그 상태를 보충해
**계획만** 로그한다. HTTP 요청이나 건조 호출은 의도적으로 제공하지 않는다.

라이브 전환 전 남은 하드 게이트
-------------------------------

* 다음 성장비는 현재 큐와 공용 성장트리로 보수적으로 추정한다. AI 정책이 동적으로 추가한
  함선까지 완전하게 재현하는 단일 ``committed_resources`` 계약은 아직 없다.
* 플릿세이브 목적지는 현재 자기 식민지 존재 여부로 확인할 수 있지만, 새 화물선이 완성될
  미래 시점의 슬롯과 정확한 비행 연료는 확정할 수 없다.
* 함선/방어는 같은 unit queue를 쓰므로 shadow 시점에 비어 있어도 다른 후행 행동과 원자적
  예약이 되지 않는다.

따라서 관측 결과가 PLAN이어도 실행 허가가 아니다. 라이브 전환은 위 세 입력을 하나의
원자적 스냅샷/예약 계약으로 만든 뒤 별도 리뷰해야 한다.

부하 제한
---------

계정마다 12틱 중 한 번만, 이메일 CRC 버킷으로 서로 다른 틱에 분산한다. 관측 시에도
MariaDB SELECT만 사용하며 게임 HTTP 요청은 0회다. DB 장애는 조용히 None으로 닫아 성장
루프와 기존 로그를 방해하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
import zlib

from bots.rule_based import fleetsave, metal_sink
from bots.rule_based.personality import FLEETSAVE_MIN_VALUE, GROWTH, _plan_primary_base
from bots.shared import db_reader, game_objects as go


SHADOW_SAMPLE_INTERVAL = 12

# 운영자가 단순히 SHADOW 문자열을 지우고 라이브로 오인 전환하지 않도록, 남은 계약을
# 기계적으로도 노출한다. 이 모듈에는 실행 함수가 없고 이 값은 의도적으로 False 고정이다.
LIVE_EXECUTION_ENABLED = False
LIVE_BLOCKERS = (
    "ai-policy-extra-ship-commitment-unavailable",
    "future-fleetsave-slot-not-reserved",
    "exact-fleetsave-fuel-unavailable",
    "unit-queue-not-atomically-reserved",
)

# 공용 성장트리 및 AI 정책이 현재 사용할 수 있는 함선의 canonical 단가. 예약비 추정에만
# 쓰며 이 모듈은 어떤 함선도 건조하지 않는다.
GROWTH_SHIP_COST: dict[int, dict[str, int]] = {
    202: {"metal": 2_000, "crystal": 2_000, "deuterium": 0},
    203: {"metal": 6_000, "crystal": 6_000, "deuterium": 0},
    204: {"metal": 3_000, "crystal": 1_000, "deuterium": 0},
    205: {"metal": 6_000, "crystal": 4_000, "deuterium": 0},
    210: {"metal": 0, "crystal": 1_000, "deuterium": 0},
    212: {"metal": 0, "crystal": 2_000, "deuterium": 500},
    219: {"metal": 8_000, "crystal": 15_000, "deuterium": 8_000},
}
COLONY_SHIP_COST = {"metal": 10_000, "crystal": 20_000, "deuterium": 10_000}

# 공용 post_actions의 실제 식민 목표와 일치한다.
PLANET_GOAL = {
    "miner": 3,
    "raider": 2,
    "turtle": 1,
    "explorer": 3,
    "hybrid": 3,
}

_RESOURCE_KEYS = ("metal", "crystal", "deuterium")


@dataclass(frozen=True)
class QueueItem:
    technology_id: int
    target_level: int
    active: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    """한 번의 shadow 계획에 필요한 읽기 전용 런타임 상태."""

    resource_state: dict[str, int]
    on_homeworld: bool
    colony_action_pending: bool
    building_queue_depth: int
    research_queue_depth: int
    unit_queue_depth: int
    fleet_slots_used: int
    safe_fleetsave_target: bool
    planet_count: int
    total_large_cargo: int
    total_colony_ships: int
    parked_units: dict[int, int]
    building_queue: tuple[QueueItem, ...]
    research_queue: tuple[QueueItem, ...]


def should_sample(email: str, tick_index: int) -> bool:
    """결정적 버킷으로 계정 관측을 12틱에 분산한다."""
    try:
        tick = int(tick_index)
    except (TypeError, ValueError, OverflowError):
        return False
    bucket = zlib.crc32(email.encode("utf-8")) % SHADOW_SAMPLE_INTERVAL
    return tick % SHADOW_SAMPLE_INTERVAL == bucket


def _read_queue(cur, sql: str, email: str) -> tuple[QueueItem, ...]:
    cur.execute(sql, (email,))
    return tuple(
        QueueItem(
            technology_id=int(row["object_id"]),
            target_level=int(row["object_level_target"]),
            active=bool(row["building"]),
        )
        for row in cur.fetchall()
    )


def read_runtime_snapshot(email: str) -> RuntimeSnapshot:
    """현재 홈 행성과 계정 전체의 sink 관련 상태를 SELECT로만 읽는다.

    모든 행성+비행+미완료 unit queue의 대형화물/식민선을 합쳐, 주기장 수량만 보고
    중복 생산하는 오류를 막는다. unit queue의 이미 완성된 ``object_amount_progress``는
    행성 보유량에 들어가므로 남은 수량만 더한다.
    """
    resource_state = db_reader.get_resource_state_db(email)
    parked_ids = tuple(dict.fromkeys((*fleetsave.SAVABLE_SHIPS, 401, 208)))
    parked_columns = ", ".join(
        f"p.{go.name_of(tid)} AS parked_{tid}" for tid in parked_ids
    )

    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.planet_current current_planet_id, "
            "(SELECT MIN(ph.id) FROM planets ph WHERE ph.user_id=u.id "
            " AND ph.planet_type=1) homeworld_id, "
            "(SELECT COUNT(*) FROM planets pp WHERE pp.user_id=u.id "
            " AND pp.planet_type=1) planet_count, "
            "(SELECT COUNT(*) FROM building_queues bq "
            " WHERE bq.planet_id=u.planet_current AND bq.processed=0 "
            " AND bq.canceled=0) building_depth, "
            "(SELECT COUNT(*) FROM research_queues rq "
            " JOIN planets rp ON rp.id=rq.planet_id "
            " WHERE rp.user_id=u.id AND rq.processed=0 AND rq.canceled=0) research_depth, "
            "(SELECT COUNT(*) FROM unit_queues uq "
            " WHERE uq.planet_id=u.planet_current AND uq.processed=0) unit_depth, "
            "(SELECT COUNT(*) FROM fleet_missions fm "
            " WHERE fm.user_id=u.id AND fm.processed=0 AND fm.mission_type<>10) fleet_used, "
            "(SELECT COUNT(*) FROM fleet_missions cm "
            " WHERE cm.user_id=u.id AND cm.processed=0 AND cm.canceled=0 "
            " AND cm.mission_type=7 AND cm.parent_id IS NULL) colony_pending, "
            "(SELECT COALESCE(SUM(ap.large_cargo),0) FROM planets ap "
            " WHERE ap.user_id=u.id) + "
            "(SELECT COALESCE(SUM(lf.large_cargo),0) FROM fleet_missions lf "
            " WHERE lf.user_id=u.id AND lf.processed=0) + "
            "(SELECT COALESCE(SUM(GREATEST(lq.object_amount-lq.object_amount_progress,0)),0) "
            " FROM unit_queues lq JOIN planets lp ON lp.id=lq.planet_id "
            " WHERE lp.user_id=u.id AND lq.processed=0 AND lq.object_id=203) "
            " total_large_cargo, "
            "(SELECT COALESCE(SUM(cp.colony_ship),0) FROM planets cp "
            " WHERE cp.user_id=u.id) + "
            "(SELECT COALESCE(SUM(cf.colony_ship),0) FROM fleet_missions cf "
            " WHERE cf.user_id=u.id AND cf.processed=0) + "
            "(SELECT COALESCE(SUM(GREATEST(cq.object_amount-cq.object_amount_progress,0)),0) "
            " FROM unit_queues cq JOIN planets cqp ON cqp.id=cq.planet_id "
            " WHERE cqp.user_id=u.id AND cq.processed=0 AND cq.object_id=208) "
            " total_colony_ships, "
            f"{parked_columns} "
            "FROM users u JOIN planets p ON p.id=u.planet_current WHERE u.email=%s",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"metal sink user not found: {email}")

        building_queue = _read_queue(
            cur,
            "SELECT bq.object_id, bq.object_level_target, bq.building "
            "FROM building_queues bq JOIN users u ON u.planet_current=bq.planet_id "
            "WHERE u.email=%s AND bq.processed=0 AND bq.canceled=0 ORDER BY bq.id",
            email,
        )
        research_queue = _read_queue(
            cur,
            "SELECT rq.object_id, rq.object_level_target, rq.building "
            "FROM research_queues rq JOIN planets p ON p.id=rq.planet_id "
            "JOIN users u ON u.id=p.user_id WHERE u.email=%s "
            "AND rq.processed=0 AND rq.canceled=0 ORDER BY rq.id",
            email,
        )

    parked = {tid: max(0, int(row.get(f"parked_{tid}") or 0)) for tid in parked_ids}
    planet_count = max(0, int(row["planet_count"]))
    return RuntimeSnapshot(
        resource_state={k: int(v) for k, v in resource_state.items()},
        on_homeworld=int(row["current_planet_id"]) == int(row["homeworld_id"]),
        colony_action_pending=bool(row["colony_pending"]),
        building_queue_depth=max(0, int(row["building_depth"])),
        research_queue_depth=max(0, int(row["research_depth"])),
        unit_queue_depth=max(0, int(row["unit_depth"])),
        fleet_slots_used=max(0, int(row["fleet_used"])),
        safe_fleetsave_target=planet_count >= 2,
        planet_count=planet_count,
        total_large_cargo=max(0, int(row["total_large_cargo"])),
        total_colony_ships=max(0, int(row["total_colony_ships"])),
        parked_units=parked,
        building_queue=building_queue,
        research_queue=research_queue,
    )


def _add_cost(reserve: dict[str, int], cost: dict[str, int] | None) -> bool:
    if cost is None:
        return False
    for key in _RESOURCE_KEYS:
        reserve[key] += max(0, int(cost.get(key, 0)))
    return True


def estimate_growth_reserve(
    levels: dict[int, int],
    personality_key: str,
    tick_index: int,
    snapshot: RuntimeSnapshot,
) -> dict[str, int] | None:
    """현재 약정 큐 + 다음 공용 성장 행동들의 보수적 예약비.

    대기(building=0) 큐는 아직 자원이 차감되지 않았으므로 전부 예약한다. 그 큐가 끝난
    가상 레벨에서 다음 primary 최대 3건, 연구 최대 2건, 성격 함선 1배치와 필요 시
    식민선 1기를 더한다. 비용표 밖 정책 함선이 보이면 None으로 닫는다.
    """
    if personality_key not in GROWTH:
        return None
    simulated = dict(levels)
    simulated.update(snapshot.parked_units)
    reserve = {key: 0 for key in _RESOURCE_KEYS}

    for item in (*snapshot.building_queue, *snapshot.research_queue):
        simulated[item.technology_id] = max(
            simulated.get(item.technology_id, 0), item.target_level
        )
        if not item.active and not _add_cost(
            reserve, go.object_cost(item.technology_id, item.target_level)
        ):
            return None

    # 성장 봇이 한 틱에 실제로 열 수 있는 건설/연구 깊이만큼 다음 비용을 보호한다.
    primary = _plan_primary_base(personality_key, simulated, int(tick_index))
    for action in primary[:3]:
        tid = int(action[1])
        if not _add_cost(reserve, go.object_cost(tid, simulated.get(tid, 0) + 1)):
            return None

    research_candidates: list[int] = []
    for tid, target in GROWTH[personality_key]["research_targets"]:
        if simulated.get(tid, 0) < target:
            research_candidates.append(tid)
        if len(research_candidates) >= 2:
            break
    for tid in research_candidates:
        if not _add_cost(reserve, go.object_cost(tid, simulated.get(tid, 0) + 1)):
            return None

    # 공용 성격 함선 한 배치를 다음 unit-queue 약정으로 보호한다. 실제 AI 정책 추가 함선은
    # 호출부의 정책 객체 없이는 완전 재현할 수 없어, 이 값은 shadow 전용 추정치다.
    for tid, target, batch in GROWTH[personality_key]["ship_targets"]:
        have = simulated.get(tid, 0)
        if have >= target:
            continue
        unit_cost = GROWTH_SHIP_COST.get(tid)
        if unit_cost is None:
            return None
        amount = min(target - have, batch)
        _add_cost(
            reserve,
            {key: unit_cost[key] * amount for key in _RESOURCE_KEYS},
        )
        break

    goal = PLANET_GOAL[personality_key]
    if snapshot.planet_count < goal and snapshot.total_colony_ships < 1:
        _add_cost(reserve, COLONY_SHIP_COST)
    return reserve


def observe_metal_sink_shadow(
    email: str,
    levels: dict[int, int],
    personality_key: str,
    tick_index: int,
) -> str | None:
    """샘플 틱에만 shadow 계획 한 줄을 반환한다. 외부 쓰기/HTTP는 절대 하지 않는다."""
    if not should_sample(email, tick_index):
        return None
    try:
        snapshot = read_runtime_snapshot(email)
        reserve = estimate_growth_reserve(levels, personality_key, tick_index, snapshot)
        refreshed_levels = dict(levels)
        refreshed_levels.update(snapshot.parked_units)
        savable, _reserved = fleetsave.ships_after_identity_reserve(
            fleetsave.savable_ships(refreshed_levels), personality_key, refreshed_levels
        )
        slots_free = max(
            0, fleetsave.fleet_slots_max(refreshed_levels) - snapshot.fleet_slots_used
        )
        decision = metal_sink.plan_metal_sink(
            refreshed_levels,
            snapshot.resource_state,
            reserve,
            personality_key=personality_key,
            energy=snapshot.resource_state.get("energy"),
            on_homeworld=snapshot.on_homeworld,
            colony_action_pending=snapshot.colony_action_pending,
            building_queue_depth=snapshot.building_queue_depth,
            research_queue_depth=snapshot.research_queue_depth,
            unit_queue_depth=snapshot.unit_queue_depth,
            fleet_slots_free=slots_free,
            safe_fleetsave_target=snapshot.safe_fleetsave_target,
            parked_savable_value=fleetsave.fleet_value(savable),
            fleetsave_min_value=FLEETSAVE_MIN_VALUE.get(personality_key),
            total_large_cargo=snapshot.total_large_cargo,
            planet_count=snapshot.planet_count,
        )
    except Exception:  # noqa: BLE001 - 관측 실패가 성장 루프/기존 로그에 영향 금지
        return None

    formatted = metal_sink.format_decision(decision)
    return formatted.replace("metal-sink ", "metal-sink SHADOW ", 1)
