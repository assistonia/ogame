"""비전투 성격(채굴러/탐험가/하이브리드)의 **최소 방어선(base defense line)**.

## 왜 (배경 — 순수 무방비 철학의 폐기)
종전 설계는 miner/explorer/hybrid 를 "순수 무방비"로 굴렸다(방어 ~0). 실전 오게임에서
채굴러도 **최소 방어는 유지**한다: "털러 오는 비용(함대 손실+연료) > 약탈 가치"가 되게
만들면, 레이더의 함대 사이징 시뮬이 그 공격을 EV-음수로 채점해 스스로 거른다. 이 모듈은
그 최소선 하나를 채운다 — 무적이 목적이 아니라 **약탈 채산성만 무너뜨리는** 얇은 방어벽이다.

터틀(turtle.py)은 이미 잉여를 전량 방어로 묶는 별도 정체성이므로 여기서 건드리지 않는다.
레이더(raider)도 대상이 아니다. 이 모듈은 miner/explorer/hybrid 세 성격에만 적용된다.

## 앵커: 약탈 가치(stored-loot) — 일일 생산이 아니다
방어 목표는 "지금 창고에 있는 약탈 가능 자원"에 맞춘다. 서버 기본 약탈률은 저장 자원의
50%(SettingsService)이므로 **약탈 가치 = 현재 저장 자원(메탈 환산) × 0.5** 다. 구설계의
×100-일일-생산 앵커는 폐기했다 — 그건 실제 약탈 가능액의 수십 배라 로켓런처 수천 문을
요구해 채굴러가 터틀보다 두꺼워지고 성격 랭킹이 붕괴했다.

## 무엇을 (한 틱 1건, 자기 교정)
post_actions 가 경제(광산/시설/연구/함선)를 먼저 큐잉한 **뒤** 이 훅을 부른다. 남은 잉여로
방어를 한 틱에 1건 건조한다. 다음 틱에 방어 수량을 다시 조회해 부족분을 재계산하므로
(터틀과 동일) 조용히 실패한 건조는 자동 재시도된다.

  1) 최소선 판정 : 현재 방어 가치 < (약탈 가치 × BASE_DEFENSE_LOOT_MULT) 이면 부족.
  2) 건조        : fodder-first. 로켓런처(401, 메탈만·조선소1). 총알받이 ~50 도달 +
                   선행조건(shielding2) 충족 시 소형 실드돔(407) 1문(strategies.md §3).
  3) 동적 성장   : 고정 총량 상한 없이 현재 약탈 가능 자원에 비례해 방어 목표도 함께 성장한다.
  4) 적응        : 최근 피격(threat 상태-피크 트리거)이면 목표 배수를 ADAPT 로 올렸다가,
                   진정되면 baseline 으로 감쇠(threat.py 의 쿨다운/피크리셋 담당).

## 가치 척도(메탈 환산, 3:2:1)
약탈 가치·방어 가치를 모두 metal-equivalent(metal=1, crystal=1.5, deut=3; investment.py /
strategies.md §1 의 3:2:1 교환비)로 환산해 같은 척도로 비교한다. 이렇게 해야 크리스탈/듀트
편중 자원이 저평가되지 않고, 배수/상한이 자원 구성과 무관하게 일관된다.

## 플래그 (2026-07-12 게이트 통과 후 활성)
실질자산 추세 게이트(`scripts/base_defense_gate.py`)가 READY_TREND를 확인해 활성했다.
규칙봇·AI(공용 post_actions 경로) 양쪽 miner/explorer/hybrid에 적용되며, 함대 사이징
섀도우가 채굴러-타깃 공격 EV 하락을 계속 측정한다. 긴급 비활성은 아래 플래그 한 줄로 한다.

## 안전
- base_defense_tick 은 어떤 경우에도 예외를 올리지 않는다(빌드틱/루프 보호).
- 조회 실패(자원·방어)면 이번 틱은 조용히 쉰다(잘못된 0 으로 오판하지 않는다).
- 방어 건조 helper·object id·비용표는 turtle.py 것을 그대로 재사용한다(중복 정의 회피).
"""
from __future__ import annotations

from bots.rule_based import threat, turtle
from bots.rule_based.personality import (
    GROWTH,
    next_facility,
    prereqs_met,
    tech_label,
)
from bots.shared.game_objects import object_cost

# ---------------------------------------------------------------------------
# 플래그 & 적용 대상
# ---------------------------------------------------------------------------
# 2026-07-12 READY_TREND 통과 후 ON. False로 내리고 루프를 재시작하면 즉시 kill switch다.
MINER_BASE_DEFENSE_ENABLED = True

# 이 최소선은 비전투 채굴형 3종에만 적용한다. 터틀(잉여 전량 방어)·레이더(공격형)는 제외.
BASE_DEFENSE_PERSONALITIES = frozenset({"miner", "explorer", "hybrid", "raider"})

# ---------------------------------------------------------------------------
# 앵커: 약탈 가치(stored-loot) 배수
# ---------------------------------------------------------------------------
# 서버 기본 약탈률: 공격자는 방어측 저장 자원의 50%까지 훔친다. 약탈 가치 = 저장자원(meq)×이 값.
LOOT_FRACTION = 0.50

# 최소선 목표: 방어 건조 가치가 (약탈 가치 × 이 배수) 미만이면 잉여로 방어를 쌓는다.
# 근거(battle_sim 실측, 2026-07-12): 현 레이더 코호트는 최대 ~15 경전투기(무기4/실드3/장갑2)
# 수준이라, 6라운드 준결정적 전투에서 로켓런처 ~80문이면 레이더 승률이 0으로 무너져 약탈이
# 실현되지 않고 손실만 남는다(EV≪0). "약탈 가치의 절반"어치 방어면 그 문턱을 크게 넘는다.
# 부유한 채굴러는 이 값이 커도 아래 HARD CAP 에 먼저 걸려 터틀 밑으로 묶인다.
BASE_DEFENSE_LOOT_MULT = 0.50
# 적응: 최근 피격(threat 상태-피크)이면 목표를 약탈 가치 전액까지 올린다(여전히 HARD CAP 안).
ADAPT_DEFENSE_LOOT_MULT = 1.00

# ---------------------------------------------------------------------------
# 과거 고정 상한(호환용 상수)
# ---------------------------------------------------------------------------
# 앵커 계산이 어떻게 나오든, 채굴형 방어 가치는 터틀 코호트 중앙값의 50%를 절대 못 넘는다.
# "수식이 이상해도 랭킹이 막는다." 터틀 중앙값은 매 틱 전수 조회(비쌈) 대신 아래 1회 DB
# 스냅샷을 캐시 상수로 둔다 — 터틀은 방어를 해체하지 않아 중앙값이 단조 증가하므로, 지금
# 값으로 고정한 CAP 은 시간이 갈수록 오히려 더 보수적(랭킹 안전)이 된다.
#
# 산출(2026-07-12, laravel DB, botread 읽기전용):
#   personality='turtle' 계정(n=18)의 방어 총가치 중앙값(메탈 환산 3:2:1, DefenseObjects price)
#   = 1,179,250 (= 로켓런처 ~590문 상당). 참고: 채굴/하이브리드 방어 중앙값은 0.
#   각 계정 planets 의 rocket_launcher..large_shield_dome 수량 × 단가(meq) 합의 중앙값.
TURTLE_COHORT_MEDIAN_DEFENSE_VALUE = 1_179_250.0
HARD_CAP_FRACTION_OF_TURTLE = 0.50
# 채굴형 방어 가치 절대 상한(메탈 환산). = 0.5 × 터틀 중앙값 = 로켓런처 ~295문 상당.
HARD_CAP_VALUE = float("inf")

# 한 틱에 건조할 총알받이(로켓런처) 최대 수량. 실제 수량은 CAP 헤드룸·잉여로 더 잘린다.
BASE_DEFENSE_BATCH = 10

# 예비비 배수·듀트 하한: turtle 과 동일 개념(다음 건설 1건을 굶기지 않게 예약분을 뺀 잉여만).
RESERVE_MULT = 1.1
DEUT_RESERVE = 5000

# 메탈 환산 교환비(3:2:1). investment._RESOURCE_VALUES / strategies.md §1 과 동일.
RESOURCE_VALUE: dict[str, float] = {"metal": 1.0, "crystal": 1.5, "deuterium": 3.0}


# ---------------------------------------------------------------------------
# 가치 산정(메탈 환산)
# ---------------------------------------------------------------------------
def _meq(metal: float, crystal: float, deuterium: float) -> float:
    """(메탈, 크리스탈, 듀테륨) → 메탈 환산 가치(3:2:1)."""
    return (
        metal * RESOURCE_VALUE["metal"]
        + crystal * RESOURCE_VALUE["crystal"]
        + deuterium * RESOURCE_VALUE["deuterium"]
    )


def lootable_value(status: dict) -> float:
    """현재 저장 자원의 약탈 가능 가치(메탈 환산). = 약탈 가치 앵커의 기반.

    서버 약탈률(LOOT_FRACTION) × 저장 자원(메탈 환산). status 는 turtle.read_resources 형식
    ({"metal","crystal","deuterium",...}). 값이 없으면 0 으로 본다.
    """
    metal = float(status.get("metal") or 0.0)
    crystal = float(status.get("crystal") or 0.0)
    deut = float(status.get("deuterium") or 0.0)
    return LOOT_FRACTION * _meq(metal, crystal, deut)


def _defense_value(counts: dict[int, int] | None) -> float | None:
    """현재 방어 유닛의 총 건조 가치(메탈 환산). turtle.DEF_COST 단가 재사용.

    counts 가 비어 있으면(조회 실패) None 을 돌려 호출부가 이번 틱을 건너뛰게 한다 —
    조회 실패의 0 을 '방어 전무'로 오인해 헛건조하지 않기 위함(turtle._defense_value 와 동일 규약).
    """
    if not counts:
        return None
    total = 0.0
    for tid, cost in turtle.DEF_COST.items():
        n = counts.get(tid, 0)
        if n:
            total += n * _meq(cost[0], cost[1], cost[2])
    return total


# ---------------------------------------------------------------------------
# 잉여(surplus) 산정 — 성격별 다음 건설 1건을 예약한 뒤 남는 자원
# ---------------------------------------------------------------------------
def _reserve_cost(personality_key: str, levels: dict[int, int]) -> tuple[int, int]:
    """이 성격의 다음 건설 목표 1건의 (메탈, 크리스탈) 비용. 예비비 산정용.

    turtle._next_build_cost 와 같은 로직을 성격별로 일반화한다(그쪽은 'turtle' 고정):
      1) 자원 floor 미달분이 있으면 그 중 가장 비싼 건물.
      2) floor 충족이면 다음 시설(next_facility).
      3) 둘 다 없으면 자원 건물 4종 중 가장 비싼 다음 레벨.
    """
    spec = GROWTH.get(personality_key) or GROWTH["miner"]
    floor = spec["resource_floor"]
    below = [t for t in (1, 2, 3, 4) if levels.get(t, 0) < floor.get(t, 0)]
    if below:
        cands = below
    else:
        nf = next_facility(personality_key, levels)
        cands = [nf] if nf is not None else [1, 2, 3, 4]

    best_m = best_c = 0
    for tid in cands:
        cost = object_cost(tid, levels.get(tid, 0) + 1)
        if cost is None:
            continue
        if cost["metal"] + cost["crystal"] > best_m + best_c:
            best_m, best_c = cost["metal"], cost["crystal"]
    return best_m, best_c


def _surplus(
    status: dict, levels: dict[int, int], personality_key: str
) -> tuple[int, int, int]:
    """경제 예약 후 남은 (메탈, 크리스탈, 듀테륨) 잉여. turtle._surplus_one 재사용."""
    metal = status.get("metal") or 0
    crystal = status.get("crystal") or 0
    deut = status.get("deuterium") or 0
    tgt_m, tgt_c = _reserve_cost(personality_key, levels)

    sm = turtle._surplus_one(metal, int(tgt_m * RESERVE_MULT), status.get("metal_max"))
    sc = turtle._surplus_one(crystal, int(tgt_c * RESERVE_MULT), status.get("crystal_max"))
    sd = turtle._surplus_one(deut, DEUT_RESERVE, status.get("deuterium_max"))
    return sm, sc, sd


# ---------------------------------------------------------------------------
# 최소선 방어 조합 (fodder-first)
# ---------------------------------------------------------------------------
def _plan_base_defense(
    counts: dict[int, int], levels: dict[int, int]
) -> list[tuple[int, int]]:
    """이번 틱 최소선 건조 후보를 우선순위대로. 각 항목 (technologyId, 목표수량).

    모든 성격이 값싼 총알받이뿐 아니라 선행 연구를 마친 뒤 실드돔·가우스·이온 계층까지
    사용할 수 있다. turtle.plan_defense의 비율 설계를 공유하되 투자 목표만 현재 약탈가치에
    맞춘다.
    """
    return turtle.plan_defense(counts, levels)


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def base_defense_tick(
    client, email: str, levels: dict[int, int], personality_key: str, *,
    energy: int | None = None,
) -> str | None:
    """채굴형 최소 방어선 1건. 로그 문자열 또는 None(행동 없음/최소선 충족) 반환.

    levels 는 post_actions 가 이미 조회한 건물/시설/연구/함선 레벨 dict.
    이 함수는 어떤 경우에도 예외를 올리지 않는다(루프 보호).
    """
    try:
        return _base_defense_tick_inner(
            client, email, levels, personality_key, energy=energy
        )
    except Exception as exc:  # noqa: BLE001 - 최소선 버그가 빌드틱/루프를 깨지 않게
        return f"basedef err {type(exc).__name__}: {exc}"


def _base_defense_tick_inner(
    client, email: str, levels: dict[int, int], personality_key: str, *,
    energy: int | None = None,
) -> str | None:
    # 조선소 없으면 어떤 방어도 불가 → 스킵.
    if levels.get(21, 0) < 1:
        return None

    # 약탈 가치 앵커의 기반인 현재 저장 자원. 조회/파싱 실패면 이번 틱은 쉰다(추측 금지).
    try:
        status = turtle.read_resources(client)
    except Exception:  # noqa: BLE001
        return "basedef idle (자원 조회 실패)"
    if status.get("metal") is None:
        return "basedef idle (자원 파싱 실패)"

    # 약탈 가치 = 저장 자원(메탈 환산) × 서버 약탈률. 창고가 비면(약탈 가치 0) 아래 최소선
    # 판정에서 def_value >= 0 이라 자연히 None(지킬 약탈이 없으면 방어도 불필요).
    loot_value = lootable_value(status)

    # 현재 방어 수량·가치. 조회 실패({})면 이번 틱은 쉰다(0 을 '방어 전무'로 오인 방지).
    counts = turtle._read_defense_counts(client)
    def_value = _defense_value(counts)
    if def_value is None:
        return "basedef idle (방어 조회 실패)"

    # 적응: 최근 피격(threat 상태-피크 트리거, 이벤트 파싱 아님)이면 목표 배수를 ADAPT 로 올린다.
    # threat.assess_turtle_threat 이 쿨다운/피크리셋으로 진정 시 baseline 으로 자동 감쇠한다.
    # (에너지 재건이 energy<0 '상태'에 반응하는 것과 같은 상태 기반 트리거 — 사용자 선호.)
    loot_mult = BASE_DEFENSE_LOOT_MULT
    adapted = False
    try:
        state = threat.assess_turtle_threat(email, def_value)
        if state.under_reflex:
            loot_mult = ADAPT_DEFENSE_LOOT_MULT
            adapted = True
    except Exception:  # noqa: BLE001 - 적응 판정 실패는 baseline 으로 진행
        pass

    # 평시 에너지 적자는 태양광을 먼저 복구한다. 그러나 실제 피격 상태에서는 이 게이트를
    # 우회한다. 그렇지 않으면 에너지 적자 식민지가 반복 약탈을 당하면서도 영구히 방어를
    # 한 문도 짓지 못한다(방어시설 자체는 에너지를 소비하지 않는다).
    if energy is not None and energy < 0 and not adapted:
        return "basedef hold (에너지 우선)"

    # 고정 총량 상한 없이 지킬 수 있는 현재 자산과 함께 방어 목표도 성장한다.
    target_value = loot_mult * loot_value

    # 최소선 충족: 이미 목표 이상이면 방어에 자원을 더 묶지 않는다(성장 우선). 정상 상태라
    # 로그 노이즈를 피해 조용히 None 을 돌린다(colony_tick 이 할 일 없을 때 None 과 같은 규약).
    if def_value >= target_value:
        return None

    # 목표까지의 헤드룸이 이번 틱 방어 예산.
    headroom_value = target_value - def_value

    # 경제 예약 후 남은 잉여만 방어로. 잉여 없으면 성장 우선.
    sm, sc, sd = _surplus(status, levels, personality_key)
    if adapted:
        # 성장 행동이 먼저 실행된 뒤 남은 현재 자원이다. 반복 피격 중에는 다음 건설 예비비가
        # 방어를 다시 막지 않도록, 터틀 reflex와 같은 방식으로 잔여 자원을 재건 예산에 연다.
        sm = max(sm, int(status.get("metal") or 0))
        sc = max(sc, int(status.get("crystal") or 0))
        sd = max(sd, max(0, int(status.get("deuterium") or 0) - DEUT_RESERVE))
    if sm <= 0 and sc <= 0:
        return "basedef hold (잉여 없음 — 경제 우선)"

    for tid, want in _plan_base_defense(counts, levels):
        cost = turtle.DEF_COST[tid]
        unit_meq = _meq(cost[0], cost[1], cost[2])
        if unit_meq <= 0:
            continue
        max_by_cap = int(headroom_value // unit_meq)
        amount = min(want, max_by_cap, turtle._affordable(cost, sm, sc, sd))
        if amount < 1:
            continue
        res = turtle.build_defense(client, tid, amount, levels=levels)
        if turtle._is_success(res):
            tag = "ADAPT-DEF" if adapted else "BASE-DEF"
            fodder = sum(counts.get(t, 0) for t in turtle.FODDER_IDS)
            return (
                f"basedef {tag} {tech_label(tid)}({tid})x{amount} "
                f"(def~{int(def_value)}/{int(target_value)}, 총알받이~{fodder})"
            )
        # 큐 참·자원부족 등 → 다음 후보로(자기 교정은 다음 틱 재조회가 담당).

    return "basedef idle (건조 후보 없음/예산 부족)"
