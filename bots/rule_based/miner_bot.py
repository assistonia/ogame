"""규칙 기반 성장봇 (성격 파라미터 + 상태머신).

한 틱의 동작:
  로그인 → 자원 현황(에너지) + 건물/시설/연구/함선 레벨 조회 →
  성격별 성장 상태머신으로 여러 건 행동.
  - primary  : 자원 건물/시설을 공용 건설 큐에 우선순위대로 최대 3건 쌓기
    (서버 큐 상한 5=진행1+대기4. 큐가 차면 중단).
  - secondary: 연구를 연구 큐에 최대 2건 + 함선 1배치(무제한 조선소 큐).
  - 성장 단계: 자원 건물(floor 도달) → 시설(로봇공장→연구소→조선소) →
    성격별 연구/함선 분기. 에너지 음수면 성격과 무관하게 태양광 강제.
  - 선행조건은 봇이 직접 확인(personality.REQUIREMENTS)해 못 지을 것은 시도 안 함.
  - 자원 부족/큐 참으로 실패하면 조용히 다음 후보로 넘어간다(예외 미발생).
  - 상태(levels) 조회에 실패하면 자원 건물만 짓는 안전 모드로 폴백한다.
  - 무엇을 했는지 한 줄 결과 문자열로 반환한다.

silent-success 대비: 별도 재조회 대신, 다음 틱의 levels 조회가 실제 반영을
검증한다(반영 안 됐으면 같은 후보를 다시 시도 → 자기 교정).

로그인 실패(계정 없음/비번 오류)는 예외로 올려서 스케줄러가 스킵하게 한다.
"""
from __future__ import annotations

import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from bots.shared.client import OGameClient  # noqa: E402
from bots.shared import db_reader  # noqa: E402
from bots.shared import economy_batch  # noqa: E402
from bots.shared import game_objects  # noqa: E402
from bots.rule_based import storage  # noqa: E402
from bots.rule_based import post_actions  # noqa: E402
from bots.rule_based.tick_context import TickContext, build_tick_context  # noqa: E402
from bots.rule_based.personality import (  # noqa: E402
    PERSONALITIES,
    assign_personality,
    build_candidates,
    idle_fallback_candidates,
    plan_primary,
    plan_secondary,
    planning_personality,
    tech_label,
)


# 서버 큐 상한(실측·ogamex/app/ViewModels/Queue/Abstracts/QueueListViewModel.php):
# 건설 큐·연구 큐는 각각 "진행중 1 + 대기 4 = 5"건. 조선소/방어(unit) 큐는 무제한.
# 틱당 이 상한까지 다 채우지 않고 headroom 을 남긴다(다음 틱과의 경합·과요청 방지).
#
# OGameX 건설/연구 큐의 add 는 자원을 검사하지 않는다: 큐 항목이 실제로 '시작'될 때
# 자원을 차감하고, 감당 못 하면 그 항목만 조용히 취소한다. 따라서 큐가 가득 차기
# 전까지 add 는 자원과 무관하게 성공하며, 봇은 우선순위대로 큐를 채우기만 하면 된다
# (자원 배분은 서버가 시작 시점에 처리).
MAX_BUILD_PER_TICK = 3     # 건설 큐(자원건물+시설 공용)에 한 틱에 쌓을 최대 건수
MAX_RESEARCH_PER_TICK = 2  # 연구 큐에 한 틱에 쌓을 최대 건수

# --- 유휴(IDLE) 폴백 게이트 (기본 OFF) -------------------------------------
# 배경(실측): 채굴러/탐험가/하이브리드 AI 는 광산이 고레벨(예 24/20/20)이 되면 모든 광산
# 다음 레벨이 수십만 크리스탈로 뛰고, 에너지가 흑자라 태양광 개입도 안 걸려서 "이번 틱에
# 살 수 있는 정상 후보가 하나도 없는" 상태에 자주 빠진다 → 큰 잉여(예 2.9M 메탈)를 방치한
# 채 idle(전체 틱의 ~40%). 이 플래그가 True 면, 정상 primary 후보가 모두 스킵돼 아무것도
# 큐잉되지 못한 틱에 한해, personality.idle_fallback_candidates 의 저비용 대체 체인
# (싼 광산 → 저장고 → 시설 → 최후수단 태양광)을 같은 사전체크로 시도해 잉여를 소진한다.
#
# 기본 False: 오케스트레이터가 True 로 뒤집고 루프를 재기동하기 전까지 동작은 바이트 단위로
# 기존과 동일하다(폴백 블록 자체가 실행되지 않음). 켜져도 정상 후보를 감당하는 규칙봇에는
# 사실상 no-op 다(정상 후보가 이미 큐를 채워 done 이 비지 않으므로 폴백 진입 안 함).
MINER_IDLE_FALLBACK_ENABLED = True


def research_blocked_by_lab(
    active_build_targets: set[int], primary_actions: list[tuple]
) -> bool:
    """연구소 공사 중이면 같은 틱의 연구 요청을 막는다.

    OGameX는 연구소(31) 업그레이드가 건설 큐에 있는 동안 연구 add 요청을 success로 받은
    뒤 취소 행으로 남길 수 있다. 틱 시작부터 공사 중이었거나 이번 primary 단계에서 새로
    연구소를 올렸다면 연구 POST를 한 바퀴 양보한다. 광산/다른 시설 공사는 연구와 무관하다.
    """
    return (
        game_objects.RESEARCH_LAB in active_build_targets
        or any(
            len(action) >= 2 and action[1] == game_objects.RESEARCH_LAB
            for action in primary_actions
        )
    )


def _is_success(res: dict) -> bool:
    """build() 응답이 성공인지 판정.

    성공: {"status": "success", ...}
    실패(큐 참 등): {"success": false, "message": "..."}  ← status 키 없음
    """
    if not isinstance(res, dict):
        return False
    return res.get("status") == "success" or res.get("success") is True


def _is_queue_full(res: dict) -> bool:
    """add-buildrequest 응답이 '큐 참'인지 판정.

    서버는 큐가 가득 차면 {"success": false, "message":
    "Maximum number of items already in queue."} 로 답한다
    (ogamex BuildingQueueService/ResearchQueueService). 이 응답이 오면
    같은 큐에 더 넣을 자리가 없으니 그 큐 대상 후보 시도를 멈춘다.
    """
    if not isinstance(res, dict):
        return False
    return "Maximum number of items" in str(res.get("message", ""))


def default_tick_index() -> int:
    """스케줄러가 tick_index를 안 주면 분 단위 시각을 인덱스로 쓴다.

    같은 봇이라도 틱마다 성격 사이클을 한 칸씩 돌게 하기 위함이다.
    """
    return int(time.time() // 60)


def _collect_levels(client: OGameClient) -> dict[int, int]:
    """건물/시설/연구/함선 레벨을 조회. 읽기 전용 DB 직독 우선, 실패 시 HTTP 폴백.

    DB 직독(db_reader.get_levels_db)은 한 번의 쿼리로 건물·시설·연구·함선·방어를 모두
    반환하고 지연 정산(미처리 완료 큐)까지 보정한다. HTTP 파싱보다 훨씬 가볍다.

    폴백(HTTP): 부하를 줄이려고 필요한 페이지만 단계적으로 본다.
      - 항상: resources(자원 건물) + facilities(시설, 연구소/조선소 레벨 포함).
      - 연구소(31)≥1 일 때만: research 페이지 추가 (연구 레벨/선행조건 판단용).
      - 조선소(21)≥1 일 때만: shipyard 페이지 추가 (함선 수량 판단용).
    """
    try:
        return db_reader.get_levels_db(client.email)
    except Exception:  # noqa: BLE001 - DB 실패 시 기존 HTTP 경로로 폴백
        levels = client.get_levels(pages=("resources", "facilities"))
        if levels.get(31, 0) >= 1:
            levels.update(client.get_levels(pages=("research",)))
        if levels.get(21, 0) >= 1:
            levels.update(client.get_levels(pages=("shipyard",)))
        return levels


def _do_action(client: OGameClient, action: tuple) -> dict:
    """행동 튜플을 알맞은 client 메서드로 실행."""
    kind = action[0]
    if kind == "build":
        return client.build(action[1])
    if kind == "facility":
        return client.build_facility(action[1])
    if kind == "research":
        return client.research(action[1])
    if kind == "ship":
        return client.build_ships(action[1], action[2])
    raise ValueError(f"unknown action {action!r}")


def _fmt_action(action: tuple) -> str:
    """로그용 행동 표기."""
    kind = action[0]
    if kind == "ship":
        return f"ship {tech_label(action[1])}({action[1]})x{action[2]}"
    return f"{kind} {tech_label(action[1])}({action[1]})"


def _try_first(client: OGameClient, candidates: list[tuple]) -> tuple[tuple | None, str]:
    """후보를 우선순위대로 시도, 서버가 처음 성공을 준 행동을 반환.

    반환: (성공한 action 또는 None, 마지막 실패 메시지).
    """
    last_msg = ""

    # Every ship producer shares one authoritative queue horizon.  This also
    # covers fixed bootstrap targets and LLM ship_priority additions, which used
    # to bypass strategic_growth's cap and could create month-long queues.
    if any(action and action[0] == "ship" for action in candidates):
        from bots.rule_based import production_capacity

        snapshot = production_capacity.production_queue_snapshot(client.email)
        try:
            live_levels = (
                db_reader.get_levels_db(client.email, planet_id=snapshot.planet_id)
                if snapshot.available and snapshot.planet_id is not None
                else {}
            )
        except Exception:  # noqa: BLE001 - production must fail closed
            live_levels = {}
        admitted: list[tuple] = []
        repeat_batch_floor = {
            202: 5,   # small cargo
            203: 5,   # large cargo
            204: 10,  # light fighter
            205: 5,   # heavy fighter
            206: 3,   # cruiser
            210: 10,  # espionage probe
            217: 5,   # crawler
        }
        for action in candidates:
            if not action or action[0] != "ship" or len(action) < 3:
                admitted.append(action)
                continue
            technology_id = int(action[1])
            # One active row per unit type.  Re-evaluate after that batch
            # completes instead of appending the same one-unit order every tick.
            if production_capacity.pending_unit_amount(snapshot, technology_id) > 0:
                continue
            requested = int(action[2])
            floor = repeat_batch_floor.get(technology_id, 1)
            if requested < floor and int(live_levels.get(technology_id, 0)) >= floor:
                continue
            horizon = (
                production_capacity.MAX_QUEUE_HORIZON_SECONDS
                if technology_id == 214 else None
            )
            amount = production_capacity.cap_ship_amount_from_snapshot(
                snapshot,
                technology_id,
                requested,
                levels=live_levels,
                horizon_seconds=horizon,
            )
            if amount > 0 and not (requested > 1 and amount < min(requested, floor)):
                admitted.append(("ship", technology_id, amount))
        candidates = admitted
        if not candidates:
            return None, "unit queue horizon/pending admission hold"

    # Opt-in batch path: all ordered ship candidates cross GlobalGame once and the
    # server stops after the first queue row it actually created.  A feature-hidden
    # 404 returns None and falls through to the browser-compatible loop below.
    if candidates and economy_batch.enabled_for_account(client.email):
        try:
            state = db_reader.get_resource_state_db(client.email)
            planet_id = int(state.get("planet_id") or 0)
        except Exception:  # noqa: BLE001 - explicit target unknown => legacy path
            planet_id = 0
        if planet_id > 0:
            payload = []
            by_id: dict[str, tuple] = {}
            for index, action in enumerate(candidates):
                if not action or action[0] != "ship" or len(action) < 3:
                    continue
                action_id = f"ship-{index}"
                by_id[action_id] = action
                payload.append({
                    "action_id": action_id,
                    "kind": "ship",
                    "planet_id": planet_id,
                    "technology_id": int(action[1]),
                    "amount": max(1, int(action[2])),
                })
            try:
                response = economy_batch.try_economy_batch(
                    client, payload, max_success=1, stop_on_queue_full=False
                )
            except economy_batch.EconomyBatchError as exc:
                # Unknown partial execution must never be replayed through legacy.
                return None, f"economy-batch error: {exc}"
            if response is not None:
                for result in response.get("results", []):
                    action = by_id.get(str(result.get("action_id", "")))
                    if action is None:
                        continue
                    if result.get("success") is True and result.get("status") == "accepted":
                        accepted = int(result.get("accepted_amount") or action[2])
                        return (action[0], action[1], accepted), last_msg
                    if result.get("status") != "skipped":
                        last_msg = str(result.get("message") or result.get("code") or result)
                return None, last_msg

    for action in candidates:
        try:
            res = _do_action(client, action)
        except Exception as exc:  # noqa: BLE001 - 개별 행동 오류는 다음 후보로
            last_msg = f"{_fmt_action(action)} error {type(exc).__name__}"
            continue
        if _is_success(res):
            return action, last_msg
        last_msg = str(res.get("message", res)) if isinstance(res, dict) else str(res)
    return None, last_msg


# 큐 재조회로 검증하는 행동 종류(건설/시설 → building_queues, 연구 → research_queues).
# 함선(unit_queues)은 서버가 취소하지 않으므로 검증 대상이 아니다.
_VERIFY_TABLE = {"build": "building", "facility": "building", "research": "research"}


def _queue_actions(
    client: OGameClient,
    candidates: list[tuple],
    max_items: int,
    *,
    verify: bool = True,
    tick_index: int | None = None,
    levels: dict[int, int] | None = None,
    resource_state: dict | None = None,
    report: list[str] | None = None,
    ctx: TickContext | None = None,
    backoff_key: str | None = None,
    idle_fallback: bool = False,
) -> tuple[list[tuple], str]:
    """후보를 우선순위대로 시도하며 성공한 것을 **같은 큐**에 최대 max_items건까지 쌓는다.

    하나의 큐(건설 큐 또는 연구 큐)를 채우는 후보 목록에 쓴다.
      - '큐 참'(Maximum number of items): 그 큐엔 자리가 없으니 즉시 중단.
      - 그 밖의 HTTP 실패(조선소 사용중 등): 조용히 다음 후보로 넘어간다.

    ## 성공 판정 강화 (verify=True, 건설/연구 큐 항목 대상)
    OGameX 의 add 는 자원을 검사하지 않고 항목을 큐에 넣은 뒤 같은 요청 안에서 시작을
    시도하며, 자원(창고 용량 상한 포함) 부족이면 그 항목만 canceled=1 로 취소한다. 따라서
    HTTP success 만으로는 실제 건설을 보장하지 못한다. 이 함수는 add 직후 DB 를 재조회해
    (db_reader.latest_queue_canceled) 내 항목이 살아있는지 확인한다:
      - 살아있음(canceled=0): 성공으로 집계하고 그 대상의 백오프를 해제한다.
      - 취소됨(canceled=1): 사유를 분류(storage.classify_cancel)해 report 에 남긴다.
          · '용량부족' → 백오프하지 않는다(저장고 규칙이 창고를 지어 근본 해결).
          · '자원부족' → 3틱 백오프(과요청 억제). 다음 몇 틱 그 대상을 건너뛴다.
          · '선행조건/기타' → 집계만(백오프 없음).
    이미 백오프 중인 대상은 add 시도조차 하지 않고 건너뛴다.

    ## 사전 자원체크 (verify=True + 자원상태 보유 시)
    add 를 보내기 전에 "현재 보유(이번 틱 성공분 차감 반영) ≥ 비용" 을 로컬로 판정해,
    못 살 것은 요청 자체를 보내지 않는다("사전스킵" 으로 report 집계). 서버 취소 스팸이
    곧 서버 부하이고, 원인(크리스탈 가뭄이든 다음 병목이든)과 무관한 재발 방지책이다.
    단일자원 비용이 용량 상한을 넘으면 "사전스킵:용량부족"(저장고 규칙이 근본 해결),
    보유량만 모자라면 "사전스킵:자원부족". 스킵은 HTTP 비용이 없으므로 백오프하지 않는다.
    자원상태·레벨을 모르거나(폴백 경로) 비용 미상(id가 BASE_PRICE 에 없음)이면 기존처럼
    일단 시도하고 사후 검증으로 잡는다.

    함선/방어 등 검증 불가 종류나 verify=False 면 기존처럼 HTTP success 로만 판정한다.
    반환: (성공한 action 리스트, 마지막 메시지). 하위호환(2-튜플) 유지.

    ctx(TickContext)를 주면 사전체크에 쓰는 tick_index/levels/resource_state 를 그 필드에서
    채운다(개별 인자로 명시하면 그쪽이 우선). 규칙봇/AI 두 경로가 같은 ctx 를 넘겨,
    AI 경로에서 연구 큐 사전체크 인자가 누락되던 회귀를 구조적으로 막는다. report 는
    틱별 로그 수집기라 ctx 에 넣지 않고 개별 인자로 남긴다.

    backoff_key 를 주면 백오프(자원부족 스킵) 상태를 그 키로 격리한다(기본 client.email).
    식민지 개발이 행성별 백오프를 쓰게 하는 용도 — db_reader 조회는 계속 email 로 한다.

    idle_fallback=True 이고 모듈 게이트(MINER_IDLE_FALLBACK_ENABLED)가 켜져 있을 때만,
    정상 후보가 하나도 큐잉되지 못한 틱(잉여 방치=IDLE)에 한해 저비용 대체 체인
    (personality.idle_fallback_candidates)을 같은 사전체크/검증 경로로 추가 시도한다.
    ctx 로 personality_key/active_targets 를 받아 폴백 후보를 만든다. 게이트가 꺼져 있으면
    이 인자와 무관하게 동작은 기존과 동일하다(폴백 블록 미실행). 건설 큐(primary) 호출에서만
    True 를 주고, 연구 큐 등 다른 큐 호출은 기본 False 로 둔다.
    """
    if ctx is not None:
        if tick_index is None:
            tick_index = ctx.tick_index
        if levels is None:
            levels = ctx.levels
        if resource_state is None:
            resource_state = ctx.resource_state
    # 유휴 폴백용 상태(게이트 OFF 면 사용되지 않는다). ctx 가 있어야 폴백을 계산한다.
    fb_personality = ctx.personality_key if ctx is not None else None
    fb_active_targets = ctx.active_targets if ctx is not None else set()
    if tick_index is None:
        tick_index = default_tick_index()
    email = client.email
    # 백오프(자원부족 스킵) 상태키. 기본은 email(홈월드). 식민지 개발은 행성 스코프 키를
    # 넘겨(backoff_key="email#colony<id>") 홈월드/형제 식민지와 백오프를 분리한다 — 자원이
    # 행성별로 독립이라, 한 행성의 자원부족이 다른 행성의 빌드를 막으면 안 되기 때문이다.
    # db_reader 조회는 계속 email 로 한다(전환된 planet_current 기준으로 서버가 해석).
    bkey = backoff_key or email

    done: list[tuple] = []
    last_msg = ""
    # 취소 사유 분류에 쓸 상태(현재자원+용량, 레벨). 없으면 취소가 처음 발생할 때 지연 조회.
    _rs = resource_state
    _lv = levels
    # 사전체크용 잔여 예산: 이번 틱에 성공적으로 큐잉한 항목의 비용을 차감해 나간다.
    # (자원상태는 틱 시작 스냅샷이라 약간 보수적 — 생산 유입분만큼 실제보다 적게 본다.)
    _budget: dict[str, int] | None = None
    if _rs:
        _budget = {r: int(_rs.get(r) or 0) for r in ("metal", "crystal", "deuterium")}

    def _note(msg: str) -> None:
        nonlocal last_msg
        last_msg = msg
        if report is not None:
            report.append(msg)

    def _spend(cost: dict[str, int] | None) -> None:
        """큐잉 성공한 항목의 비용을 사전체크 예산에서 차감(같은 틱 후순위 후보 기준)."""
        if _budget is not None and cost:
            for r in ("metal", "crystal", "deuterium"):
                _budget[r] -= cost[r]

    def _attempt(action: tuple) -> str:
        """단일 후보 1건 시도. 반환: 'stop'(큐 참→중단) | 'done'(집계됨) | 'next'(건너뜀).

        기존 for-루프 본문을 그대로 함수로 뽑은 것(동작 불변) — 정상 후보 루프와 유휴 폴백
        루프가 같은 사전체크/백오프/검증 경로를 공유하게 해 두 경로의 분기(divergence)를 막는다.
        """
        nonlocal last_msg, _rs, _lv
        kind = action[0]
        tid = action[1]
        verifiable = verify and kind in _VERIFY_TABLE
        cost: dict[str, int] | None = None  # 사전체크에서 계산되면 성공 시 예산 차감에 재사용

        # 백오프 중인 대상은 시도하지 않는다(자원부족으로 최근 취소된 것).
        if verifiable and storage.in_backoff(bkey, tid, tick_index):
            _note(f"{_fmt_action(action)} 백오프중")
            return "next"

        # 사전 자원체크: 못 살 것은 add 요청 자체를 보내지 않는다(취소 스팸 = 부하 방지).
        if verifiable and _budget is not None:
            if _lv is None:
                try:
                    _lv = db_reader.get_levels_db(email)
                except Exception:  # noqa: BLE001 - 레벨 미상이면 사전체크 없이 진행
                    _lv = {}
            cost = game_objects.object_cost(tid, (_lv or {}).get(tid, 0) + 1)
            if cost:
                over_cap = any(
                    cost[r] > int(_rs.get(f"{r}_max") or 0) > 0
                    for r in ("metal", "crystal", "deuterium")
                )
                if over_cap:
                    _note(f"{_fmt_action(action)} 사전스킵:용량부족")
                    return "next"
                if any(cost[r] > _budget[r] for r in ("metal", "crystal", "deuterium")):
                    _note(f"{_fmt_action(action)} 사전스킵:자원부족")
                    return "next"

        try:
            res = _do_action(client, action)
        except Exception as exc:  # noqa: BLE001 - 개별 행동 오류는 다음 후보로
            last_msg = f"{_fmt_action(action)} error {type(exc).__name__}"
            return "next"

        if not _is_success(res):
            last_msg = str(res.get("message", res)) if isinstance(res, dict) else str(res)
            if _is_queue_full(res):
                return "stop"  # 큐가 가득 참 → 이 큐엔 더 못 넣는다
            return "next"

        # --- HTTP success. 검증 대상이면 큐 재조회로 실제 생존 확인 ---
        if not verifiable:
            done.append(action)
            return "done"

        try:
            canceled = db_reader.latest_queue_canceled(email, tid, _VERIFY_TABLE[kind])
        except Exception:  # noqa: BLE001 - 재조회 실패 시 보수적으로 성공 처리(과취소 방지)
            _spend(cost)
            done.append(action)
            return "done"

        if canceled != 1:
            # 살아있음(대기/진행/완료) → 성공. 백오프 해제.
            storage.clear_backoff(bkey, tid)
            _spend(cost)
            done.append(action)
            return "done"

        # 취소됨 → 사유 분류 후 처리. 필요한 상태를 지연 조회.
        if _rs is None:
            try:
                _rs = db_reader.get_resource_state_db(email)
            except Exception:  # noqa: BLE001
                _rs = {}
        if _lv is None:
            try:
                _lv = db_reader.get_levels_db(email)
            except Exception:  # noqa: BLE001
                _lv = {}
        target_level = _lv.get(tid, 0) + 1
        reason = storage.classify_cancel(tid, target_level, _rs)
        _note(f"{_fmt_action(action)} 취소:{reason}")
        if reason == "자원부족":
            storage.set_backoff(bkey, tid, tick_index)
        # '용량부족'은 저장고 규칙이 해결하므로 백오프하지 않는다.
        return "next"

    # --- opt-in local bot batch endpoint ---------------------------------
    # This is deliberately below _attempt so both paths share all state and the
    # legacy loop remains byte-for-byte available when the endpoint is OFF/404.
    # The server verifies the newly-created queue row after every action and applies
    # max_success to real accepts, not optimistic HTTP responses.
    _batch_ambiguous = False

    def _run_batch(batch_candidates: list[tuple]) -> bool:
        """Try one ordered batch. True means handled; False means use legacy safely."""
        nonlocal last_msg, _lv, _rs, _batch_ambiguous
        if not economy_batch.enabled_for_account(email):
            return False
        planet_id = int((_rs or {}).get("planet_id") or 0)
        if planet_id <= 0:
            return False

        payload: list[dict] = []
        action_by_id: dict[str, tuple] = {}
        cost_by_id: dict[str, dict[str, int] | None] = {}
        for index, action in enumerate(batch_candidates):
            kind = action[0]
            tid = int(action[1])
            verifiable = verify and kind in _VERIFY_TABLE
            cost: dict[str, int] | None = None

            if verifiable and storage.in_backoff(bkey, tid, tick_index):
                _note(f"{_fmt_action(action)} 백오프중")
                continue

            if verifiable and _budget is not None:
                if _lv is None:
                    try:
                        _lv = db_reader.get_levels_db(email)
                    except Exception:  # noqa: BLE001
                        _lv = {}
                cost = game_objects.object_cost(tid, (_lv or {}).get(tid, 0) + 1)
                if cost:
                    over_cap = any(
                        cost[r] > int((_rs or {}).get(f"{r}_max") or 0) > 0
                        for r in ("metal", "crystal", "deuterium")
                    )
                    if over_cap:
                        _note(f"{_fmt_action(action)} 사전스킵:용량부족")
                        continue
                    # Individual affordability is known before the request.  The
                    # server's optional virtual budget handles deductions between
                    # accepted candidates in this same ordered list.
                    if any(cost[r] > _budget[r] for r in ("metal", "crystal", "deuterium")):
                        _note(f"{_fmt_action(action)} 사전스킵:자원부족")
                        continue

            action_id = f"queue-{index}"
            item = {
                "action_id": action_id,
                "kind": kind,
                "planet_id": planet_id,
                "technology_id": tid,
            }
            if len(action) >= 3:
                item["amount"] = max(1, int(action[2]))
            if cost:
                item["expected_cost"] = {
                    r: max(0, int(cost[r]))
                    for r in ("metal", "crystal", "deuterium")
                }
            payload.append(item)
            action_by_id[action_id] = action
            cost_by_id[action_id] = cost

        # All candidates were deterministically skipped locally.  This is already
        # a fully handled no-op and should not create an empty HTTP request.
        if not payload:
            return True

        try:
            response = economy_batch.try_economy_batch(
                client,
                payload,
                max_success=min(max_items, len(payload)),
                stop_on_queue_full=True,
                budget=dict(_budget) if _budget is not None else None,
            )
        except economy_batch.EconomyBatchError as exc:
            _batch_ambiguous = True
            _note(f"economy-batch error: {exc}")
            return True
        if response is None:
            return False  # endpoint OFF/hidden before execution: legacy is safe

        for result in response.get("results", []):
            action_id = str(result.get("action_id", ""))
            action = action_by_id.get(action_id)
            if action is None:
                continue
            kind = action[0]
            tid = int(action[1])
            if result.get("success") is True and result.get("status") == "accepted":
                if verify and kind in _VERIFY_TABLE:
                    storage.clear_backoff(bkey, tid)
                _spend(cost_by_id.get(action_id))
                done.append(action)
                continue

            code = str(result.get("code") or "")
            if code == "budget_insufficient":
                _note(f"{_fmt_action(action)} 사전스킵:자원부족")
                continue
            if code == "queue_canceled" and verify and kind in _VERIFY_TABLE:
                if _rs is None:
                    try:
                        _rs = db_reader.get_resource_state_db(email)
                    except Exception:  # noqa: BLE001
                        _rs = {}
                if _lv is None:
                    try:
                        _lv = db_reader.get_levels_db(email)
                    except Exception:  # noqa: BLE001
                        _lv = {}
                reason = storage.classify_cancel(tid, (_lv or {}).get(tid, 0) + 1, _rs)
                _note(f"{_fmt_action(action)} 취소:{reason}")
                if reason == "자원부족":
                    storage.set_backoff(bkey, tid, tick_index)
                continue
            if code in {"max_success_reached", "queue_full_stop"}:
                continue
            message = str(result.get("message") or code or result)
            if message:
                last_msg = message
        return True

    batch_handled = _run_batch(candidates)
    if batch_handled:
        # Match the legacy contract: fallback is considered only when no normal
        # candidate survived.  It gets its own request because that decision depends
        # on the first response.  Never continue after an ambiguous transport error.
        if (
            not _batch_ambiguous
            and MINER_IDLE_FALLBACK_ENABLED
            and idle_fallback
            and not done
            and _budget is not None
            and fb_personality is not None
        ):
            try:
                fallback = idle_fallback_candidates(
                    fb_personality, _lv or {}, _rs, fb_active_targets
                )
            except Exception:  # noqa: BLE001
                fallback = []
            if fallback and not _run_batch(fallback):
                batch_handled = False
        if batch_handled:
            return done, last_msg

    for action in candidates:
        if len(done) >= max_items:
            break
        if _attempt(action) == "stop":
            break

    # 유휴(IDLE) 폴백: 정상 후보가 하나도 큐잉되지 못했을 때(잉여 방치)만, 저비용 대체 체인을
    # 같은 사전체크/검증 경로로 시도해 잉여를 소진한다. 게이트 OFF 면 진입하지 않아 동작 불변.
    # _budget is not None(=자원상태 보유)이라야 감당 판정이 가능하고, ctx 로 성격을 받았을 때만
    # 폴백 후보를 만든다. 예외는 절대 틱을 깨지 않도록 흡수한다(방어적).
    if (
        MINER_IDLE_FALLBACK_ENABLED
        and idle_fallback
        and not done
        and _budget is not None
        and fb_personality is not None
    ):
        try:
            for action in idle_fallback_candidates(
                fb_personality, _lv or {}, _rs, fb_active_targets
            ):
                if len(done) >= max_items:
                    break
                if _attempt(action) == "stop":
                    break
        except Exception:  # noqa: BLE001 - 폴백 실패는 절대 틱을 깨뜨리지 않는다
            pass

    return done, last_msg


def _fallback_resource_tick(
    client: OGameClient, email: str, label: str, pkey: str,
    tick_index: int, energy: int | None,
) -> str:
    """levels 조회 실패 시 안전 모드: 기존처럼 자원 건물만 짓되 큐를 여러 건 채운다."""
    candidates = [("build", t) for t in build_candidates(pkey, tick_index, energy)]
    acts, last_msg = _queue_actions(client, candidates, MAX_BUILD_PER_TICK)
    if acts:
        names = " + ".join(_fmt_action(a) for a in acts)
        return f"{email} [{label}] {names} (safe-mode) E={energy}"
    return f"{email} [{label}] nothing buildable (safe-mode, last: {last_msg}) E={energy}"


def tick(
    email: str,
    password: str = "botpass",
    personality_key: str | None = None,
    tick_index: int | None = None,
    client: OGameClient | None = None,
) -> str:
    """봇 1틱 실행. 결과를 한 줄 문자열로 반환한다.

    personality_key 를 주지 않으면 이메일로부터 결정적으로 배정한다.
    client 를 주면 로그인 생략(이미 로그인된 세션 재사용). 안 주면 새로 로그인.
    """
    pkey = personality_key or assign_personality(email)
    p = PERSONALITIES[pkey]
    label = p["label"]
    if tick_index is None:
        tick_index = default_tick_index()

    # 로그인: 실패 시 예외를 그대로 올려 스케줄러가 계정을 스킵하도록 한다.
    if client is None:
        client = OGameClient(email, password)
        client.login()

    # 자원 현황(현재값+에너지+저장 용량) 조회. DB 직독 우선, 실패 시 HTTP 폴백(에너지만).
    # resource_state 는 저장고 규칙(창고 근접도 판정)과 취소 사유 분류에 쓴다. 둘 다 실패해도
    # 건설 자체는 시도한다(에너지/저장고 판단만 생략).
    energy: int | None = None
    resource_state: dict | None = None
    try:
        try:
            resource_state = db_reader.get_resource_state_db(client.email)
            energy = resource_state.get("energy")
        except Exception:  # noqa: BLE001 - DB 실패 시 HTTP 폴백(에너지만, 저장고 규칙은 생략)
            energy = client.get_status().get("energy")
    except Exception:  # noqa: BLE001 - 조회 실패는 치명적이지 않음
        energy = None

    # 이미 큐에 있는 건설 대상(저장고 중복 큐잉 방지용). 실패해도 규칙은 동작.
    active_targets: set[int] = set()
    try:
        active_targets = db_reader.active_build_targets(client.email)
    except Exception:  # noqa: BLE001
        active_targets = set()

    # 성장 상태머신용 레벨 조회. 실패하면 자원 건물만 짓는 안전 모드로.
    try:
        levels = _collect_levels(client)
    except Exception as exc:  # noqa: BLE001 - 조회 실패 시 폴백
        note = _fallback_resource_tick(client, email, label, pkey, tick_index, energy)
        return f"{note} [levels err: {type(exc).__name__}]"

    # ship_targets의 목표치는 계정 전체 상한이다. 현재 행성 주기장만 보면 비행 중이거나
    # 식민지/조선소 큐에 있는 함선이 사라진 것처럼 보여 매 틱 재생산된다. 합계 조회가
    # 실패해도 건설·연구 틱은 계속하되, 공용 함선 배치만 아래 ctx에서 fail-closed한다.
    account_ship_totals: dict[int, int] | None = None
    try:
        account_ship_totals = db_reader.get_account_ship_totals_db(client.email)
    except Exception:  # noqa: BLE001 - 함선 건조만 보수적으로 정지
        account_ship_totals = None

    done: list[str] = []
    last_msg = ""
    report: list[str] = []  # 취소 사유/백오프 스킵 기록(로그용)

    # 이번 틱 상태를 공용 컨텍스트로 묶는다. AI 경로(policy_engine)도 같은 빌더로 같은
    # 객체를 만들어 계획 계층에 넘겨, 한쪽만 파라미터를 누락하는 회귀를 구조적으로 막는다.
    plan_key = planning_personality(email, pkey)
    ctx = build_tick_context(
        plan_key, levels, tick_index,
        energy=energy, resource_state=resource_state, email=email,
        active_targets=active_targets, allow_satellite=True,
        account_ship_totals=account_ship_totals,
        require_account_ship_totals=True,
    )

    # primary: 자원 건물/시설 (공용 건설 큐 → 한 틱에 최대 MAX_BUILD_PER_TICK 건 쌓기).
    # ctx 로 resource_state/state_key/active_targets/allow_satellite 가 전달돼 저장고·위성 규칙 활성.
    primary = plan_primary(ctx=ctx)
    acts, last_msg = _queue_actions(
        client, primary, MAX_BUILD_PER_TICK, ctx=ctx, report=report,
        idle_fallback=True,
    )
    for a in acts:
        done.append(_fmt_action(a))

    # secondary: 연구(연구 큐 → 여러 건) + 함선(무제한 조선소 큐 → 1배치).
    # 연구/조선소는 건설 큐와 별개라 primary 와 동시에 진행된다.
    # ctx 의 energy/resource_state 로 에너지 위성 경로(적자 시 위성 건조)가 활성화된다.
    secondary = plan_secondary(ctx=ctx)
    research_c = [a for a in secondary if a[0] == "research"]
    if research_blocked_by_lab(active_targets, acts):
        research_c = []
    ship_c = [a for a in secondary if a[0] == "ship"]
    res_acts, msg_r = _queue_actions(
        client, research_c, MAX_RESEARCH_PER_TICK, ctx=ctx, report=report,
    )
    for a in res_acts:
        done.append(_fmt_action(a))
    ship_act, msg_s = _try_first(client, ship_c)
    if ship_act is not None:
        done.append(_fmt_action(ship_act))
    if not last_msg:
        last_msg = msg_r or msg_s

    # 규칙봇/AI가 완전히 같은 후행 파이프라인을 사용한다. 새 함대·행성 훅을 한쪽에만
    # 추가하는 이중경로 회귀를 막기 위해 순서는 post_actions 한 곳에서 관리한다.
    done.extend(post_actions.run_post_actions(
        client, email, levels, pkey, tick_index, energy=energy,
    ))

    # 취소/백오프 기록이 있으면 로그 끝에 압축해 붙인다(정체 원인 관측용).
    rpt = f" | {'; '.join(report)}" if report else ""
    if done:
        return f"{email} [{label}] {' + '.join(done)} E={energy}{rpt}"
    return f"{email} [{label}] nothing buildable (last: {last_msg}) E={energy}{rpt}"


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "bot001@bots.local"
    password = sys.argv[2] if len(sys.argv) > 2 else "botpass"
    print(tick(email, password))
