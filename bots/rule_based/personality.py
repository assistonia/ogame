"""규칙봇 성격(Personality) 시스템.

90명의 규칙봇을 서로 다르게 굴리기 위한 성격 파라미터 정의와,
봇 계정(bot001~bot090)에 성격을 결정적(시드 기반)으로 배정하는 함수 모음.

성격은 자원 건물 4종에 대한 "가중치(weights)"로 표현된다.
technologyId: 1=메탈광산, 2=크리스탈광산, 3=듀테륨합성기, 4=태양광발전소

가중치는 두 가지로 쓰인다.
  1) build_cycle(): 가중치 비율대로 고르게 퍼뜨린 건설 사이클을 만든다.
     틱마다 사이클을 한 칸씩 돌면서 "이번 틱의 1순위 건물"을 정한다.
     → 장기적으로 각 건물이 지어지는 비율이 가중치 비율에 수렴한다.
  2) priority_order(): 가중치 내림차순 우선순위. 1순위가 못 지어질 때의
     대체 후보 순서로 쓴다.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from bots.shared.game_objects import SOLAR_PLANT, SOLAR_SATELLITE, object_cost

if TYPE_CHECKING:
    from bots.rule_based.tick_context import TickContext

# technologyId -> 한글 이름
TECH_NAMES: dict[int, str] = {
    1: "메탈광산",
    2: "크리스탈광산",
    3: "듀테륨합성기",
    4: "태양광발전소",
}

# 배정 재현성을 위한 기본 시드. run_universe.py 에서 바꿀 수 있다.
DEFAULT_SEED = "ogame-universe-2026"

# ---------------------------------------------------------------------------
# 성격 정의
# ---------------------------------------------------------------------------
# 각 성격은 4개 자원 건물에 대한 가중치를 가진다.
# 태양광(4)은 어느 성격이든 최소한의 비중을 준다(에너지 흑자 유지).
# 에너지가 음수로 떨어지면 성격과 무관하게 태양광이 강제 1순위가 된다
# (build_candidates 참고) — 여기 가중치는 "평상시" 성향일 뿐이다.
# 개편(docs/strategies.md §7): 자원건물 가중치만 다른 5종 경제형 →
# 실제 플레이스타일 5종(채굴러/레이더/터틀/탐험가/하이브리드)으로 교체.
# weights 는 여전히 자원 건물 4종 가중치이고, 성격별 성장/함대/방어 행동은
# 아래 GROWTH 스펙과 miner_bot 의 성격 분기(raider/turtle 호출)로 표현된다.
PERSONALITIES: dict[str, dict] = {
    "miner": {
        "label": "채굴러",
        "description": "광산 복리로 후반을 지배하는 경제형. 함대·방어는 최소, 남는 자원은 시설·연구·확장에 재투자.",
        "weights": {1: 6, 2: 4, 3: 2, 4: 3},  # 메탈:크리스탈:듀테륨 → 3:2:1 수렴
    },
    "raider": {
        "label": "레이더",
        "description": "소형화물+전투기로 비활성·무방비 유저를 약탈해 성장하는 공격형(구 공격형).",
        "weights": {1: 5, 2: 3, 3: 2, 4: 2},  # 메탈 편향(함선·무기 뼈대)
    },
    "turtle": {
        "label": "터틀",
        "description": "잉여 자원을 방어시설로 바꿔 약탈 가치를 0으로 만드는 수비형. 공격은 하지 않는다.",
        "weights": {1: 4, 2: 3, 3: 2, 4: 3},  # 균형~메탈 편향(방어가 메탈을 많이 먹음)
    },
    "explorer": {
        "label": "탐험가",
        "description": "파스파인더+대형화물+소수 전투함으로 원정 파밍을 노리는 탐험형. 듀테륨(연료) 비중이 높다.",
        "weights": {1: 3, 2: 3, 3: 3, 4: 3},  # 듀테륨↑(원정은 연료를 태움)
    },
    "hybrid": {
        "label": "하이브리드",
        "description": "채굴을 기본으로 소규모 함대와 소량 방어를 병행하는 기회주의형(구 밸런스).",
        "weights": {1: 4, 2: 3, 3: 2, 4: 3},
    },
}

PERSONALITY_KEYS: list[str] = sorted(PERSONALITIES.keys())

# 성격 배정 분포(docs/strategies.md §7): "채굴러 다수 + 소수 공격자"라는 실제 우주
# 생태계를 흉내낸다. 합계 100. _WEIGHTED_ORDER 는 결정적 버킷 경계 순서(재현성).
PERSONALITY_WEIGHTS: dict[str, int] = {
    "miner": 35,
    "raider": 25,
    "turtle": 15,
    "explorer": 15,
    "hybrid": 10,
}
_WEIGHTED_ORDER: list[str] = ["miner", "raider", "turtle", "explorer", "hybrid"]

EXPERIMENTAL_PERSONALITIES = {
    "pirate01@bots.local": "raider",
    "explorer01@bots.local": "explorer",
}
EXPERIMENTAL_PROFILES = {
    "pirate01@bots.local": "raid_aggressive",
    "explorer01@bots.local": "exp_aggressive",
}


def experimental_profile(email: str) -> str | None:
    return EXPERIMENTAL_PROFILES.get(email.lower())


def planning_personality(email: str, base: str) -> str:
    """실험 프로필의 성장 계획 키를 한 곳에서 결정한다."""
    return "explorer_aggressive" if experimental_profile(email) == "exp_aggressive" else base


# ---------------------------------------------------------------------------
# 가중치 → 순서 계산
# ---------------------------------------------------------------------------
def build_cycle(weights: dict[int, int]) -> list[int]:
    """가중치 비율대로 techId를 고르게 퍼뜨린 건설 사이클을 만든다.

    길이 = 가중치 합. Sainte-Laguë(Webster) 방식으로 배분해서
    같은 건물이 몰리지 않고 최대한 균등하게 섞이도록 한다.
    예) {1:2, 2:1, 4:1} -> [1, 2, 1, 4]
    """
    techs = [t for t, w in weights.items() if w > 0]
    if not techs:
        return []
    counts = {t: 0 for t in techs}
    total = sum(weights[t] for t in techs)
    seq: list[int] = []
    for _ in range(total):
        # 아직 가중치 대비 가장 덜 지어진 건물을 고른다.
        # 동점이면 techId가 작은 쪽(메탈->크리스탈->...)을 우선.
        best = max(techs, key=lambda t: (weights[t] / (2 * counts[t] + 1), -t))
        seq.append(best)
        counts[best] += 1
    return seq


def priority_order(weights: dict[int, int]) -> list[int]:
    """가중치 내림차순 우선순위(대체 후보용). 동점이면 techId 오름차순."""
    return sorted(weights.keys(), key=lambda t: (-weights[t], t))


# 성격별 사이클/우선순위를 import 시점에 한 번만 계산해 캐싱한다.
for _p in PERSONALITIES.values():
    _p["cycle"] = build_cycle(_p["weights"])
    _p["order"] = priority_order(_p["weights"])


# ---------------------------------------------------------------------------
# 성격 배정
# ---------------------------------------------------------------------------
def assign_personality(email: str, seed: str = DEFAULT_SEED) -> str:
    """이메일(계정) 기준으로 성격을 결정적으로 배정한다.

    같은 (seed, email)이면 항상 같은 성격이 나온다(시드 기반 재현성).
    시드 문자열만 바꾸면 90명 성격 배치를 통째로 다시 뽑을 수 있다.

    PERSONALITY_WEIGHTS 분포(35/25/15/15/10)를 가중 추첨으로 반영한다:
    [0,100) 난수를 _WEIGHTED_ORDER 누적 경계로 버킷팅한다. n이 커질수록
    실제 인원 비율이 이 가중치에 수렴한다.
    """
    override = EXPERIMENTAL_PERSONALITIES.get(email.lower())
    if override:
        return override
    rng = random.Random(f"{seed}:{email}")
    r = rng.random() * 100.0
    acc = 0
    for key in _WEIGHTED_ORDER:
        acc += PERSONALITY_WEIGHTS[key]
        if r < acc:
            return key
    return _WEIGHTED_ORDER[-1]  # 부동소수 경계 안전망


def personality_of(email: str, seed: str = DEFAULT_SEED) -> dict:
    """이메일에 배정된 성격의 전체 정의(label/description/weights/cycle/order)."""
    return PERSONALITIES[assign_personality(email, seed)]


def build_candidates(
    personality_key: str,
    tick_index: int,
    energy: int | None = None,
) -> list[int]:
    """이번 틱에 시도할 건물 후보를 우선순위 순으로 반환.

    - 평상시: 성격 사이클의 tick_index 위치가 1순위, 나머지는 가중치 순.
    - 에너지가 음수면: 성격과 무관하게 태양광(4)을 강제 1순위로 끌어올린다.
    첫 후보가 자원 부족/큐 참으로 실패하면 다음 후보를 시도하기 위한 목록이다.
    """
    p = PERSONALITIES[personality_key]
    cycle: list[int] = p["cycle"]
    order: list[int] = p["order"]

    primary = cycle[tick_index % len(cycle)] if cycle else order[0]
    candidates = [primary] + [t for t in order if t != primary]

    if energy is not None and energy < 0:
        candidates = [4] + [t for t in candidates if t != 4]
    return candidates


# ---------------------------------------------------------------------------
# 성장 트리 (상태머신) — 시설/연구/함선까지 확장
# ---------------------------------------------------------------------------
# 규칙봇은 초반엔 자원 건물만 짓다가, 자원 건물이 성격별 "floor" 레벨에 도달하면
# 시설(로봇공장→연구소→조선소)을 세우고, 그 다음 성격별로 연구/함선으로 분기한다.
#
# 한 틱의 행동은 최대 2건이다:
#   - primary  : 자원 건물 1 또는 시설 1 (둘은 서버에서 같은 건설 큐 → 틱당 1건)
#   - secondary: 연구 1 또는 함선 1 (연구/조선소는 각각 별도 큐)
#
# 선행조건은 아래 REQUIREMENTS 로 봇이 직접 확인한다. API 가 선행조건 미달인데도
# success 를 반환하는 경우가 있어(silent success), 못 지을 것을 아예 시도하지 않기
# 위함이다. 자원 부족 실패는 서버가 success:false 로 알려주므로 다음 후보로 넘어간다.

# technologyId -> 선행조건 [(요구 tech_id, 요구 레벨), ...]
# 출처: ogamex/app/GameObjects/{Station,Research,CivilShip,MilitaryShip}Objects.php
REQUIREMENTS: dict[int, list[tuple[int, int]]] = {
    # 저장고 (BuildingObjects) — 선행조건 없음. 자원 창고 용량을 올려 광산 성장을 계속하게 한다.
    22: [],                                    # metal_store
    23: [],                                    # crystal_store
    24: [],                                    # deuterium_store
    # 시설 (StationObjects)
    14: [],                                    # robot_factory
    31: [],                                    # research_lab
    21: [(14, 2)],                             # shipyard ← robot_factory 2
    15: [(14, 10), (108, 10)],                 # nano_factory
    # 연구 (ResearchObjects) — 모두 research_lab 필요
    113: [(31, 1)],                            # energy_technology
    115: [(31, 1), (113, 1)],                  # combustion_drive
    117: [(31, 2), (113, 1)],                  # impulse_drive
    108: [(31, 1)],                            # computer_technology
    106: [(31, 3)],                            # espionage_technology
    109: [(31, 4)],                            # weapon_technology
    111: [(31, 2)],                            # armor_technology
    110: [(31, 6), (113, 3)],                  # shielding_technology
    120: [(31, 1), (113, 2)],                  # laser_technology
    121: [(31, 4), (113, 4), (120, 5)],        # ion_technology
    114: [(31, 7), (113, 5), (110, 5)],        # hyperspace_technology
    118: [(31, 7), (114, 3)],                  # hyperspace_drive
    122: [(31, 4), (113, 8), (120, 10), (121, 5)],  # plasma_technology
    124: [(31, 3), (117, 3), (106, 4)],        # astrophysics
    # 함선 (Civil/MilitaryShipObjects) — 모두 shipyard 필요
    202: [(21, 2), (115, 2)],                  # small_cargo
    203: [(21, 4), (115, 6)],                  # large_cargo
    204: [(21, 1), (115, 1)],                  # light_fighter
    205: [(21, 3), (111, 2), (117, 2)],        # heavy_fighter
    206: [(21, 5), (117, 4), (121, 2)],        # cruiser
    207: [(21, 7), (118, 4)],                  # battle_ship
    215: [(21, 8), (118, 5), (114, 5), (120, 12)],  # battlecruiser
    209: [(21, 4), (115, 6), (110, 2)],        # recycler
    210: [(21, 3), (115, 3), (106, 2)],        # espionage_probe
    212: [(21, 1)],                            # solar_satellite
    208: [(21, 4), (117, 3)],                  # colony_ship
    219: [(21, 5), (115, 6), (110, 4), (114, 2)],  # pathfinder (탐험가 원정 주력)
    # 방어시설 (DefenseObjects) — 모두 shipyard 필요. 터틀이 turtle.py 로 건조.
    401: [(21, 1)],                            # rocket_launcher (총알받이·메탈만)
    402: [(21, 2), (120, 3)],                  # light_laser   (총알받이·순항함에 연사 안 당함)
    403: [(21, 4), (120, 6), (113, 3)],        # heavy_laser   (중간 주포)
    404: [(21, 6), (109, 3), (110, 1), (113, 6)],  # gauss_cannon (주포)
    405: [(21, 4), (121, 4)],                  # ion_cannon    (주포)
    406: [(21, 8), (122, 7)],                  # plasma_turret (상위 주포)
    407: [(21, 1), (110, 2)],                  # small_shield_dome
    408: [(21, 6), (110, 6)],                  # large_shield_dome
}

# 성격별 성장 스펙.
#   resource_floor  : 자원 건물(1~4) 최소 레벨. 여기 못 미치면 자원 건물만 짓는다.
#   facility_targets: (시설 id, 목표 레벨) 순서대로 추진. 앞에서부터 미달+선행조건
#                     충족이면 그걸 짓는다.
#   research_targets: (연구 id, 목표 레벨) 순서대로. secondary(연구) 후보.
#   ship_targets    : (함선 id, 목표 수량, 틱당 건조 배치) 순서대로. secondary(함선) 후보.
GROWTH: dict[str, dict] = {
    "miner": {  # 채굴러: 경제·수송 중심. 함대/방어 없음(남는 자원은 시설·연구·확장).
        "resource_floor": {1: 6, 2: 4, 3: 2, 4: 5},
        # 조선소 목표 Lv.4: 식민선(208, 조선소4 요구)을 지어 "채굴러 우선 식민" 커리큘럼을
        # 실제로 가동한다(CURRICULUM_AUDIT §B — 종전 Lv.2 상한이 식민을 원천 차단).
        "facility_targets": [(14, 2), (31, 3), (21, 2), (21, 4), (31, 5), (14, 4)],
        "research_targets": [(113, 3), (115, 2), (108, 3), (106, 2)],
        "ship_targets": [(202, 8, 3), (212, 3, 2)],  # 소형화물선 + 태양광위성(경제 함선)
    },
    "raider": {  # 레이더(구 공격형): 조선소·무기연구·전투기 + 정찰. raid_tick 이 약탈 수행.
        "resource_floor": {1: 4, 2: 4, 3: 3, 4: 5},
        # 연구소6은 shielding_technology(110)의 서버 선행조건이다. 리사이클러는
        # 조선소4+연소6+실드2를 요구하므로 연구소3에서 멈추면 데브리 수거가 영구 봉쇄된다.
        # 연구소7+조선소8은 하이퍼스페이스 엔진과 전함/순양전함까지의 상위 함종 경로를
        # 닫는 최종 단계다. 조선소5에서 순양함은 먼저 열리므로 초기 전력 성장은 기다리지 않는다.
        "facility_targets": [(14, 2), (31, 3), (21, 3), (21, 5), (31, 6),
                             (14, 4), (31, 7), (21, 7), (21, 8)],
        # 컴퓨터기술(108) 추가 → 함대 슬롯 확장. 정찰기술(106)로 정찰기 언락(먼저 정찰).
        # 연소(115) Lv.6: 리사이클러(209, 연소6 요구) 언락 — 데브리 수거 루틴(raider.py)의
        # 선행조건. Lv.2 상한이던 시절 라이더 24명 전원이 수거 불가였다(2026-07-12 실측).
        # 연구는 같은 id의 단계별 마일스톤을 허용한다(plan_secondary가 첫 미달
        # 마일스톤만 후보로 낸다). 이로써 레이저의 기존 정찰/리사이클러 경로를
        # 먼저 유지하고, 순양함 최소 선행조건(에너지4·레이저5·이온2·임펄스4)을
        # 상위 전투연구와 전함/순양전함 계열보다 앞에 닫아 게임시간 리드타임을 줄인다.
        # 무기/실드/장갑6은 각 6, 임펄스6·이온5까지 마무리해 '함종만 열고
        # 전투연구 2~4'에 머무는 프론트러너 편차도 없앤다.
        "research_targets": [
            # 1) 기존 정찰/리사이클러+초기 전투 기반.
            (113, 3), (115, 2), (106, 4), (108, 3), (109, 4),
            (111, 2), (117, 2), (110, 3), (115, 6),
            # 2) 순양함 즉시 언락.
            (113, 4), (120, 5), (121, 2), (117, 4),
            # 3) 공통 전투 기술+전함 언락.
            (109, 6), (111, 6), (110, 5), (113, 5), (114, 3), (118, 4),
            # 4) 순양전함 언락 후 최종 군사 커리큘럼.
            (114, 5), (118, 5), (120, 12), (117, 6), (121, 5), (110, 6),
        ],
        # 소형/대형화물(약탈 적재) + 전투기(초기 타격) + 정찰기(정찰) +
        # 순양함/전함/순양전함(사이징이 실제로 선택할 상위 전투 후보).
        # 서버 첩보 공개식상 연구까지 보려면 동급 기술 7기, 기술차 +2면 11기가 필요하다.
        # 임무 수를 늘리지 않고 한 임무의 정보 깊이를 확보하도록 12기까지 비축한다.
        "ship_targets": [(202, 8, 3), (204, 12, 3), (210, 12, 4), (205, 5, 2),
                         (203, 12, 3), (206, 12, 2), (207, 6, 2), (215, 4, 1)],
    },
    "turtle": {  # 터틀: 방어 계층을 짓기 위한 조선소/연구소 심화 + 방어 연구.
        "resource_floor": {1: 5, 2: 4, 3: 2, 4: 5},
        # 조선소(21)를 로봇공장 다음으로 먼저 1레벨 올려 로켓런처(조선소1) 방어를 조기 가동한다.
        # 이후 연구소·조선소를 심화: 가우스/대형실드돔(조선소6)·shielding(연구소6)까지.
        "facility_targets": [(14, 2), (21, 1), (31, 3), (21, 3), (31, 6), (21, 6), (14, 4)],
        # 레이저3(경레이저)→shielding2(소형돔)→무기3+에너지6(가우스)→레이저5(이온)→shielding6(대형돔).
        "research_targets": [(113, 3), (120, 3), (110, 2), (109, 3),
                             (113, 6), (120, 5), (110, 6), (121, 4)],
        "ship_targets": [],  # 순수 수비: 함선 안 뽑음. 방어는 turtle.defense_tick 이 건조.
    },
    "explorer": {  # 탐험가: 원정 함대(파스파인더+대형화물+소수 전투함) 준비.
        "resource_floor": {1: 4, 2: 4, 3: 3, 4: 5},
        "facility_targets": [(14, 2), (31, 3), (21, 4), (31, 7), (21, 5), (14, 4)],
        # 연소6(대형화물)→shielding4·하이퍼2(파스파인더)→컴퓨터(함대 슬롯).
        "research_targets": [(113, 3), (115, 6), (110, 4), (113, 5),
                             (110, 5), (114, 2), (108, 3)],
        # 대형화물(운반 주력) + 전투기(함대가치) + 파스파인더(원정 보너스) + 소형화물 약간.
        "ship_targets": [(203, 10, 3), (204, 20, 3), (219, 5, 2), (202, 3, 2)],
    },
    "hybrid": {  # 하이브리드(구 밸런스): 채굴 기본 + 소규모 함대 + 소량 방어.
        "resource_floor": {1: 5, 2: 5, 3: 2, 4: 5},
        # 조선소 목표 Lv.4: 식민선(208, 조선소4 요구) 확보 — miner 와 동일 근거(CURRICULUM_AUDIT §B).
        "facility_targets": [(14, 2), (31, 3), (21, 2), (21, 4), (14, 4), (31, 5)],
        "research_targets": [(113, 3), (115, 3), (108, 2), (109, 2), (106, 2)],
        "ship_targets": [(202, 5, 2), (204, 5, 2), (210, 2, 2)],  # 화물 + 전투기 + 정찰기
    },
}

# Named experiment only: closes astrophysics and scales cargo/pathfinder production.
PERSONALITIES["explorer_aggressive"] = dict(PERSONALITIES["explorer"])
GROWTH["explorer_aggressive"] = {
    **GROWTH["explorer"],
    "research_targets": [(113, 3), (115, 6), (106, 4), (117, 3), (124, 4),
                         (110, 4), (113, 5), (114, 2), (108, 6)],
    "ship_targets": [(203, 60, 5), (219, 8, 2), (206, 4, 1), (202, 20, 4)],
}


# 성격별 플릿세이브 가치 임계(fleetsave.fleetsave_tick 의 min_value). 파킹 함대의 총 건조가치가
# 이 값 이상일 때만 저위험 왕복 수송으로 대피한다. 함대가 정체성인 성격은 낮게(상시 세이브),
# 함선이 거의 없는 성격은 높게(사실상 off) 잡는다. 근거(docs 전투경제): raider 는 함대가
# 곧 자산이라 소규모라도 지키고, explorer 는 원정으로 이미 대피 중이라 잔류분만, miner/turtle 은
# 화물 소수뿐이라 의미 있는 규모일 때만.
FLEETSAVE_MIN_VALUE: dict[str, int] = {
    "raider": 3000,     # 전투기 1기(4000)면 이미 세이브 — 함대가 정체성
    "explorer": 15000,  # 원정 잔류분(대형화물·전투기 소수)이 쌓였을 때
    "hybrid": 20000,    # 소규모 함대 — 어느 정도 모이면 세이브
    "miner": 40000,     # 화물 소수 — 큰 규모일 때만(대개 off)
    "turtle": 40000,    # 함선 거의 없음 — 사실상 off(방어는 파킹 고정이라 세이브 불가)
}


# 저장고 3종(metal_store=22 / crystal_store=23 / deuterium_store=24)은 성격과 무관하게
# 공통으로 다룬다: GROWTH 의 성격별 자원건물/시설/연구/함선 트리와 달리, 저장고는 "자원이
# 창고 용량에 막히는가"라는 상태로만 판정한다(아래 plan_primary 의 저장고 규칙 + storage.py).
# 그래서 성격별 GROWTH 스펙이 아니라 이 공용 상수로 등록한다.
STORAGE_BUILDINGS: tuple[int, int, int] = (22, 23, 24)

# 투자 계산기는 태양광(4)을 생산 투자 후보로 다루지 않는다. 에너지/저장고/시설
# 상태머신이 정한 비광산 후보의 자리는 그대로 둔 채, 아래 세 광산이 이미 차지한
# 자리끼리만 ROI 순서로 바꾼다.
MINE_BUILDINGS: tuple[int, int, int] = (1, 2, 3)


def tech_label(tech_id: int) -> str:
    """로그용 이름. 자원 건물은 한글(TECH_NAMES), 그 외는 machine_name."""
    if tech_id in TECH_NAMES:
        return TECH_NAMES[tech_id]
    try:
        from bots.shared.game_objects import name_of  # 지연 import (import 순환 방지)

        return name_of(tech_id)
    except Exception:  # noqa: BLE001 - 이름 조회 실패는 로그 품질 문제일 뿐
        return f"tech{tech_id}"


def prereqs_met(tech_id: int, levels: dict[int, int]) -> bool:
    """levels(현재 레벨/수량 dict) 기준으로 tech_id 의 선행조건이 모두 충족됐는지."""
    for req_id, req_lv in REQUIREMENTS.get(tech_id, []):
        if levels.get(req_id, 0) < req_lv:
            return False
    return True


def next_facility(
    personality_key: str,
    levels: dict[int, int],
    production_candidate: int | None = None,
) -> int | None:
    """부트스트랩 시설 뒤에도 광산 규모에 맞춰 연구소·조선소·로봇공장을 계속 성장."""
    for tid, target in GROWTH[personality_key]["facility_targets"]:
        if levels.get(tid, 0) < target and prereqs_met(tid, levels):
            return tid
    if production_candidate is not None:
        return production_candidate
    mine_average = sum(max(0, int(levels.get(tid, 0))) for tid in MINE_BUILDINGS) // 3
    dynamic_targets = {
        14: max(4, 1 + mine_average // 3),
        31: max(7, mine_average // 2),
        21: max(8, mine_average // 2),
    }
    pending = [
        (target - int(levels.get(tid, 0)), tid)
        for tid, target in dynamic_targets.items()
        if int(levels.get(tid, 0)) < target and prereqs_met(tid, levels)
    ]
    if pending:
        # 목표 대비 가장 뒤처진 시설부터. 동률은 로봇→연구소→조선소 순서(id 오름차순).
        return max(pending, key=lambda item: (item[0], -item[1]))[1]
    return None


_NANITE_SHIP_UTILIZATION: dict[str, float] = {
    "miner": 0.12,
    "raider": 0.45,
    "turtle": 0.08,
    "explorer": 0.30,
    "explorer_aggressive": 0.40,
    "hybrid": 0.24,
}
_NANITE_DEFENSE_UTILIZATION: dict[str, float] = {
    "miner": 0.02,
    "raider": 0.04,
    "turtle": 0.35,
    "explorer": 0.02,
    "explorer_aggressive": 0.03,
    "hybrid": 0.08,
}
NANITE_BACKPRESSURE_SECONDS = 6 * 3600


def production_facility_recommendation(
    personality_key: str,
    levels: dict[int, int],
    resource_state: dict | None,
    email: str | None,
):
    """Shared AI/rule-bot Nanite recommendation with live queue state."""
    if not email:
        return None, None
    from bots.rule_based import investment, production_capacity

    queue = production_capacity.production_queue_snapshot(email)
    recommendation = investment.nanite_investment_recommendation(
        levels,
        universe_speed=queue.economy_speed,
        queues_known=queue.available,
        building_queue_depth=queue.building_queue_depth,
        building_queue_seconds=queue.building_queue_seconds,
        unit_queue_depth=queue.unit_queue_depth,
        unit_queue_seconds=queue.unit_queue_seconds,
        resource_state=resource_state,
        ship_utilization=_NANITE_SHIP_UTILIZATION.get(personality_key, 0.20),
        defense_utilization=_NANITE_DEFENSE_UTILIZATION.get(personality_key, 0.05),
    )
    return queue, recommendation


def production_facility_candidate(
    personality_key: str,
    levels: dict[int, int],
    resource_state: dict | None,
    email: str | None,
) -> tuple[str, int] | None:
    """Next queue-safe Robotics/Nanite facility action, or no candidate."""
    queue, recommendation = production_facility_recommendation(
        personality_key, levels, resource_state, email
    )
    if (
        queue is None
        or recommendation is None
        or not (
            recommendation.recommended
            or queue.unit_queue_seconds >= NANITE_BACKPRESSURE_SECONDS
        )
        or queue.building_queue_depth > 0
    ):
        return None
    if levels.get(14, 0) < 10:
        return ("facility", 14)
    if levels.get(108, 0) < 10:
        return None
    if recommendation.ready_now:
        return ("facility", 15)
    # In particular, a live ship/defense queue returns no Nanite candidate.
    return None


# 유휴 폴백을 적용할 성격(잉여를 함대/방어로 소진하지 않아 방치가 잦은 경제형).
# raider/turtle 은 잉여를 함선/방어로 소진하므로 대상이 아니다(그쪽 파일도 건드리지 않는다).
IDLE_FALLBACK_PERSONALITIES: frozenset[str] = frozenset(
    {"miner", "explorer", "explorer_aggressive", "hybrid"}
)

# 저장고 building id -> 그 창고가 담는 자원명(잉여 큰 자원의 창고를 먼저 짓기 위함).
_STORE_RESOURCE: dict[int, str] = {22: "metal", 23: "crystal", 24: "deuterium"}


def idle_fallback_candidates(
    personality_key: str,
    levels: dict[int, int],
    resource_state: dict | None,
    active_targets: set[int] | None = None,
) -> list[tuple]:
    """정상 primary 후보가 모두 자원부족으로 스킵돼 봇이 유휴(잉여 방치)일 때, 방치된
    잉여 자원을 유용하게 소진할 저비용 대체 건설 후보를 STRICT 우선순위로 반환한다.

    우선순위(사용자 지정, 고정):
      1) 더 싼 광산 레벨 — 감당 가능한 광산 중 다음 레벨 비용 오름차순(가장 싼 광산부터).
      2) 저장고(metal/crystal/deut store) — 잉여를 창고 용량으로 전환. 가장 많이 쌓인
         자원의 창고부터(잉여를 실제로 소진하는 순서).
      3) 시설(로봇/연구소/조선소) — facility_targets 범위 내 다음 시설(선행조건 충족).
      4) 최후수단: 에너지 인프라(태양광 발전소). 위성 스팸은 하지 않는다.

    이 함수는 '순서'만 만든다. 각 후보의 실제 감당가능 여부(비용 vs 보유/용량)와 백오프는
    호출부(_queue_actions)의 기존 사전체크가 그대로 거른다 — 여기서 다시 판정하지 않는다.
    이미 건설 큐에 있는 대상(active_targets)은 중복을 피해 제외한다. 유휴 폴백 대상이 아닌
    성격(raider/turtle 등)이면 빈 목록을 돌려 완전한 no-op 이 된다.
    """
    if personality_key not in IDLE_FALLBACK_PERSONALITIES:
        return []
    active = active_targets or set()
    out: list[tuple] = []

    # 1) 광산: 다음 레벨 비용 오름차순(가장 싼 광산부터). 감당 여부는 호출부가 거른다.
    mine_costs: list[tuple[int, int]] = []
    for tid in MINE_BUILDINGS:
        cost = object_cost(tid, levels.get(tid, 0) + 1)
        if cost is not None:
            mine_costs.append((sum(cost.values()), tid))
    for _total, tid in sorted(mine_costs):
        if tid not in active:
            out.append(("build", tid))

    # 2) 저장고: 가장 많이 쌓인 자원의 창고부터(잉여 소진). 자원상태 없으면 고정 순서.
    store_order = list(STORAGE_BUILDINGS)  # (22, 23, 24) = metal/crystal/deut
    if resource_state:
        store_order.sort(
            key=lambda sid: -int(resource_state.get(_STORE_RESOURCE[sid], 0) or 0)
        )
    for sid in store_order:
        if sid not in active:
            out.append(("build", sid))

    # 3) 시설: facility_targets 범위 내 다음 시설(선행조건 충족).
    nf = next_facility(personality_key, levels)
    if nf is not None and nf not in active:
        out.append(("facility", nf))

    # 4) 최후수단: 태양광 발전소(에너지 인프라). 위성 스팸 대신 잉여를 건물로 소진.
    if SOLAR_PLANT not in active:
        out.append(("build", SOLAR_PLANT))

    return out


def _order_mines_by_roi(
    candidates: list[tuple],
    levels: dict[int, int],
    energy: int | None,
    resource_state: dict | None,
) -> list[tuple]:
    """광산 후보가 있던 슬롯만 투자회수율 순서로 안정 재정렬한다.

    ROI 계산에는 행성 온도와 현재 에너지 여유가 필요하다. 둘 중 하나라도 없거나
    계산 입력이 깨졌으면 기존 성격 사이클 순서를 그대로 반환한다. 에너지로 감당할 수
    없는 광산은 ``mine_roi_order``가 추천 목록에서 빼며, 여기서는 후보 자체를 삭제하지
    않고 추천 가능한 광산 뒤에 원래 순서로 남긴다.

    태양광/저장고/시설은 이 함수가 이동시키지 않는다. 따라서 plan_primary가 뒤에서
    덧붙이는 에너지·저장고 안전 접두부와 시설 진행 순서는 ROI가 침범할 수 없다.
    """
    if energy is None or not resource_state or "temp_max" not in resource_state:
        return candidates

    mine_actions = [
        action
        for action in candidates
        if action[0] == "build" and action[1] in MINE_BUILDINGS
    ]
    if len(mine_actions) < 2:
        return candidates

    try:
        from bots.rule_based import investment  # 지연 import: 계획 모듈의 시작 비용 최소화

        roi = investment.mine_roi_order(
            levels,
            energy_ok=energy >= 0,
            temp_max=resource_state["temp_max"],
            energy_surplus=max(energy, 0),
        )
    except (TypeError, ValueError):
        # DB 폴백/깨진 상태값 하나 때문에 전체 봇 틱을 중단하지 않는다.
        return candidates

    if not roi:
        return candidates

    rank = {technology_id: index for index, (technology_id, _score) in enumerate(roi)}
    indexed_mines = list(enumerate(mine_actions))
    ordered_mines = [
        action
        for _original_index, action in sorted(
            indexed_mines,
            key=lambda item: (rank.get(item[1][1], len(rank)), item[0]),
        )
    ]
    mine_iter = iter(ordered_mines)
    return [
        next(mine_iter)
        if action[0] == "build" and action[1] in MINE_BUILDINGS
        else action
        for action in candidates
    ]


def _plan_primary_base(
    personality_key: str,
    levels: dict[int, int],
    tick_index: int,
    energy: int | None = None,
    resource_state: dict | None = None,
    email: str | None = None,
) -> list[tuple]:
    """저장고/에너지 판정을 뺀 '기존 계획'(자원 floor → 시설 단계) 후보 목록.

    각 후보는 ("build", tid) 또는 ("facility", tid). 저장고 규칙/에너지 비상은
    plan_primary 가 이 결과 앞에 얹는다(우선순위: 에너지비상 > 저장고 > 이 계획).
    """
    rc_tids = build_candidates(personality_key, tick_index, None)
    rc = [("build", t) for t in rc_tids]

    # 자원 floor 미달: 미달분을 앞으로, 나머지 자원 건물이 뒤따른다
    floor = GROWTH[personality_key]["resource_floor"]
    below = [t for t in (1, 2, 3, 4) if levels.get(t, 0) < floor.get(t, 0)]
    if below:
        front = [("build", t) for t in rc_tids if t in below]
        rest = [a for a in rc if a not in front]
        # 최소 레벨 미달군 자체가 성장 안전장치다. ROI가 이미 floor를 채운 광산을
        # 미달 광산보다 앞으로 넘기지 못하게 두 우선순위 군 안에서만 각각 정렬한다.
        front = _order_mines_by_roi(front, levels, energy, resource_state)
        rest = _order_mines_by_roi(rest, levels, energy, resource_state)
        return front + rest

    # 시설 단계
    dynamic_facility = production_facility_candidate(
        personality_key, levels, resource_state, email
    )
    nf = next_facility(
        personality_key,
        levels,
        None if dynamic_facility is None else dynamic_facility[1],
    )
    if nf is None:
        base = rc  # 더 지을 시설 없음 → 자원 건물 계속
        return _order_mines_by_roi(base, levels, energy, resource_state)
    fa = ("facility", nf)
    if tick_index % 3 == 0:
        # 경제 틱: 자원 건물 1순위, 시설은 자원이 남으면 (여기선 큐가 하나라
        # 실제로는 자원 건물 1건만 지어짐) — 경제가 시설에 완전히 밀리지 않게 한다.
        base = [rc[0], fa] + rc[1:]
    else:
        base = [fa] + rc  # 시설 틱: 시설 우선, 자원 부족하면 자원 건물로 폴백
    return _order_mines_by_roi(base, levels, energy, resource_state)


def plan_primary(
    personality_key: str | None = None,
    levels: dict[int, int] | None = None,
    tick_index: int | None = None,
    energy: int | None = None,
    resource_state: dict | None = None,
    state_key: str | None = None,
    active_targets: set[int] | None = None,
    allow_satellite: bool = False,
    *,
    ctx: "TickContext | None" = None,
) -> list[tuple]:
    """이번 틱의 primary(자원건물/시설) 행동 후보를 우선순위대로 반환.

    각 후보는 ("build", tid) 또는 ("facility", tid). 앞에서부터 시도해 큐에 쌓인다.

    우선순위(위에서부터 강함):
      1) 에너지 개입(energy.plan_energy): 현재 적자이거나 이번 틱 예정 광산 업글로 적자
         전환되면(선제) 에너지 생산을 앞으로 끌어온다.
           - 태양광(건물)이 선택되면 태양광(4)을 저장고보다도 앞에 둔다. 단 태양광 비용이
             창고 용량을 넘으면 그 저장고를 태양광보다 먼저(무한 취소 방지).
           - 위성이 선택되면(교차점 통과 + allow_satellite) primary 는 손대지 않는다 —
             위성은 조선소 큐라 plan_secondary 가 병행 건조한다.
      2) 저장고: 자원이 창고 용량에 막히거나(반응) 다음 목표 비용이 용량을 넘을(선제) 때
         해당 저장고를 앞으로 (storage.storage_ids_needed). resource_state 가 있을 때만.
      3) 기존 계획: 자원 floor → 시설 단계(_plan_primary_base).

    ## ctx (TickContext) 경로 — 권장
    ctx 를 주면 그 필드(personality_key/levels/tick_index/energy/resource_state/state_key/
    active_targets/allow_satellite)를 개별 인자보다 우선해 쓴다. 규칙봇(miner_bot)과
    AI(policy_engine) 두 실가동 경로가 build_tick_context 로 만든 같은 ctx 를 넘겨,
    한쪽만 파라미터를 누락하는 회귀를 구조적으로 막는다. ctx 를 안 주면 기존 개별 kwargs
    시그니처로 동작한다(하위호환 — 테스트/외부 호출부용).

    resource_state 를 주지 않으면 저장고 규칙은 비활성 — 기존 동작과 100% 동일하다.
    state_key(보통 이메일)는 반응 규칙의 2틱 스트릭 상태 파일 키다.
    allow_satellite=False(기본) 면 에너지 개입은 항상 태양광(건물)으로만 처리한다.
    """
    if ctx is not None:
        personality_key = ctx.personality_key
        levels = ctx.levels
        tick_index = ctx.tick_index
        energy = ctx.energy
        resource_state = ctx.resource_state
        state_key = ctx.state_key
        active_targets = ctx.active_targets
        allow_satellite = ctx.allow_satellite
        email = ctx.email
    else:
        email = None
    base = _plan_primary_base(
        personality_key, levels, tick_index, energy, resource_state, email
    )

    # 2) 저장고 판정 (resource_state 있을 때만). lead_targets = 이번 틱 실제 시도 상위 후보.
    from bots.rule_based import storage  # 지연 import (import 순환 방지)
    from bots.rule_based import energy as energy_rule

    store_ids = storage.storage_ids_needed(
        levels, resource_state, base[:3], state_key, tick_index, active_targets
    )
    stores = [("build", sid) for sid in store_ids]

    # 1) 에너지 개입. 태양광이면 여기서 primary 에 끼우고, 위성이면 primary 무개입.
    eplan = energy_rule.plan_energy(
        levels, energy, resource_state, base, allow_satellite=allow_satellite
    )
    if eplan.solar_action is not None:
        solar = eplan.solar_action  # ("build", 4)
        # 태양광 지불용 저장고(단일자원 비용>용량×0.9)를 태양광보다 앞에.
        solar_store_ids = storage.stores_for_affording(
            levels, resource_state, solar, active_targets
        )
        ss = [("build", sid) for sid in solar_store_ids]
        head = ss + [solar] + [a for a in stores if a != solar and a not in ss]
        return head + [a for a in base if a not in head]

    if stores:
        return stores + [a for a in base if a not in stores]
    return base


def plan_secondary(
    personality_key: str | None = None,
    levels: dict[int, int] | None = None,
    tick_index: int | None = None,
    energy: int | None = None,
    resource_state: dict | None = None,
    account_ship_totals: dict[int, int] | None = None,
    require_account_ship_totals: bool = False,
    *,
    ctx: "TickContext | None" = None,
) -> list[tuple]:
    """이번 틱의 secondary(연구/함선) 행동 후보를 우선순위대로 반환.

    각 후보는 ("research", tid) 또는 ("ship", tid, amount).
    연구는 연구소(31)≥1, 함선은 조선소(21)≥1 이 있어야 후보에 오른다.
    틱 짝/홀로 연구·함선 우선순위를 번갈아 → 장기적으로 둘 다 진행된다.

    energy/resource_state 를 주면(규칙봇/AI 두 경로 모두) 에너지 위성 경로를 활성화한다:
    에너지 개입이 필요하고 교차점이 위성을 가리키면(energy.plan_energy) 태양광 위성(212)
    건조를 함선 후보 맨 앞에 끼운다 — plan_primary(allow_satellite=True)가 태양광을 건너뛴
    것과 짝을 이뤄, 적자를 위성으로 메운다. 둘 다 안 주면 기존 동작(위성 주입 없음).

    ctx(TickContext)를 주면 그 필드(personality_key/levels/tick_index/energy/resource_state)를
    개별 인자보다 우선해 쓴다(규칙봇/AI 공용 경로 — plan_primary 와 동일 규약).

    성격별 ``ship_targets``의 목표 수량은 ``account_ship_totals``가 있으면 현재 행성이
    아니라 모든 행성+비행+미완료 unit queue의 계정 합계를 기준으로 한다. 실가동 경로는
    ``require_account_ship_totals=True``라서 합계 조회 실패 시 공용 함선 배치를 보수적으로
    멈춘다(연구와 에너지 비상 위성은 계속 계획). 직접 호출하는 옛 코드/테스트는 기본값
    False로 두어 합계가 없으면 종전처럼 ``levels``를 사용한다.
    """
    if ctx is not None:
        personality_key = ctx.personality_key
        levels = ctx.levels
        tick_index = ctx.tick_index
        energy = ctx.energy
        resource_state = ctx.resource_state
        account_ship_totals = ctx.account_ship_totals
        require_account_ship_totals = ctx.require_account_ship_totals
        email = ctx.email
        ship_strategy = ctx.ship_strategy
    else:
        email = None
        ship_strategy = None
    g = GROWTH[personality_key]

    facilities: list[tuple] = []
    queue, nanite_recommendation = production_facility_recommendation(
        personality_key, levels, resource_state, email
    )
    pursue_nanite = (
        queue is not None
        and nanite_recommendation is not None
        and (
            nanite_recommendation.recommended
            or queue.unit_queue_seconds >= NANITE_BACKPRESSURE_SECONDS
        )
    )
    if pursue_nanite:
        # Computer Technology is a research-lane prerequisite and can close in
        # parallel with Robotics.  The facility action is also exposed here for
        # secondary-plan consumers; current executors run it through primary.
        if levels.get(108, 0) < 10 and levels.get(31, 0) >= 1:
            research_prerequisite = ("research", 108)
        else:
            research_prerequisite = None
        facility = production_facility_candidate(
            personality_key, levels, resource_state, email
        )
        if facility is not None:
            facilities.append(facility)
    else:
        research_prerequisite = None

    research: list[tuple] = []
    if levels.get(31, 0) >= 1:
        # 같은 연구 id를 여러 목표치로 나누어 중간 함종을 먼저 열 수 있다.
        # 현재 레벨에서 각 id의 **첫 미달 마일스톤**만 후보로 내어, 같은
        # 연구를 한 틱에 중복 시도하거나 뒤의 최종 목표가 앞 단계를 넘지 못하게 한다.
        pending_milestones: set[int] = set()
        for tid, target in g["research_targets"]:
            if tid in pending_milestones or levels.get(tid, 0) >= target:
                continue
            pending_milestones.add(tid)
            if prereqs_met(tid, levels):
                research.append(("research", tid))
    if research_prerequisite is not None:
        research = [research_prerequisite] + [
            action for action in research if action[1] != 108
        ]

    ships: list[tuple] = []
    ship_inventory = account_ship_totals if account_ship_totals is not None else levels
    ship_inventory_known = (
        account_ship_totals is not None or not require_account_ship_totals
    )
    if levels.get(21, 0) >= 1 and ship_inventory_known:
        for tid, target, batch in g["ship_targets"]:
            have = ship_inventory.get(tid, 0)
            if have < target and prereqs_met(tid, levels):
                ships.append(("ship", tid, min(target - have, batch)))

    # 고정 ship_targets는 초반 해금용 부트스트랩일 뿐 종착점이 아니다. 실가동 컨텍스트는
    # 현재 유동자원·계정 전체 함대·개인 전략을 사용해 그 뒤의 동적 투자 후보를 계속 만든다.
    # 직접 plan_secondary를 호출하는 구 테스트/외부 코드는 email이 없어 종전 결과를 유지한다.
    if email and ship_inventory_known:
        from bots.rule_based import strategic_growth

        dynamic_ships = strategic_growth.strategic_ship_candidates(
            personality_key,
            levels,
            resource_state,
            account_ship_totals,
            email=email,
            strategy=ship_strategy,
        )
        existing_ship_ids = {action[1] for action in ships}
        ships.extend(
            action for action in dynamic_ships if action[1] not in existing_ship_ids
        )

    # 유한 연구 마일스톤을 모두 닫은 뒤에도 연구가 멈추지 않는다. 성격별 우선순위 안에서
    # 현재 레벨이 가장 낮고 선행조건을 만족한 연구를 계속 발전시킨다.
    if email and levels.get(31, 0) >= 1:
        from bots.rule_based import strategic_growth

        existing_research_ids = {action[1] for action in research}
        research.extend(
            action
            for action in strategic_growth.strategic_research_candidates(
                personality_key, levels
            )
            if action[1] not in existing_research_ids
        )

    # 에너지 위성 주입(energy/resource_state 가 있을 때만). plan_primary 와 동일 입력으로
    # plan_energy 를 계산해 결정을 일치시킨다(양쪽이 같은 교차점 판정을 공유).
    if energy is not None:
        from bots.rule_based import energy as energy_rule

        base = _plan_primary_base(
            personality_key, levels, tick_index, energy, resource_state
        )
        eplan = energy_rule.plan_energy(
            levels, energy, resource_state, base, allow_satellite=True
        )
        pending_satellites = (
            sum(
                item.remaining_amount for item in queue.unit_items
                if item.object_id == SOLAR_SATELLITE
            )
            if queue is not None and queue.available else 0
        )
        if eplan.satellite_action is not None and pending_satellites == 0:
            sat_id = eplan.satellite_action[1]
            ships = [eplan.satellite_action] + [s for s in ships if s[1] != sat_id]

    if tick_index % 2 == 0:
        return facilities + research + ships
    return facilities + ships + research


def assign_all(
    n: int = 90,
    prefix: str = "bot",
    domain: str = "bots.local",
    seed: str = DEFAULT_SEED,
) -> dict[str, str]:
    """bot001~bot0NN 계정 전체의 (email -> personality_key) 매핑."""
    result: dict[str, str] = {}
    for i in range(1, n + 1):
        email = f"{prefix}{i:03d}@{domain}"
        result[email] = assign_personality(email, seed)
    return result


def summary(n: int = 90, seed: str = DEFAULT_SEED) -> str:
    """90명 배정 결과를 성격별 인원수로 요약한 문자열."""
    counts: dict[str, int] = {k: 0 for k in PERSONALITY_KEYS}
    for pkey in assign_all(n, seed=seed).values():
        counts[pkey] += 1
    lines = [f"성격 배정 요약 (n={n}, seed={seed!r}):"]
    for k in PERSONALITY_KEYS:
        p = PERSONALITIES[k]
        lines.append(
            f"  {p['label']:<8}({k:<10}) {counts[k]:>2}명  weights={p['weights']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    print(summary(n))
    print()
    # 처음 몇 명의 배정 예시
    for email, pkey in list(assign_all(n).items())[:8]:
        print(f"  {email} -> {PERSONALITIES[pkey]['label']}")
