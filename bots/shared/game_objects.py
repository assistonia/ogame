"""OGameX technologyId <-> 이름 매핑 상수.

모든 ID는 OGameX 소스에서 직접 확인한 값이다 (추측 아님). 근거 파일:
  - 자원 건물  : app/GameObjects/BuildingObjects.php
  - 시설(스테이션): app/GameObjects/StationObjects.php
  - 연구        : app/GameObjects/ResearchObjects.php
  - 민간 함선   : app/GameObjects/CivilShipObjects.php
  - 군용 함선   : app/GameObjects/MilitaryShipObjects.php

이름은 소스의 machine_name 을 그대로 사용한다 (OGameX 내부 canonical key).
add-buildrequest 는 이 id 를 technologyId 파라미터로 보낸다.
"""
from __future__ import annotations

# --- 자원 건물 (BuildingObjects.php) : /resources 페이지, GameObjectType::Building ---
BUILDINGS: dict[int, str] = {
    1: "metal_mine",              # BuildingObjects.php:26  메탈 광산
    2: "crystal_mine",            # BuildingObjects.php:50  크리스탈 광산
    3: "deuterium_synthesizer",   # BuildingObjects.php:73  듀테륨 합성소
    4: "solar_plant",             # BuildingObjects.php:98  태양광 발전소
    12: "fusion_plant",           # BuildingObjects.php:120 핵융합로
    22: "metal_store",            # BuildingObjects.php:148 메탈 저장소
    23: "crystal_store",          # BuildingObjects.php:168 크리스탈 저장소
    24: "deuterium_store",        # BuildingObjects.php:186 듀테륨 탱크
}

# --- 시설 / 스테이션 (StationObjects.php) : /facilities 페이지, GameObjectType::Station ---
FACILITIES: dict[int, str] = {
    14: "robot_factory",   # StationObjects.php:24  로봇 공장
    21: "shipyard",        # StationObjects.php:38  조선소 (요구: robot_factory 2)
    31: "research_lab",    # StationObjects.php:56  연구소
    34: "alliance_depot",  # StationObjects.php:72  동맹 창고
    44: "missile_silo",    # StationObjects.php:88  미사일 격납고
    15: "nano_factory",    # StationObjects.php:106 나노봇 공장
    33: "terraformer",     # StationObjects.php:126 테라포머
    36: "space_dock",      # StationObjects.php:145 우주 정박소
    41: "lunar_base",      # StationObjects.php:166 달 기지 (달 전용)
    42: "sensor_phalanx",  # StationObjects.php:182 센서 팔랑크스 (달 전용)
    43: "jump_gate",       # StationObjects.php:200 점프 게이트 (달 전용)
}

# --- 연구 (ResearchObjects.php) : /research 페이지, GameObjectType::Research ---
RESEARCH: dict[int, str] = {
    113: "energy_technology",              # ResearchObjects.php:23  에너지 기술
    120: "laser_technology",               # ResearchObjects.php:41  레이저 기술
    121: "ion_technology",                 # ResearchObjects.php:60  이온 기술
    114: "hyperspace_technology",          # ResearchObjects.php:80  하이퍼스페이스 기술
    122: "plasma_technology",              # ResearchObjects.php:100 플라즈마 기술
    115: "combustion_drive",               # ResearchObjects.php:121 연소 엔진
    117: "impulse_drive",                  # ResearchObjects.php:140 임펄스 엔진
    118: "hyperspace_drive",               # ResearchObjects.php:160 하이퍼스페이스 엔진
    106: "espionage_technology",           # ResearchObjects.php:179 스파이 기술
    108: "computer_technology",            # ResearchObjects.php:197 컴퓨터 기술
    124: "astrophysics",                   # ResearchObjects.php:221 천체물리학
    123: "intergalactic_research_network", # ResearchObjects.php:249 은하간 연구망
    199: "graviton_technology",            # ResearchObjects.php:269 그라비톤 기술
    109: "weapon_technology",              # ResearchObjects.php:287 무기 기술
    110: "shielding_technology",           # ResearchObjects.php:304 방어막 기술
    111: "armor_technology",               # ResearchObjects.php:324 장갑 기술
}

# --- 함선 (CivilShipObjects.php / MilitaryShipObjects.php) : /shipyard 페이지, GameObjectType::Ship ---
SHIPS: dict[int, str] = {
    # 민간 함선 (CivilShipObjects.php)
    202: "small_cargo",      # CivilShipObjects.php:27  소형 화물선 (요구: shipyard 2, combustion_drive 2)
    203: "large_cargo",      # CivilShipObjects.php:55  대형 화물선
    208: "colony_ship",      # CivilShipObjects.php:80  식민선
    209: "recycler",         # CivilShipObjects.php:105 재활용선
    210: "espionage_probe",  # CivilShipObjects.php:135 정찰기
    212: "solar_satellite",  # CivilShipObjects.php:156 태양광 위성
    217: "crawler",          # CivilShipObjects.php:181 크롤러
    219: "pathfinder",       # CivilShipObjects.php:204 패스파인더
    # 군용 함선 (MilitaryShipObjects.php)
    204: "light_fighter",    # MilitaryShipObjects.php:25  전투기
    205: "heavy_fighter",    # MilitaryShipObjects.php:49  중형 전투기
    206: "cruiser",          # MilitaryShipObjects.php:75  순양함
    207: "battle_ship",      # MilitaryShipObjects.php:104 전함
    215: "battlecruiser",    # MilitaryShipObjects.php:128 순양전함
    211: "bomber",           # MilitaryShipObjects.php:158 폭격기
    213: "destroyer",        # MilitaryShipObjects.php:194 구축함
    214: "deathstar",        # MilitaryShipObjects.php:220 데스스타
    218: "reaper",           # MilitaryShipObjects.php:266 리퍼
}

# --- 방어시설 (DefenseObjects.php) : /defense 페이지, GameObjectType::Defense ---
DEFENSES: dict[int, str] = {
    401: "rocket_launcher",         # DefenseObjects.php:24  로켓 런처 (요구: shipyard 1)
    402: "light_laser",             # DefenseObjects.php:42  경레이저
    403: "heavy_laser",             # DefenseObjects.php:61  중레이저
    404: "gauss_cannon",            # DefenseObjects.php:83  가우스포
    405: "ion_cannon",              # DefenseObjects.php:104 이온포
    406: "plasma_turret",           # DefenseObjects.php:123 플라즈마포
    407: "small_shield_dome",       # DefenseObjects.php:143 소형 실드돔
    408: "large_shield_dome",       # DefenseObjects.php:162 대형 실드돔
    502: "anti_ballistic_missile",  # DefenseObjects.php:183 요격 미사일
    503: "interplanetary_missile",  # DefenseObjects.php:203 행성간 미사일
}

# --- 통합 매핑 ---
#: id -> machine_name (전체)
ID_TO_NAME: dict[int, str] = {**BUILDINGS, **FACILITIES, **RESEARCH, **SHIPS, **DEFENSES}
#: machine_name -> id (전체)
NAME_TO_ID: dict[str, int] = {name: tid for tid, name in ID_TO_NAME.items()}


def name_of(technology_id: int) -> str:
    """technologyId 로 machine_name 조회. 미상이면 'unknown(<id>)'."""
    return ID_TO_NAME.get(technology_id, f"unknown({technology_id})")


def id_of(name: str) -> int:
    """machine_name 으로 technologyId 조회. 없으면 KeyError."""
    return NAME_TO_ID[name]


# 자주 쓰는 개별 상수 (편의용)
METAL_MINE = 1
CRYSTAL_MINE = 2
DEUTERIUM_SYNTHESIZER = 3
SOLAR_PLANT = 4
ROBOT_FACTORY = 14
SHIPYARD = 21
RESEARCH_LAB = 31
ENERGY_TECHNOLOGY = 113
COMBUSTION_DRIVE = 115
ASTROPHYSICS = 124
SMALL_CARGO = 202
SOLAR_SATELLITE = 212
ESPIONAGE_PROBE = 210
LIGHT_FIGHTER = 204
COLONY_SHIP = 208

# --- 저장고(창고) 건물 --------------------------------------------------------
# 저장고 3종. 자원별 저장 용량(planets.metal_max 등)을 레벨에 따라 올린다.
# 용량 공식(서버 BuildingObjects.php): 5000 * floor(2.5 * exp(20*level/33)).
#   level 0 = 10000 (기본 창고), 이후 레벨당 대략 배증. 봇 코드는 용량 자체를
#   재계산할 필요 없이 planets.*_max 를 직접 읽는다(db_reader.get_resource_state_db).
METAL_STORE = 22
CRYSTAL_STORE = 23
DEUTERIUM_STORE = 24
#: 자원 키("metal"/"crystal"/"deuterium") -> 그 자원을 담는 저장고 building id
STORE_FOR_RESOURCE: dict[str, int] = {
    "metal": METAL_STORE,
    "crystal": CRYSTAL_STORE,
    "deuterium": DEUTERIUM_STORE,
}

# --- 레벨 비례 비용 모델 (건물/시설/연구) -------------------------------------
# 서버 비용 공식(app/Services/ObjectService.php getObjectRawPrice)을 그대로 복제한다:
#   level L(목표 레벨)의 비용 = floor(base_r * factor^(L-1)),  단 level 0 은 0.
#   roundNearest100=True 인 항목은 100 단위 반올림(천체물리 등).
# 근거 base 값 출처: app/GameObjects/{Building,Station,Research}Objects.php 의
#   new GameObjectPrice(metal, crystal, deuterium, energy, factor, roundNearest100).
# 봇은 이 비용을 "취소 사유 분류(용량부족 vs 자원부족)"와 "저장고 선제 판정"에 쓴다.
# 함선/방어(레벨 무관 단가)는 여기 없다 — 저장고/취소 판정 대상이 아니다.
# (metal, crystal, deuterium, factor, round100)
BASE_PRICE: dict[int, tuple[float, float, float, float, bool]] = {
    # 자원 건물 (BuildingObjects.php)
    1: (60, 15, 0, 1.5, False),        # metal_mine
    2: (48, 24, 0, 1.6, False),        # crystal_mine
    3: (225, 75, 0, 1.5, False),       # deuterium_synthesizer
    4: (75, 30, 0, 1.5, False),        # solar_plant
    12: (900, 360, 180, 1.8, False),   # fusion_plant
    22: (1000, 0, 0, 2, False),        # metal_store
    23: (1000, 500, 0, 2, False),      # crystal_store
    24: (1000, 1000, 0, 2, False),     # deuterium_store
    # 시설 (StationObjects.php)
    14: (400, 120, 200, 2, False),     # robot_factory
    21: (400, 200, 100, 2, False),     # shipyard
    31: (200, 400, 200, 2, False),     # research_lab
    15: (1000000, 500000, 100000, 2, False),  # nano_factory
    # 연구 (ResearchObjects.php)
    113: (0, 800, 400, 2, False),      # energy_technology
    120: (200, 100, 0, 2, False),      # laser_technology
    121: (1000, 300, 100, 2, False),   # ion_technology
    114: (0, 4000, 2000, 2, False),    # hyperspace_technology
    122: (2000, 4000, 1000, 2, False), # plasma_technology
    115: (400, 0, 600, 2, False),      # combustion_drive
    117: (2000, 4000, 600, 2, False),  # impulse_drive
    118: (10000, 20000, 6000, 2, False),  # hyperspace_drive
    106: (200, 1000, 200, 2, False),   # espionage_technology
    108: (0, 400, 600, 2, False),      # computer_technology
    124: (4000, 8000, 4000, 1.75, True),  # astrophysics (round100)
    109: (800, 200, 0, 2, False),      # weapon_technology
    110: (200, 600, 0, 2, False),      # shielding_technology
    111: (1000, 0, 0, 2, False),       # armor_technology
}


def object_cost(technology_id: int, target_level: int) -> dict[str, int] | None:
    """건물/시설/연구를 target_level 로 올리는 비용을 {"metal","crystal","deuterium"} 로 반환.

    서버 공식(getObjectRawPrice) 복제: floor(base_r * factor^(L-1)). level 0 은 비용 0.
    BASE_PRICE 에 없는 id(함선/방어 등 레벨 무관 단가)면 None 을 돌려 호출부가
    "비용 미상"으로 처리하게 한다.
    """
    spec = BASE_PRICE.get(technology_id)
    if spec is None or target_level <= 0:
        return None
    base_m, base_c, base_d, factor, round100 = spec
    exp = factor ** (target_level - 1)
    cost = {
        "metal": _floor(base_m * exp, round100),
        "crystal": _floor(base_c * exp, round100),
        "deuterium": _floor(base_d * exp, round100),
    }
    return cost


def _floor(value: float, round100: bool) -> int:
    """비용 반올림: 기본은 내림(floor), round100 이면 100 단위 반올림(서버와 동일)."""
    import math

    v = math.floor(value)
    if round100:
        v = round(v / 100) * 100
    return int(v)


# --- 함선 단가 (레벨 무관, GameObjectPrice) ------------------------------------
# 함선은 레벨 개념이 없어 BASE_PRICE(레벨 비례) 대신 고정 단가를 쓴다. 에너지 위성
# 교차점 계산(태양광 다음 레벨 vs 위성 N기 비용 비교)에서 위성 단가가 필요하다.
# 출처: app/GameObjects/CivilShipObjects.php 의 new GameObjectPrice(metal, crystal, deuterium, ...).
SHIP_PRICE: dict[int, dict[str, int]] = {
    212: {"metal": 0, "crystal": 2000, "deuterium": 500},  # solar_satellite (CivilShipObjects.php:166)
}


def ship_cost(technology_id: int) -> dict[str, int] | None:
    """함선 1기 단가를 {"metal","crystal","deuterium"} 로 반환. 미등록이면 None."""
    return SHIP_PRICE.get(technology_id)


# --- 에너지 생산/소비 공식 (서버 소스 그대로 복제) -----------------------------
# 근거:
#   - 태양광 발전소 생산 : app/GameObjects/BuildingObjects.php:109
#       energy_formula = 20 * L * 1.1**L        (레벨 L, 생산=양수)
#   - 광산 에너지 소비   : BuildingObjects.php:39/63/87
#       metal_mine / crystal_mine  energy_formula = -10 * L * 1.1**L
#       deuterium_synthesizer      energy_formula = -20 * L * 1.1**L
#   - 태양광 위성 생산   : app/GameObjects/CivilShipObjects.php:170
#       energy_formula = floor((temp_max + 140) / 6) * count    (행성 최고온도 의존)
#
# 서버 반올림(app/Services/PlanetService.php getObjectProduction):
#   energy 는 오브젝트별로 floor() 된다. 소비(음수) 는 floor 후 abs → 실질 ceil(양수값).
#   즉 소비 = abs(floor(-x)) = ceil(x). 에너지에는 우주 배속(universe_speed)이 곱해지지
#   않는다(자원과 달리 순간값) — GameObjectProduction::calculateMine 에서 energy 만
#   building_percentage 만 곱하고 speed 는 안 곱함. 봇은 배속 무관하게 이 공식을 쓴다.
#: 광산 종류별 에너지 소비 계수 (technologyId -> coef). 소비 = ceil(coef * L * 1.1**L).
_MINE_ENERGY_COEF: dict[int, int] = {
    1: 10,   # metal_mine
    2: 10,   # crystal_mine
    3: 20,   # deuterium_synthesizer
}


def solar_plant_energy(level: int) -> int:
    """태양광 발전소 레벨 L 의 에너지 생산량. 서버: floor(20 * L * 1.1**L)."""
    import math

    if level <= 0:
        return 0
    return int(math.floor(20 * level * 1.1 ** level))


def mine_energy_consumption(technology_id: int, level: int) -> int:
    """광산(1/2/3) 레벨 L 의 에너지 소비량(양수). 서버: ceil(coef * L * 1.1**L).

    _MINE_ENERGY_COEF 에 없는 id(에너지를 안 먹는 건물)나 level<=0 이면 0.
    """
    import math

    coef = _MINE_ENERGY_COEF.get(technology_id)
    if coef is None or level <= 0:
        return 0
    return int(math.ceil(coef * level * 1.1 ** level))


def solar_satellite_energy_each(temp_max: int) -> int:
    """태양광 위성 1기의 에너지 생산량. 서버: floor((temp_max + 140) / 6).

    행성 최고온도(temp_max)가 높을수록 위성 효율이 좋다. 아주 추운 행성
    (temp_max ≤ -140)이면 0 이하 → 위성이 무의미(호출부가 태양광으로 폴백).
    """
    import math

    return int(math.floor((temp_max + 140) / 6))
