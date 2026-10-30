"""저장고(창고) 전략 + 건설 성공 판정 보조 상태(백오프/용량 스트릭).

## 왜 필요한가 (배경)
OGameX 는 자원을 저장 용량 상한(planets.metal_max 등, 기본 10000)에서 자른다. 저장고를
안 지으면 광산 레벨이 오를수록 건설비가 상한을 넘어서고, 서버는 그 항목을 시작 시점에
canceled=1 로 조용히 취소한다. 저장고 개념이 없던 봇은 이 취소를 감지 못 해 같은 요청을
매 틱 반복하다 성장 정지에 빠졌다(실측: 최근 1h 건설요청의 99.8% 취소).

이 모듈은 두 가지를 제공한다.
  1) 저장고 판정: 자원 상태(현재값+용량)와 다음 목표 비용을 보고, 필요한 저장고 3종
     (metal_store=22 / crystal_store=23 / deuterium_store=24)을 우선 건설 후보로 올린다.
       - 선제(proactive): 다음 목표의 단일자원 비용 > 그 자원 용량 × 0.9  → 미리 저장고.
       - 반응(reactive) : 자원이 용량의 95% 이상인 상태가 2틱 지속       → 생산 낭비 방지 저장고.
  2) 건설 백오프: '자원부족'으로 취소된 (봇,대상) 조합을 몇 틱 재시도 금지(과요청 억제).
     '용량부족' 취소는 백오프 대신 저장고 건설로 푼다(판정 1이 담당).

## 상태 파일
봇마다 .build_backoff/<봇>.json 하나. 스키마:
  {"backoff": {"<object_id>": <재시도 허용 tick_index>},
   "cap_streak": {"metal": n, "crystal": n, "deuterium": n},  # 95%+ 연속 틱수
   "cap_streak_tick": <마지막 갱신 tick_index>}               # 틱당 1회만 증가
tick_index 는 run_universe 가 바퀴마다 1씩 올리는 논리 시계다(분 기반 시작).

스레드 안전: run_universe 는 봇별로 서로 다른 스레드가 처리하되 **한 봇은 한 스레드**가
전담하므로, 한 파일에 대한 접근은 그 봇의 틱 안에서 순차적이다(교차 쓰기 없음).
"""
from __future__ import annotations

import json
import pathlib

from bots.shared.game_objects import STORE_FOR_RESOURCE, object_cost

STATE_DIR = pathlib.Path(__file__).resolve().parent / ".build_backoff"

# --- 튜닝 파라미터 ---
BACKOFF_TICKS = 3         # '자원부족' 취소 후 재시도 금지 틱수
CAP_STREAK_TICKS = 2      # 반응 규칙: 95%+ 가 이 틱수만큼 연속되면 저장고
PROACTIVE_RATIO = 0.9     # 선제 규칙: 다음 목표 비용 > 용량 × 이 비율 → 저장고
REACTIVE_RATIO = 0.95     # 반응 규칙: 현재자원 ≥ 용량 × 이 비율 → 스트릭 증가

_RESOURCES = ("metal", "crystal", "deuterium")


# ---------------------------------------------------------------------------
# 상태 파일 I/O (방어적 — 깨진 파일은 빈 상태로 취급)
# ---------------------------------------------------------------------------
def _state_path(state_key: str) -> pathlib.Path:
    # 이메일 등 임의 문자열을 파일명 안전하게. localpart 만 써도 충돌 거의 없지만
    # @/./ 등을 '_' 로 치환해 그대로 보존한다(재현·디버깅 용이).
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in state_key)
    return STATE_DIR / f"{safe}.json"


def _load(state_key: str) -> dict:
    path = _state_path(state_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(state_key: str, data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_path(state_key).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        # 상태 저장 실패는 치명적이지 않다(백오프/스트릭이 잠깐 리셋될 뿐). 조용히 무시.
        pass


# ---------------------------------------------------------------------------
# 백오프 (자원부족 취소 억제)
# ---------------------------------------------------------------------------
def in_backoff(state_key: str, object_id: int, tick_index: int) -> bool:
    """(봇, object_id) 가 아직 백오프 중이면 True → 이번 틱 시도를 건너뛴다."""
    data = _load(state_key)
    until = data.get("backoff", {}).get(str(object_id))
    return isinstance(until, (int, float)) and tick_index < until


def set_backoff(state_key: str, object_id: int, tick_index: int) -> None:
    """object_id 를 tick_index + BACKOFF_TICKS 까지 재시도 금지로 표시."""
    data = _load(state_key)
    data.setdefault("backoff", {})[str(object_id)] = tick_index + BACKOFF_TICKS
    _save(state_key, data)


def clear_backoff(state_key: str, object_id: int) -> None:
    """object_id 백오프 해제(건설 성공 시)."""
    data = _load(state_key)
    bo = data.get("backoff")
    if isinstance(bo, dict) and str(object_id) in bo:
        del bo[str(object_id)]
        _save(state_key, data)


# ---------------------------------------------------------------------------
# 취소 사유 분류
# ---------------------------------------------------------------------------
def classify_cancel(
    object_id: int, target_level: int, resource_state: dict | None
) -> str:
    """서버가 취소한 건설/연구 항목의 사유를 추정해 한 단어로 분류.

    - "용량부족": 단일자원 비용이 그 자원의 저장 용량 상한(*_max)을 넘음 → 저장고 필요.
    - "자원부족": 용량엔 담기지만 현재 보유량이 모자람 → 잠깐 백오프 후 재시도.
    - "선행조건/기타": 비용도 감당 가능한데 취소됨 → 선행조건/동시성 등.
    비용/자원상태를 모르면(비용 미상 함선 등) "선행조건/기타" 로 둔다.
    """
    cost = object_cost(object_id, target_level)
    if cost is None or not resource_state:
        return "선행조건/기타"
    # 1) 용량 초과: 어느 한 자원이라도 비용 > 용량상한(상한>0 일 때만 판정)
    for r in _RESOURCES:
        cap = resource_state.get(f"{r}_max", 0)
        if cap > 0 and cost[r] > cap:
            return "용량부족"
    # 2) 현재 보유량 부족
    for r in _RESOURCES:
        if cost[r] > resource_state.get(r, 0):
            return "자원부족"
    return "선행조건/기타"


# ---------------------------------------------------------------------------
# 저장고 판정
# ---------------------------------------------------------------------------
def _lead_target_needs(
    levels: dict[int, int], resource_state: dict, lead_targets: list[tuple]
) -> set[str]:
    """선제 규칙: 다음 목표들(lead_targets)의 단일자원 비용 > 용량 × 0.9 인 자원 집합.

    lead_targets 는 plan_primary 가 이번 틱에 실제로 시도할 상위 후보
    (("build"|"facility", tid) 튜플). 각 후보를 현재레벨+1 로 올릴 비용을 계산한다.
    """
    need: set[str] = set()
    for action in lead_targets:
        tid = action[1]
        cost = object_cost(tid, levels.get(tid, 0) + 1)
        if cost is None:
            continue
        for r in _RESOURCES:
            cap = resource_state.get(f"{r}_max", 0)
            if cap > 0 and cost[r] > cap * PROACTIVE_RATIO:
                need.add(r)
    return need


def _update_cap_streak(
    state_key: str, resource_state: dict, tick_index: int
) -> set[str]:
    """반응 규칙: 자원 ≥ 용량×0.95 연속 틱수를 갱신하고, 임계 도달한 자원 집합을 반환.

    틱당 1회만 증가(cap_streak_tick 으로 중복 증가 방지). 95% 미만이면 그 자원 스트릭 리셋.
    """
    data = _load(state_key)
    streak = data.get("cap_streak")
    if not isinstance(streak, dict):
        streak = {}
    last_tick = data.get("cap_streak_tick")
    already_counted = last_tick == tick_index  # 같은 틱 재호출 방어

    ripe: set[str] = set()
    for r in _RESOURCES:
        cap = resource_state.get(f"{r}_max", 0)
        cur = resource_state.get(r, 0)
        capped = cap > 0 and cur >= cap * REACTIVE_RATIO
        n = int(streak.get(r, 0))
        if capped:
            if not already_counted:
                n += 1
            if n >= CAP_STREAK_TICKS:
                ripe.add(r)
        else:
            n = 0
        streak[r] = n

    data["cap_streak"] = streak
    data["cap_streak_tick"] = tick_index
    _save(state_key, data)
    return ripe


def _stores_from_need(
    need: set[str],
    levels: dict[int, int],
    resource_state: dict,
    active_targets: set[int],
) -> list[int]:
    """필요 자원 집합(need)을 저장고 building id 목록으로 변환(metal→crystal→deuterium 순).

    엣지 필터: 이미 큐에 있는 저장고(active_targets)는 제외하고, 저장고 다음 레벨 비용이
    현재 용량을 넘어 지금은 못 짓는 경우도 제외(무한 재시도 방지).
    """
    out: list[int] = []
    for r in _RESOURCES:
        if r not in need:
            continue
        sid = STORE_FOR_RESOURCE[r]
        if sid in active_targets:
            continue
        scost = object_cost(sid, levels.get(sid, 0) + 1)
        cap = resource_state.get(f"{r}_max", 0)
        if scost is not None and cap > 0 and scost[r] > cap:
            continue
        out.append(sid)
    return out


def stores_for_affording(
    levels: dict[int, int],
    resource_state: dict | None,
    target_action: tuple,
    active_targets: set[int] | None = None,
) -> list[int]:
    """target_action 을 이번 틱 지불 가능하게 하려면 먼저 지어야 하는 저장고 id 목록.

    대상(("build"|"facility", tid))을 현재레벨+1 로 올리는 단일자원 비용 > 그 자원 용량×0.9
    이면 그 저장고를 반환한다(선제 규칙과 같은 임계). 에너지 비상 시 태양광(태양광 비용이
    창고 용량을 넘으면 서버가 '용량부족'으로 취소 → 무한 재시도)이 취소되지 않도록, 이
    저장고를 태양광보다 **앞에** 끼우는 데 쓴다. 반응(용량 스트릭) 규칙은 건드리지 않아
    부작용(상태 파일 증가)이 없다 — storage_ids_needed 와 달리 순수 함수다.
    """
    if not resource_state:
        return []
    need = _lead_target_needs(levels, resource_state, [target_action])
    return _stores_from_need(need, levels, resource_state, active_targets or set())


def storage_ids_needed(
    levels: dict[int, int],
    resource_state: dict | None,
    lead_targets: list[tuple],
    state_key: str | None,
    tick_index: int,
    active_targets: set[int] | None = None,
) -> list[int]:
    """이번 틱에 우선 건설할 저장고 building id 목록(metal→crystal→deuterium 순).

    선제(다음 목표 비용) + 반응(2틱 용량 스트릭) 규칙을 합집합으로 적용한다. 단:
      - 용량 상한이 0(미정산 행성)인 자원은 건너뛴다(오판 방지).
      - 저장고 자체 비용이 현재 용량을 넘으면(초고레벨 엣지) 큐잉하지 않는다(무한 재시도 방지).
      - 이미 건설 중/대기 중인 저장고(active_targets)는 중복 큐잉하지 않는다.
    resource_state 가 없으면(AI 경로 등) 빈 목록.
    """
    if not resource_state:
        return []
    need: set[str] = set(_lead_target_needs(levels, resource_state, lead_targets))
    if state_key is not None:
        need |= _update_cap_streak(state_key, resource_state, tick_index)

    return _stores_from_need(need, levels, resource_state, active_targets or set())
