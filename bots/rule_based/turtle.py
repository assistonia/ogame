"""터틀(turtle) 규칙봇의 방어 건조 행동.

miner_bot.tick() 이 자원/시설/연구 건설을 마친 뒤, 터틀 성격 봇에 한해
`defense_tick()` 을 호출해 **한 틱에 방어 1건**을 건조한다. 나머지 성격 봇은
이 모듈을 전혀 타지 않는다.

설계 근거: docs/strategies.md §3/§7 "터틀".
  - 약탈 가치를 0으로: 잉여 자원을 방어시설로 묶어 털 게 없게 만든다.
  - 계층 구조: 총알받이(로켓런처·경레이저) + 주포(가우스·이온) + 실드돔.
  - 비율 정설: 총알받이 8당 가우스 1·이온 1, 총알받이 50 도달 시 소형 실드돔,
    이후 대형 실드돔.
  - 함대 파견은 하지 않는다(순수 수비).

트리거(잉여 판정 — 두 경로의 큰 쪽):
  1) 예비비 경로: 건설 후 남은 자원이 "봇의 실제 다음 건설 목표 1건 비용 × RESERVE_MULT"를
     넘으면 그 초과분을 잉여로 본다. 예비비를 '가장 비싼 광산 × 2'(구 방식)가 아니라
     '다음 목표 1건'에 연동해, 광산이 올라도 예비비가 폭증해 잉여 창이 영구히 닫히던
     구조적 결함을 없앤다(성장과 방어가 양립).
  2) 용량 연동 경로: 자원이 저장 용량(planets.*_max)의 CAP_RATIO(85%) 이상이면 예비비를
     무시하고 (용량×CAP_RATIO) 초과분을 잉여로 본다. 어차피 창고에서 넘쳐 버려질 생산이라
     방어로 묶는 게 순이익이고, 저장고 규칙(storage.py, 95% 반응)보다 먼저 흡수한다.

자기 교정(silent success 대비):
  raider.py 와 달리 쿨다운 상태파일이 필요 없다. 다음 틱에 방어 수량을 다시 조회해
  목표 대비 부족분을 재계산하므로, 이번 틱 건조가 조용히 실패했으면 같은 후보가 다음
  틱에 자동 재시도된다.

안전장치:
  - defense_tick 은 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호).
  - 조선소(21)≥1 이 아니면 방어 자체가 불가하므로 아무것도 하지 않는다.
  - 방어 건조는 turtle.py 안의 helper 로 /defense/add-buildrequest 를 직접 호출한다
    (client.py 는 수정 불가 → 조선소 add-buildrequest 와 동일 패턴을 재사용).
"""
from __future__ import annotations

import re

from bots.rule_based.personality import GROWTH, next_facility, prereqs_met
from bots.shared.game_objects import object_cost

# ---------------------------------------------------------------------------
# 방어 유닛 정의 (DefenseObjects.php 에서 확인한 값)
# ---------------------------------------------------------------------------
ROCKET_LAUNCHER = 401
LIGHT_LASER = 402
HEAVY_LASER = 403
GAUSS_CANNON = 404
ION_CANNON = 405
PLASMA_TURRET = 406
SMALL_SHIELD_DOME = 407
LARGE_SHIELD_DOME = 408

# technologyId -> (메탈, 크리스탈, 듀테륨) 단가. DefenseObjects.php price 그대로.
DEF_COST: dict[int, tuple[int, int, int]] = {
    ROCKET_LAUNCHER: (2000, 0, 0),
    LIGHT_LASER: (1500, 500, 0),
    GAUSS_CANNON: (20000, 15000, 2000),
    ION_CANNON: (2000, 6000, 0),
    SMALL_SHIELD_DOME: (10000, 10000, 0),
    LARGE_SHIELD_DOME: (50000, 50000, 0),
}

# 총알받이(fodder)로 세는 유닛.
FODDER_IDS: tuple[int, ...] = (ROCKET_LAUNCHER, LIGHT_LASER)

# 비율 정설: 총알받이 N당 주포 1문(§3). N당 가우스 1, N당 이온 1.
FODDER_PER_CANNON = 8
# 총알받이 이 수 도달 시 소형 실드돔 → 이후 대형 실드돔(§3).
FODDER_FOR_SHIELD_DOME = 50

# 한 틱에 건조할 총알받이 최대 수량(잉여를 여러 틱에 나눠 소비 → 자기 교정).
FODDER_BATCH = 20
# 주포는 비율 맞춤이라 한 틱 배치를 작게.
CANNON_BATCH = 5

# 예비비 배수: 다음 건설 목표 1건 비용 × 이 배수만큼만 남기고 초과분을 방어로.
# (구: "가장 비싼 광산 × 2" → 광산이 오를수록 예비비가 폭증해 잉여 창이 영구히 닫히던
#  구조적 결함. 이제 '봇의 실제 다음 목표 1건'에 연동해 성장과 방어가 양립한다.)
RESERVE_MULT = 1.1

# 용량 연동 규칙: 자원이 저장 용량의 이 비율 이상이면 예비비를 무시하고 (용량×비율) 초과분을
# 잉여로 본다. 어차피 창고에서 넘쳐 버려질 생산이므로 방어로 묶는 게 순이익이다.
# 저장고 규칙(storage.py)이 95%에서 반응하므로, 그 전에 방어가 초과분을 흡수한다(자연 정합).
CAP_RATIO = 0.85

# 듀테륨 최소 예비(가우스포가 2000/문 소모). 용량 연동 규칙과 별개의 하한.
DEUT_RESERVE = 5000


# ---------------------------------------------------------------------------
# 터틀 반사(turtle reflex) — 피격 시 방어 재건 최우선화 (roadmap A-b)
# ---------------------------------------------------------------------------
# 2026-07-12 독립 안전성 게이트를 통과해 선행 활성했다. 평시에는 baseline만 갱신하고,
# 실제 피격 손실이 감지될 때만 규칙봇·AI 터틀 양쪽의 재건 예산을 일시 상향한다.
# False로 내리고 루프를 재시작하면 즉시 kill switch다.
TURTLE_REFLEX_ENABLED = True


# ---------------------------------------------------------------------------
# 방어 건조 helper (client.py 수정 불가 → 조선소 패턴 재사용)
# ---------------------------------------------------------------------------
def _is_success(res: dict) -> bool:
    """add-buildrequest 응답 성공 판정(client._add_buildrequest 반환 형식과 동일)."""
    if not isinstance(res, dict):
        return False
    return res.get("status") == "success" or res.get("success") is True


# 자원 현황은 overview 의 `id="resources_X" ... data-raw="N"` 에서 정밀값을 읽는다.
# client.get_status() 는 표시용 축약(예: "1.999Mn")을 int() 하다가 큰 값에서 깨지는데,
# 터틀은 정확히 자원이 넘칠 때(=큰 값) 동작해야 하므로 data-raw 를 직접 파싱한다.
_RESOURCE_RE = {
    key: re.compile(rf'id="resources_{key}".*?data-raw="([\d.]+)"', re.DOTALL)
    for key in ("metal", "crystal", "deuterium", "energy")
}


def read_resources(client) -> dict[str, int]:
    """자원 현황 + 저장 용량 상한을 반환. DB 직독 우선, HTTP 폴백.

    DB 직독(db_reader.get_resource_state_db)은 metal/crystal/deuterium/energy 정밀값에
    더해 저장 용량 상한(*_max)까지 준다. 터틀의 잉여 판정은 용량 연동 규칙(CAP_RATIO)에서
    이 상한을 쓰므로 get_status_db 가 아니라 get_resource_state_db 를 쓴다.
    HTTP 폴백은 overview 의 `id="resources_X" ... data-raw="N"` 정밀값(용량 없음 → 용량
    연동 규칙은 비활성, 예비비 경로만 동작)을 파싱한다.
    파싱/조회 실패한 항목은 dict 에서 빠진다(호출부가 없으면 0/None 취급).
    """
    from bots.shared import db_reader

    try:
        return db_reader.get_resource_state_db(client.email)
    except Exception:  # noqa: BLE001 - DB 실패 시 HTTP 폴백(용량 없음)
        html = client.overview_html()
        out: dict[str, int] = {}
        for key, rx in _RESOURCE_RE.items():
            m = rx.search(html)
            if m:
                out[key] = int(float(m.group(1)))
        return out


def build_defense(
    client, technology_id: int, amount: int, *, levels: dict[int, int] | None = None
) -> dict:
    """방어시설 건조 요청. /defense/add-buildrequest 는 조선소와 같은
    AbstractUnitsController(technologyId + amount)를 쓴다(DefenseController).

    client._add_buildrequest 를 그대로 재사용한다(토큰 재발급·CSRF·헤더 처리 동일).
    """
    from bots.rule_based import production_capacity

    snapshot = production_capacity.production_queue_snapshot(client.email)
    if technology_id in (SMALL_SHIELD_DOME, LARGE_SHIELD_DOME) and (
        production_capacity.pending_unit_amount(snapshot, technology_id) > 0
    ):
        return {"success": False, "message": "unique defense already pending"}
    requested = max(0, int(amount))
    amount = production_capacity.cap_ship_amount_from_snapshot(
        snapshot,
        technology_id,
        requested,
        levels=levels or {},
    )
    if amount < 1 or (requested > 1 and amount < min(requested, 5)):
        return {"success": False, "message": "unit queue horizon hold"}
    return client._add_buildrequest(
        "/defense/add-buildrequest", "/defense", technology_id, extra={"amount": amount}
    )


# ---------------------------------------------------------------------------
# 잉여(surplus) 산정
# ---------------------------------------------------------------------------
def _next_build_cost(levels: dict[int, int]) -> tuple[int, int]:
    """터틀의 다음 건설 목표 1건의 (메탈, 크리스탈) 비용. 예비비 산정용.

    miner_bot 의 primary 계층과 같은 순서로 '다음에 지을 것'을 추정한다:
      1) 자원 floor 미달분이 있으면 그 중 가장 비싼 건물(근시일 최댓값을 감당).
      2) floor 충족이면 다음 시설(next_facility).
      3) 둘 다 없으면(정상 성장기) 자원 건물 4종 중 가장 비싼 다음 레벨.
    예비비를 '가장 비싼 근시일 목표 1건'에 맞춰, 그 목표를 굶기지 않으면서도 (구 방식처럼)
    광산 레벨에 무한 비례해 커지지 않게 한다 — 넘치는 초과분은 용량 연동 규칙(CAP_RATIO)이
    별도로 방어에 흘려보낸다.
    """
    floor = GROWTH["turtle"]["resource_floor"]
    below = [t for t in (1, 2, 3, 4) if levels.get(t, 0) < floor.get(t, 0)]
    if below:
        cands = below
    else:
        nf = next_facility("turtle", levels)
        cands = [nf] if nf is not None else [1, 2, 3, 4]

    best_m = best_c = 0
    for tid in cands:
        cost = object_cost(tid, levels.get(tid, 0) + 1)
        if cost is None:
            continue
        if cost["metal"] + cost["crystal"] > best_m + best_c:
            best_m, best_c = cost["metal"], cost["crystal"]
    return best_m, best_c


def _surplus_one(have: int, reserve: int, cap: int | None) -> int:
    """자원 1종의 잉여. 예비비 경로와 용량 연동 경로 중 큰 값(둘 다 못 넘으면 0).

    - 예비비 경로 : have − reserve.
    - 용량 연동   : have ≥ cap×CAP_RATIO 이면 have − cap×CAP_RATIO(넘칠 생산).
    cap 을 모르면(HTTP 폴백 등 None/0) 용량 연동은 비활성 — 예비비 경로만.
    """
    surplus = max(0, have - reserve)
    if cap and cap > 0 and have >= cap * CAP_RATIO:
        surplus = max(surplus, have - int(cap * CAP_RATIO))
    return surplus


def _surplus(status: dict, levels: dict[int, int]) -> tuple[int, int, int]:
    """건설 후 남은 자원의 (메탈, 크리스탈, 듀테륨) 잉여. 각 자원 _surplus_one 참조."""
    metal = status.get("metal") or 0
    crystal = status.get("crystal") or 0
    deut = status.get("deuterium") or 0
    tgt_m, tgt_c = _next_build_cost(levels)

    surplus_m = _surplus_one(metal, int(tgt_m * RESERVE_MULT), status.get("metal_max"))
    surplus_c = _surplus_one(crystal, int(tgt_c * RESERVE_MULT), status.get("crystal_max"))
    # 듀테륨은 광산 업그레이드에 안 쓰이므로 소액 예비만(가우스가 2000/문 소모).
    surplus_d = _surplus_one(deut, DEUT_RESERVE, status.get("deuterium_max"))
    return surplus_m, surplus_c, surplus_d


def _affordable(cost: tuple[int, int, int], sm: int, sc: int, sd: int) -> int:
    """잉여(sm,sc,sd) 예산으로 단가 cost 유닛을 몇 개 살 수 있는지."""
    m, c, d = cost
    limits = []
    if m > 0:
        limits.append(sm // m)
    if c > 0:
        limits.append(sc // c)
    if d > 0:
        limits.append(sd // d)
    return min(limits) if limits else 0


# ---------------------------------------------------------------------------
# 방어 조합 선정 (계층 + 비율)
# ---------------------------------------------------------------------------
def plan_defense(counts: dict[int, int], levels: dict[int, int]) -> list[tuple[int, int]]:
    """현재 방어 수량(counts)과 선행조건(levels)으로 이번 틱 건조 후보를 우선순위대로 반환.

    각 후보는 (technologyId, 목표수량). 앞에서부터 시도해 잉여 예산으로 감당 가능한
    첫 후보를 실제로 건조한다.

    우선순위(계층 구조):
      1) 실드돔: 총알받이 50 도달 시 소형(1), 소형 보유 시 대형(1) — 일회성 보험.
      2) 주포 비율 보정: 총알받이 8당 가우스 1 / 이온 1 목표에 부족한 쪽.
      3) 총알받이 증설: 경레이저 우선(순항함 연사 회피), 그 뒤 로켓런처(메탈만).
    """
    fodder = sum(counts.get(t, 0) for t in FODDER_IDS)
    candidates: list[tuple[int, int]] = []

    # 1) 실드돔(계층 보험) — 임계치 도달 시 각 1문.
    if fodder >= FODDER_FOR_SHIELD_DOME:
        if counts.get(SMALL_SHIELD_DOME, 0) == 0 and prereqs_met(SMALL_SHIELD_DOME, levels):
            candidates.append((SMALL_SHIELD_DOME, 1))
        elif counts.get(SMALL_SHIELD_DOME, 0) >= 1 and counts.get(LARGE_SHIELD_DOME, 0) == 0 \
                and prereqs_met(LARGE_SHIELD_DOME, levels):
            candidates.append((LARGE_SHIELD_DOME, 1))

    # 2) 주포 비율 보정(총알받이 8당 1문).
    cannon_target = fodder // FODDER_PER_CANNON
    for cannon in (GAUSS_CANNON, ION_CANNON):
        have = counts.get(cannon, 0)
        if have < cannon_target and prereqs_met(cannon, levels):
            candidates.append((cannon, min(cannon_target - have, CANNON_BATCH)))

    # 3) 총알받이 증설. 경레이저 우선, 로켓런처(메탈만) 보조.
    #    경레이저가 아직 선행조건 미달이면 로켓런처만으로 총알받이를 쌓는다.
    if prereqs_met(LIGHT_LASER, levels):
        candidates.append((LIGHT_LASER, FODDER_BATCH))
    if prereqs_met(ROCKET_LAUNCHER, levels):
        candidates.append((ROCKET_LAUNCHER, FODDER_BATCH))

    return candidates


# ---------------------------------------------------------------------------
# 방어 수량 조회 + 터틀 반사(피격 시 재건 예산 상향)
# ---------------------------------------------------------------------------
def _read_defense_counts(client) -> dict[int, int]:
    """현재 방어/함선/건물 수량 조회. DB 직독 우선(방어 수량은 planets 컬럼), /defense
    HTTP 폴백, 둘 다 실패면 {}. (기존 _defense_tick_inner 인라인 조회를 함수로 분리.)"""
    from bots.shared import db_reader
    try:
        counts = db_reader.get_levels_db(client.email)
    except Exception:  # noqa: BLE001 - DB 실패 시 /defense 페이지 파싱
        try:
            counts = client.get_levels(pages=("defense",))
        except Exception:  # noqa: BLE001
            return {}
    try:
        from bots.rule_based import production_capacity

        snapshot = production_capacity.production_queue_snapshot(client.email)
        for item in snapshot.unit_items:
            if 401 <= item.object_id <= 408:
                counts[item.object_id] = (
                    int(counts.get(item.object_id, 0)) + item.remaining_amount
                )
    except Exception:  # noqa: BLE001 - completed counts remain usable
        pass
    return counts


def _defense_value(counts: dict[int, int]) -> int | None:
    """현재 방어 유닛의 총 건조 가치(메탈+크리스탈+듀테륨). 반사의 상태 신호.

    counts 가 비어 있으면(방어 수량 조회 실패) None 을 돌려 반사 판정이 전투보고 폴백으로
    넘어가게 한다 — 조회 실패의 0 을 '전량 손실'로 오인하지 않기 위함. get_levels_db 는
    방어가 없어도 채워진 dict 를 주므로, {} 는 곧 조회 실패를 뜻한다.
    """
    if not counts:
        return None
    total = 0
    for tid, cost in DEF_COST.items():
        n = counts.get(tid, 0)
        if n:
            total += n * (cost[0] + cost[1] + cost[2])
    return total


def _apply_reflex(
    email: str, counts: dict[int, int], status: dict, sm: int, sc: int, sd: int,
) -> tuple[int, int, int, bool]:
    """반사가 켜졌을 때 방어 재건 예산을 상향한다.

    (email, 현재 방어수량, 자원현황, 잉여 sm/sc/sd) → (sm, sc, sd, reflex_active).
    피격(방어 손실)이 감지되면 예비비 기반 잉여 대신 '건설 후 남은 실제 자원'까지 방어에
    쓸 수 있게 예산을 올린다 — 메탈/크리스탈은 전액(post_actions 는 광산/시설 건설을 먼저
    끝낸 뒤 이 훅을 부르므로 여기 남은 자원은 이미 성장 몫을 뺀 잔여다), 듀테륨은
    DEUT_RESERVE 만 남긴다. 재무장 상한은 threat 의 피크 로직이 담당한다(방어가 피크로
    회복되면 반사 자동 해제). 피격이 없으면 예산을 그대로 둔다(오늘 동작).

    어떤 경우에도 예외를 올리지 않는다(판정 실패 = 반사 없음).
    """
    try:
        from bots.rule_based import threat
        state = threat.assess_turtle_threat(email, _defense_value(counts))
    except Exception:  # noqa: BLE001 - 반사 판정 실패가 방어 틱을 깨지 않게
        return sm, sc, sd, False
    if not state.under_reflex:
        return sm, sc, sd, False
    metal = int(status.get("metal") or 0)
    crystal = int(status.get("crystal") or 0)
    deut = int(status.get("deuterium") or 0)
    return max(sm, metal), max(sc, crystal), max(sd, max(0, deut - DEUT_RESERVE)), True


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def defense_tick(
    client, email: str, levels: dict[int, int], *,
    energy: int | None = None, defense_ratio: float | None = None,
    strategy: dict | None = None,
) -> str | None:
    """터틀 봇의 방어 건조 1건. 로그 문자열 또는 None(행동 없음) 반환.

    levels 는 miner_bot 이 이미 조회한 건물/시설 레벨 dict.
    이 함수는 어떤 경우에도 예외를 올리지 않는다(루프 보호).

    energy 는 miner_bot 이 이번 틱에 조회한 현재 에너지다. 평시 적자(energy < 0)면 태양광을
    우선하지만, 실제 피격으로 reflex가 켜진 동안에는 방어 재건을 더 미루지 않는다. 방어시설은
    에너지를 소비하지 않고, 반복 약탈 손실이 에너지 복구의 기회비용보다 커질 수 있기 때문이다.

    defense_ratio 는 AI 정책(policy_engine)이 넘기는 선택적 오버라이드다: 잉여 자원의
    이 비율(0~1)만 방어로 돌린다(나머지는 경제에 남긴다). None 이면 잉여 전부를 방어로
    쓰는 기본 동작 — 규칙봇 호출부는 이 인자를 주지 않으므로 동작이 그대로 유지된다.
    """
    try:
        return _defense_tick_inner(
            client, email, levels, energy=energy, defense_ratio=defense_ratio,
            strategy=strategy,
        )
    except Exception as exc:  # noqa: BLE001 - 터틀 버그가 빌드틱/루프를 깨지 않게
        return f"turtle err {type(exc).__name__}: {exc}"


def _defense_tick_inner(
    client, email: str, levels: dict[int, int], *,
    energy: int | None = None, defense_ratio: float | None = None,
    strategy: dict | None = None,
) -> str | None:
    # 조선소 없으면 어떤 방어도 불가 → 스킵.
    if levels.get(21, 0) < 1:
        return None

    config = strategy if isinstance(strategy, dict) else None
    if config is not None and config.get("mode", "adaptive") == "off":
        return None

    # 건설 후 남은 자원(잉여 판정용). overview data-raw 정밀값. 실패 시 이번 틱은 쉰다.
    try:
        status = read_resources(client)
    except Exception:  # noqa: BLE001
        return "turtle idle (자원 조회 실패)"
    if status.get("metal") is None:
        return "turtle idle (자원 파싱 실패)"

    sm, sc, sd = _surplus(status, levels)
    # AI 정책: 잉여의 일부만 방어로(나머지는 경제에 남긴다). None 이면 전량(기본).
    if defense_ratio is not None and 0 <= defense_ratio < 1:
        sm, sc, sd = int(sm * defense_ratio), int(sc * defense_ratio), int(sd * defense_ratio)

    # 터틀 반사: 최근 피격으로 방어를 잃었으면 예비비를 넘어 잔여 자원까지 방어
    # 재건에 투입한다(경제 예약보다 재무장 우선). 반사는 잉여 게이트 앞에서 판정해야 하므로
    # 방어 수량을 먼저 읽는다. kill switch가 OFF면 이 블록을 건너뛰고 counts는 잉여
    # 게이트 뒤에서 읽힌다.
    counts: dict[int, int] | None = None
    reflex = False
    rebuild_on_attack = config is None or bool(config.get("rebuild_on_attack", True))
    if TURTLE_REFLEX_ENABLED and rebuild_on_attack:
        counts = _read_defense_counts(client)
        sm, sc, sd, reflex = _apply_reflex(email, counts, status, sm, sc, sd)

    # 평시에는 에너지 복구가 먼저다. 단, 이미 맞고 있는 계정은 같은 조건 때문에 영원히
    # 무방비로 남지 않도록 reflex 재건을 우선한다.
    if energy is not None and energy < 0 and not reflex:
        return "turtle hold (에너지 우선)"

    # LLM AI는 저장 자원뿐 아니라 앞으로 쌓일 실제 시간당 생산량까지 보고 방어 목표를
    # 직접 선택한다. 규칙봇(strategy=None)은 종전 잉여 기반 터틀 동작을 유지한다.
    target_value: float | None = None
    if config is not None:
        if counts is None:
            counts = _read_defense_counts(client)
        current_value = _defense_value(counts)
        if current_value is None:
            return "turtle idle (방어 조회 실패)"
        try:
            from bots.shared import db_reader

            production = db_reader.get_production_per_hour_db(email)
        except Exception:  # noqa: BLE001 - 생산량 미상이면 저장 자원만으로 판단
            production = {"metal": 0.0, "crystal": 0.0, "deuterium": 0.0}
        hours = max(0.0, float(config.get("production_hours", 6.0) or 0.0))
        multiplier = max(
            0.0, float(config.get("stored_loot_multiplier", 0.5) or 0.0)
        )
        exposed = sum(float(status.get(key, 0) or 0.0) for key in (
            "metal", "crystal", "deuterium"
        ))
        exposed += hours * sum(float(production.get(key, 0) or 0.0) for key in (
            "metal", "crystal", "deuterium"
        ))
        # 서버 기본 약탈률 50%. LLM이 고른 multiplier만큼 방어 투자 목표를 잡는다.
        target_value = exposed * 0.5 * multiplier
        if current_value >= target_value:
            return None

    if sm <= 0 and sc <= 0:
        return "turtle hold (잉여 없음 — 경제 우선)"

    # 현재 방어 수량 조회. DB 직독 우선(방어 수량은 planets 테이블 컬럼), HTTP 폴백.
    if counts is None:
        counts = _read_defense_counts(client)

    candidates = plan_defense(counts, levels)
    for tid, want in candidates:
        cost = DEF_COST[tid]
        amount = min(want, _affordable(cost, sm, sc, sd))
        if amount < 1:
            continue
        res = build_defense(client, tid, amount, levels=levels)
        if _is_success(res):
            from bots.rule_based.personality import tech_label  # 지연 import
            tag = "REFLEX-DEF" if reflex else "DEF"
            target_note = (
                f", 방어~{int(_defense_value(counts) or 0)}/{int(target_value)}"
                if target_value is not None else ""
            )
            return f"turtle {tag} {tech_label(tid)}({tid})x{amount} (총알받이~{sum(counts.get(t,0) for t in FODDER_IDS)}{target_note})"
        # 큐 참·선행조건·자원 부족 등 → 다음 후보로(자기 교정은 다음 틱 재조회가 담당)

    return "turtle idle (건조 후보 없음/예산 부족)"
