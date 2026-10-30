"""전투 시뮬레이터 — 서버 전투 엔진(Rust)을 복제한 순수 함수 모듈.

## 목적
레이더가 "방어 0" 타겟만 치는 겁쟁이 게이트를 넘어, "이 함대로 저 방어/함대를 치면
승률·예상손실이 얼마"를 미리 계산해 EV(약탈 + 데브리 − 손실) 기반으로 공격을 판단하게
하는 기반 모듈. 터틀 방어 확산으로 방어 0 타겟이 고갈 중이라 필요하다. 이 파일은 순수
계산만 하며(HTTP/DB/전역상태 없음), 통합(raider 배선)은 별도로 한다.

## 서버 전투 공식 (ogamex 소스 실측, 프로덕션 = Rust 엔진)
근거: app/GameMissions/AttackMission.php(기본 엔진 'rust'), rust/battle_engine_ffi/src/lib.rs,
app/GameObjects/{Civil,Military}ShipObjects.php · DefenseObjects.php(스탯/단가/rapidfire),
app/GameObjects/Services/Properties/*(연구 보정), app/GameMissions/BattleEngine/BattleEngine.php
(데브리/방어복구), app/Services/SettingsService.php(기본 비율).

  1. 스탯: 각 유닛은 (구조체력 structure, 실드 shield, 공격력 attack). 전투용 선체(hull)는
     floor(structure/10). 공격/실드는 원값 그대로.
  2. 연구 보정(정수절삭): attack = base*(100+10*무기)//100, shield = base*(100+10*실드)//100,
     structure = base*(100+10*장갑)//100 → hull = structure//10. (서버 intdiv 복제.)
  3. 라운드: 최대 6. 매 라운드 공격측이 먼저 전 유닛 사격 → 방어측이 사격 → 라운드 끝에
     선체≤0 제거 + 생존 유닛 실드를 max 로 복구(라운드 내엔 실드가 소진되고 복구 안 됨).
  4. 사격: 대상은 생존 적 중 균일난수 1기. 피해는 실드 먼저 흡수(부분흡수 — 넘치면 선체로),
     실드 0이면 선체로. 바운스: 공격력 < 대상 max실드 × 1% 이면 무피해 + 그 유닛 사격 종료
     (rapidfire 없음). 폭발: 매 사격 후 선체비 < 70% 면 확률 (1−선체비)×100% 로 즉시 격파.
  5. rapidfire(연사): 사격 후 사수가 대상 종류에 연사표를 가지면 확률 (rf−1)/rf 로 새 무작위
     대상에 추가 사격(연쇄). 방어시설·정찰기·위성·크롤러는 연사표 없음.
  6. 데브리: 격파된 **함선** 메탈+크리스탈 단가의 30%(양측 함선 모두). 방어시설 0%, 듀테륨 off.
  7. 방어 복구: 전투 후 격파된 방어시설의 70%가 무상 재건(유닛별 베르누이) → 방어측 영구
     손실은 격파분의 30%만.
  8. 난수: Rust 엔진은 시드 불가(thread_rng) → 확률적. 여기선 몬테카를로 소표본으로 근사하고,
     재현성을 위해 seed 파라미터로 로컬 난수를 고정할 수 있다(서버와 값이 같진 않고 분포가 같다).

## OGameX General 클래스 주의
BattleEngine.php 는 전투 보고서의 연구 레벨에는 General +2를 더하지만, 실제 Rust 입력 스탯은
GameObjectProperty::calculate(PlayerService)로 만들며 이 경로에는 클래스 보너스가 연결돼 있지
않다. 즉 현재 서버에서 +2는 **보고서 표시값일 뿐 실전 스탯에는 적용되지 않는다**. 이 모듈은
의도된 원작 규칙이 아니라 현재 배포된 서버 동작을 복제하므로 att_class/def_class를 받아도
스탯에는 더하지 않는다. 서버 쪽 배선이 고쳐지면 백테스트와 함께 이 부분도 바꿔야 한다.

## API
  simulate(attacker, defender, att_tech, def_tech, rounds=6, samples=20, seed=None) -> dict
  attack_ev(loot_estimate, sim_result, has_recycler, fuel_cost=0.0) -> float
  flight_distance(from_coord, to_coord) -> int        # 서버 비행 거리 근사(연료 항용)
  fuel_estimate(fleet, distance, speed_factor=4.0) -> float  # 왕복 듀테륨 소비 근사
  combat_stats(unit_id, weapon=0, shield=0, armor=0) -> dict  # 손계산 대조/디버그용
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import time

# ---------------------------------------------------------------------------
# 유닛 스탯 (structure, shield, attack) — ogamex GameObjects 실측
#   전투 선체 hull = floor(structure/10). 공격/실드는 원값.
# ---------------------------------------------------------------------------
_STATS: dict[int, tuple[int, int, int]] = {
    # 민간/수송/정찰 (CivilShipObjects.php)
    202: (4000, 10, 5),        # small_cargo
    203: (12000, 25, 5),       # large_cargo
    208: (30000, 100, 50),     # colony_ship
    209: (16000, 10, 1),       # recycler
    210: (1000, 0, 0),         # espionage_probe
    212: (2000, 1, 0),         # solar_satellite
    217: (4000, 1, 1),         # crawler
    219: (23000, 100, 200),    # pathfinder
    # 군용 (MilitaryShipObjects.php)
    204: (4000, 10, 50),       # light_fighter
    205: (10000, 25, 150),     # heavy_fighter
    206: (27000, 50, 400),     # cruiser
    207: (60000, 200, 1000),   # battle_ship
    215: (70000, 400, 700),    # battlecruiser
    211: (75000, 500, 1000),   # bomber
    213: (110000, 500, 2000),  # destroyer
    214: (9000000, 50000, 200000),  # deathstar
    218: (140000, 700, 2800),  # reaper
    # 방어시설 (DefenseObjects.php) — 연사표 없음
    401: (2000, 20, 80),       # rocket_launcher
    402: (2000, 25, 100),      # light_laser
    403: (8000, 100, 250),     # heavy_laser
    404: (35000, 200, 1100),   # gauss_cannon
    405: (8000, 500, 150),     # ion_cannon
    406: (100000, 300, 3000),  # plasma_turret
    407: (20000, 2000, 1),     # small_shield_dome
    408: (100000, 10000, 1),   # large_shield_dome
}

# 단가 (metal, crystal, deuterium) — 손실가치/데브리 산정용. GameObjectPrice 실측.
_COST: dict[int, tuple[int, int, int]] = {
    202: (2000, 2000, 0),      203: (6000, 6000, 0),
    208: (10000, 20000, 10000), 209: (10000, 6000, 2000),
    210: (0, 1000, 0),         212: (0, 2000, 500),
    217: (2000, 2000, 1000),   219: (8000, 15000, 8000),
    204: (3000, 1000, 0),      205: (6000, 4000, 0),
    206: (20000, 7000, 2000),  207: (45000, 15000, 0),
    215: (30000, 40000, 15000), 211: (50000, 25000, 15000),
    213: (60000, 50000, 15000), 214: (5000000, 4000000, 1000000),
    218: (85000, 55000, 20000),
    401: (2000, 0, 0),         402: (1500, 500, 0),
    403: (6000, 2000, 0),      404: (20000, 15000, 2000),
    405: (2000, 6000, 0),      406: (50000, 50000, 30000),
    407: (10000, 10000, 0),    408: (50000, 50000, 0),
}

# 연사표(사수 id -> {대상 id: rf 배수}). 방어시설/정찰기/위성/크롤러는 연사 없음.
# 근거: GameObjects 의 GameObjectRapidfire('target', amount).
_RAPIDFIRE: dict[int, dict[int, int]] = {
    202: {210: 5, 212: 5, 217: 5},                       # small_cargo
    203: {210: 5, 212: 5, 217: 5},                       # large_cargo
    208: {210: 5, 212: 5, 217: 5},                       # colony_ship
    209: {210: 5, 212: 5, 217: 5},                       # recycler
    219: {210: 5, 212: 5, 202: 3, 217: 5},               # pathfinder
    204: {210: 5, 212: 5, 217: 5},                       # light_fighter
    205: {210: 5, 212: 5, 202: 3, 217: 5},               # heavy_fighter
    206: {210: 5, 212: 5, 204: 6, 401: 10, 217: 5},      # cruiser
    207: {210: 5, 212: 5, 217: 5},                       # battle_ship
    215: {210: 5, 212: 5, 205: 4, 206: 4, 207: 7, 202: 3, 203: 3, 217: 5},  # battlecruiser
    211: {210: 5, 212: 5, 401: 20, 402: 20, 403: 10, 405: 10, 404: 5, 406: 5, 217: 5},  # bomber
    213: {210: 5, 212: 5, 402: 10, 215: 2, 217: 5},      # destroyer
    218: {210: 5, 212: 5, 204: 4, 205: 3, 202: 3, 203: 3, 217: 5},  # reaper
    214: {210: 250, 212: 250, 204: 200, 205: 100, 206: 33, 207: 30, 211: 25,
          213: 5, 202: 250, 203: 250, 208: 250, 209: 250, 401: 200, 402: 200,
          403: 100, 405: 100, 404: 50, 215: 15, 219: 30, 218: 10, 217: 250},  # deathstar
}

# 함선 기본 듀테륨 소비(rawValue) — ogamex GameObjects properties 실측
#   (GameObjectProperties 인자 순서: structure, shield, attack, speed, capacity, **fuel**).
# 연료 항(사이징 EV)에서만 쓴다. 방어시설/위성 등 비행 안 하는 유닛은 목록에서 제외(0).
_FUEL: dict[int, int] = {
    202: 10, 203: 50, 208: 100, 209: 300, 210: 1, 219: 300,   # 민간/수송/정찰
    204: 20, 205: 75, 206: 300, 207: 500, 215: 250, 211: 700,  # 군용
    213: 1000, 218: 1100,
}

# 연료 근사 상수 — 서버 실제 소비(FleetMissionService.calculateConsumption)는 함선별 속도·
# 비행시간·드라이브 연구·클래스 할인까지 반영하지만, shadow 계산 시점엔 그 입력(플릿
# speed_percent, 드라이브 레벨, 캐릭터 클래스)이 없다. 그래서 서버 공식을 100% 속도·최저속
# 함선 기준 상한으로 근사한다: 100%(speed_percent=10)에서 편대를 최저속 함선에 맞추면 함선별
# 속도항 (shipSpeedValue/10+1)^2 의 상한이 (1+1)^2=4 이므로,
#   소비 ≈ Σ(기본연료×수량) × 거리/35000 × 4.
# 이 값은 서버가 파견 시 1회 부과하는 **왕복 임무 전체 소비**에 해당한다(거리 1회 반영이 곧
# 전체 미션 연료). 드라이브 연구/제너럴(-50%) 할인을 무시하므로 보수적(과대) 추정이다.
FUEL_SPEED_FACTOR = 4.0
FUEL_DISTANCE_UNIT = 35000

# 튜닝 상수 (서버 기본값)
MAX_ROUNDS = 6
DEFAULT_SAMPLES = 20
DEBRIS_SHIP_PCT = 0.30          # 격파 함선 메탈+크리스탈의 데브리 전환율
DEFENSE_REPAIR_RATE = 0.70      # 전투 후 방어시설 무상 재건 비율(영구손실 = 격파분×0.30)
# raider.py 와 일치: 리사이클러 미보유 시 데브리 가치 할인.
DEBRIS_NO_RECYCLER_FACTOR = 0.30

# 연구 키(technologyId)
WEAPON_TECH = 109
SHIELD_TECH = 110
ARMOR_TECH = 111

# BattleEngine 보고서에 표시되는 General 보너스. 실제 Rust 입력에는 현재 연결되지 않는다
# (상단 "General 클래스 주의" 참고). 백테스트가 report의 표시 레벨을 원래 연구 레벨로
# 되돌릴 때 쓰도록 상수는 남긴다.
GENERAL_REPORTED_COMBAT_BONUS = 2

_DEFENSE_IDS = frozenset({401, 402, 403, 404, 405, 406, 407, 408})

# ---- EV 게이트 기본 임계 (decide) ----
WIN_THRESHOLD = 0.85   # 승률이 이 미만이면 공격 회피
MIN_EV = 0.0           # 종합 기댓값이 이 미만이면 회피

# ---- 안전핀 (safety pin) 튜닝 ----
STATE_DIR = pathlib.Path(__file__).resolve().parent / ".battle_sim_state"
OVERRUN_FACTOR = 2.0      # 실제 손실 > 예측 × 이 배수 = 초과(overrun)
OVERRUN_ABS_FLOOR = 10000  # 예측이 0~미미할 때 소음으로 트립하지 않도록 절대 하한(자원가치)
TRIP_STREAK = 3           # 초과가 이 횟수 연속되면 안전핀 트립 → 방어0 게이트 복귀
RECENT_CAP = 20           # 상태파일에 보관할 최근 전투 기록 수


# ---------------------------------------------------------------------------
# 스탯 계산 (연구 보정)
# ---------------------------------------------------------------------------
def combat_stats(unit_id: int, weapon: int = 0, shield: int = 0, armor: int = 0) -> dict:
    """유닛 1기의 전투 스탯(연구 보정 적용)을 반환. 손계산 대조/디버그용.

    반환: {"attack","shield","hull","structure"} — 서버 intdiv 정수절삭 복제.
    hull = floor(보정 structure / 10).
    """
    if unit_id not in _STATS:
        raise ValueError(f"unknown unit_id={unit_id} (스탯 미등록)")
    struct, base_shield, base_attack = _STATS[unit_id]
    a = base_attack * (100 + 10 * weapon) // 100
    s = base_shield * (100 + 10 * shield) // 100
    st = struct * (100 + 10 * armor) // 100
    return {"attack": a, "shield": s, "hull": st // 10, "structure": st}


def _class_bonus(character_class: str | None) -> int:
    """현재 OGameX **실전 엔진**의 클래스 전투 보정(항상 0).

    BattleEngine 결과/보고서에는 General +2가 기록되지만 RustBattleEngine.prepareBattleInput()
    은 raw PlayerService 연구 레벨로 유닛 스탯을 계산한다. 인자는 API 호환성과 향후 서버 수정
    대비용으로 유지한다.
    """
    _ = character_class
    return 0


def _tech_levels(tech: dict | None, character_class: str | None = None) -> tuple[int, int, int]:
    """tech dict + 캐릭터 클래스에서 (무기, 실드, 장갑) 유효 레벨 추출. 없으면 0.

    현재 서버 실전 경로에서는 클래스 보너스가 Rust 입력에 연결되지 않아 +0이다. 전투 보고서
    값을 입력할 때는 General 표시 보너스(+2)를 먼저 빼야 한다(백테스트 스크립트 참고).
    """
    t = tech or {}
    b = _class_bonus(character_class)
    return (
        int(t.get(WEAPON_TECH, 0) or 0) + b,
        int(t.get(SHIELD_TECH, 0) or 0) + b,
        int(t.get(ARMOR_TECH, 0) or 0) + b,
    )


def _res_value(cost: tuple[int, int, int]) -> int:
    """단가 튜플(m,c,d)의 총 자원가치(합)."""
    return cost[0] + cost[1] + cost[2]


def supported_unit_ids() -> frozenset[int]:
    """이 시뮬레이터가 스탯/단가를 모두 아는 전투 유닛 id 집합."""
    return frozenset(_STATS).intersection(_COST)


def fleet_cost_value(units: dict[int, int]) -> float:
    """함대/방어 구성의 총 건조가치(metal+crystal+deuterium)."""
    return _loss_value(units, repair_defenses=False)


# ---------------------------------------------------------------------------
# 한 진영 구성 (메타 목록)
# ---------------------------------------------------------------------------
# 메타: (attack, max_shield, max_hull, unit_id, rapidfire_dict, count)
def _build_metas(units: dict[int, int], weapon: int, shield: int, armor: int) -> list[tuple]:
    metas: list[tuple] = []
    for uid, cnt in units.items():
        cnt = int(cnt)
        if cnt <= 0:
            continue
        cs = combat_stats(uid, weapon, shield, armor)
        metas.append(
            (cs["attack"], cs["shield"], cs["hull"], uid, _RAPIDFIRE.get(uid, {}), cnt)
        )
    return metas


def _spawn(metas: list[tuple]) -> list[list]:
    """메타에서 개별 인스턴스 [current_shield, current_hull, meta_idx] 목록 생성."""
    inst: list[list] = []
    for idx, m in enumerate(metas):
        msh = float(m[1])
        mhull = float(m[2])
        inst.extend([msh, mhull, idx] for _ in range(m[5]))
    return inst


# ---------------------------------------------------------------------------
# 사격 / 라운드 (Rust process_combat 복제)
# ---------------------------------------------------------------------------
def _fire(firers: list[list], targets: list[list], metas_f: list[tuple],
          metas_t: list[tuple], rng: random.Random) -> None:
    """firers 전 유닛이 targets 를 사격(라운드 내 1회, rapidfire 로 연쇄)."""
    if not targets:
        return
    n = len(targets)          # 라운드 내 고정(격파 유닛도 제거 전이라 대상에 남음)
    rand = rng.random
    randrange = rng.randrange
    randint = rng.randint
    for f in firers:
        fmeta = metas_f[f[2]]
        atk = fmeta[0]
        frf = fmeta[4]
        while True:
            tgt = targets[randrange(n)]
            tmeta = metas_t[tgt[2]]
            t_msh = tmeta[1]
            t_mhull = tmeta[2]
            # 바운스: 공격력 < 대상 max실드 × 1% → 무피해 + 이 유닛 사격 종료(연사 없음)
            if atk < 0.01 * t_msh:
                break
            # 실드 부분흡수 → 넘치면 선체로
            if tgt[0] > 0:
                if atk <= tgt[0]:
                    tgt[0] -= atk
                else:
                    tgt[1] -= atk - tgt[0]
                    tgt[0] = 0.0
            else:
                tgt[1] -= atk
            # 폭발 판정: 선체비 < 70% 면 확률 (1−비율)×100% 로 격파
            if t_mhull > 0:
                ratio = tgt[1] / t_mhull
                if ratio < 0.7:
                    if randint(0, 100) < 100.0 - ratio * 100.0:
                        tgt[1] = 0.0
                        tgt[0] = 0.0
            # rapidfire: 확률 (rf−1)/rf 로 새 무작위 대상 추가 사격
            rf = frf.get(tmeta[3])
            if rf and rf > 1 and rand() < (rf - 1) / rf:
                continue
            break


def _cleanup(side: list[list], metas: list[tuple]) -> list[list]:
    """선체≤0 제거 + 생존 유닛 실드를 max 로 복구."""
    survivors: list[list] = []
    for inst in side:
        if inst[1] > 0:
            inst[0] = float(metas[inst[2]][1])
            survivors.append(inst)
    return survivors


def _tally_lost(metas: list[tuple], survivors: list[list]) -> dict[int, int]:
    """진영 metas(초기 수량)와 생존 인스턴스로 uid별 손실 수량 계산."""
    surv: dict[int, int] = {}
    for inst in survivors:
        uid = metas[inst[2]][3]
        surv[uid] = surv.get(uid, 0) + 1
    lost: dict[int, int] = {}
    for m in metas:
        uid = m[3]
        n_lost = m[5] - surv.get(uid, 0)
        if n_lost:
            lost[uid] = lost.get(uid, 0) + n_lost
    return lost


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def simulate(
    attacker: dict[int, int],
    defender: dict[int, int],
    att_tech: dict | None = None,
    def_tech: dict | None = None,
    rounds: int = MAX_ROUNDS,
    samples: int = DEFAULT_SAMPLES,
    seed: int | None = None,
    att_class: str | None = None,
    def_class: str | None = None,
) -> dict:
    """공격 함대 vs 방어 함대 전투를 몬테카를로로 근사해 결과 통계를 반환한다.

    attacker/defender: {unit_id: count}. att_tech/def_tech: {109:무기,110:실드,111:장갑}.
    att_class/def_class: 캐릭터 클래스명. 현재 OGameX Rust 실전 스탯에는
      General 보고서 표시 보너스(+2)가 연결되지 않으므로 결과에 영향을 주지 않는다.
      여기의 tech는 보고서 표시값이 아닌 raw 연구 레벨이어야 한다.
    samples: 몬테카를로 표본 수(서버 난수 시드 불가 → 확률 근사). seed 로 재현 가능.

    반환 dict:
      win_prob            : 공격측 완승(방어측 전멸+공격측 생존) 표본 비율.
      defender_win_prob   : 방어측 완승(공격측 전멸) 표본 비율(대칭 검증·참고용).
      draw_prob           : 무승부(양측 생존 or 상호 전멸) 비율.
      attacker_loss_value : 공격측 기대 손실(자원가치 m+c+d 합).
      defender_loss_value : 방어측 기대 영구손실(함선 전액 + 방어시설은 복구율 반영 30%).
      expected_debris     : (metal, crystal) 기대 데브리(양측 격파 함선의 30%).
      samples             : 사용 표본 수.
    """
    aw, ash, aar = _tech_levels(att_tech, att_class)
    dw, dsh, dar = _tech_levels(def_tech, def_class)
    metas_a = _build_metas(attacker, aw, ash, aar)
    metas_d = _build_metas(defender, dw, dsh, dar)

    if samples < 1:
        samples = 1
    rng = random.Random(seed)

    # 빈 함대 처리: 공격측이 비면 승률 0, 방어측이 비면 무혈 승리.
    if not metas_a:
        return _empty_result(samples, attacker_absent=True)
    if not metas_d:
        return _empty_result(samples, attacker_absent=False)

    att_wins = def_wins = draws = 0
    sum_att_loss = 0.0
    sum_def_loss = 0.0
    sum_deb_m = 0.0
    sum_deb_c = 0.0

    for _ in range(samples):
        A = _spawn(metas_a)
        D = _spawn(metas_d)
        for _r in range(rounds):
            if not A or not D:
                break
            _fire(A, D, metas_a, metas_d, rng)
            _fire(D, A, metas_d, metas_a, rng)
            A = _cleanup(A, metas_a)
            D = _cleanup(D, metas_d)

        if A and not D:
            att_wins += 1
        elif D and not A:
            def_wins += 1
        else:
            draws += 1

        lost_a = _tally_lost(metas_a, A)
        lost_d = _tally_lost(metas_d, D)
        sum_att_loss += _loss_value(lost_a, repair_defenses=False)
        sum_def_loss += _loss_value(lost_d, repair_defenses=True)
        dm, dc = _debris(lost_a, lost_d)
        sum_deb_m += dm
        sum_deb_c += dc

    inv = 1.0 / samples
    return {
        "win_prob": att_wins * inv,
        "defender_win_prob": def_wins * inv,
        "draw_prob": draws * inv,
        "attacker_loss_value": sum_att_loss * inv,
        "defender_loss_value": sum_def_loss * inv,
        "expected_debris": (sum_deb_m * inv, sum_deb_c * inv),
        "samples": samples,
    }


def _loss_value(lost: dict[int, int], repair_defenses: bool) -> float:
    """손실 수량 dict 의 총 자원가치. repair_defenses=True 면 방어시설은 복구율 반영(30%만)."""
    total = 0.0
    for uid, cnt in lost.items():
        val = _res_value(_COST.get(uid, (0, 0, 0))) * cnt
        if repair_defenses and uid in _DEFENSE_IDS:
            val *= (1.0 - DEFENSE_REPAIR_RATE)
        total += val
    return total


def _debris(lost_a: dict[int, int], lost_d: dict[int, int]) -> tuple[float, float]:
    """양측 격파 **함선**(방어시설 제외)의 메탈/크리스탈 30% 데브리."""
    dm = dc = 0.0
    for lost in (lost_a, lost_d):
        for uid, cnt in lost.items():
            if uid in _DEFENSE_IDS:
                continue  # 방어시설 데브리 0%
            m, c, _ = _COST.get(uid, (0, 0, 0))
            dm += m * cnt * DEBRIS_SHIP_PCT
            dc += c * cnt * DEBRIS_SHIP_PCT
    return dm, dc


def _empty_result(samples: int, attacker_absent: bool) -> dict:
    """빈 함대 단락 결과. 공격측 부재→승률0, 방어측 부재→무혈 승리."""
    return {
        "win_prob": 0.0 if attacker_absent else 1.0,
        "defender_win_prob": 1.0 if attacker_absent else 0.0,
        "draw_prob": 0.0,
        "attacker_loss_value": 0.0,
        "defender_loss_value": 0.0,
        "expected_debris": (0.0, 0.0),
        "samples": samples,
    }


def attack_ev(loot_estimate: float, sim_result: dict, has_recycler: bool,
              fuel_cost: float = 0.0) -> float:
    """공격의 종합 기댓값(자원가치). 양수면 공격이 기대 이득.

    EV = win_prob × 약탈 + 데브리가치(리사이클러 없으면 0.3배 할인) − 공격측 기대손실 − 왕복연료.
      - 약탈은 승리 시에만 실현되므로 win_prob 로 가중한다.
      - 데브리는 승패와 무관히 생성되나, 회수하려면 리사이클러가 필요하므로 미보유 시
        raider.py 와 동일하게 0.3배로 할인한다(당장 못 줍는 자원).
      - 공격측 손실은 표본 평균(승/패 모두 반영)이라 그대로 차감한다.
      - fuel_cost(선택, 기본 0.0): 왕복 듀테륨 소비의 자원가치. 편대 사이징 EV 비교에서
        원거리 대편대 몰빵을 억제하는 항이다. 기본 0.0 이라 이 인자를 주지 않는 기존
        호출부/테스트의 결과는 그대로다(하위호환).
    """
    debris_m, debris_c = sim_result.get("expected_debris", (0.0, 0.0))
    debris_val = debris_m + debris_c
    if not has_recycler:
        debris_val *= DEBRIS_NO_RECYCLER_FACTOR
    win = sim_result.get("win_prob", 0.0)
    return (win * loot_estimate + debris_val
            - sim_result.get("attacker_loss_value", 0.0) - float(fuel_cost))


def flight_distance(from_coord, to_coord) -> int:
    """두 좌표 (galaxy, system, position) 간 서버 비행 거리 근사(연료 항용).

    FleetMissionService.calculateFleetMissionDistance 복제:
      은하 다름 → 20000×Δ은하, 시스템 다름 → 2700 + 19×5×Δ시스템, 위치 다름 → 1000 + 5×Δ위치,
      동일 → 5. 단 shadow 에서는 사이에 낀 빈/비활성 시스템 축소를 알 수 없어 생략한다(약간
      과대 추정). 좌표가 없거나 손상되면 0(→ 연료 항 0).
    """
    if not from_coord or not to_coord:
        return 0
    try:
        g1, s1, p1 = int(from_coord[0]), int(from_coord[1]), int(from_coord[2])
        g2, s2, p2 = int(to_coord[0]), int(to_coord[1]), int(to_coord[2])
    except (TypeError, ValueError, IndexError):
        return 0
    if g1 != g2:
        return abs(g1 - g2) * 20000
    if s1 != s2:
        return abs(s1 - s2) * 19 * 5 + 2700
    if p1 != p2:
        return abs(p1 - p2) * 5 + 1000
    return 5


def fuel_estimate(fleet: dict[int, int], distance: int,
                  speed_factor: float = FUEL_SPEED_FACTOR) -> float:
    """편대 구성 + 거리로 왕복 임무 듀테륨 소비를 근사한다(위 FUEL_SPEED_FACTOR 주석 참고).

    소비 ≈ Σ(기본연료×수량) × 거리/35000 × speed_factor. 거리 0(좌표 미상)이면 0.0.
    """
    if distance <= 0:
        return 0.0
    total = 0.0
    for uid, cnt in fleet.items():
        base = _FUEL.get(int(uid))
        if not base:
            continue
        cnt = int(cnt)
        if cnt <= 0:
            continue
        total += base * cnt
    return total * (distance / FUEL_DISTANCE_UNIT) * float(speed_factor)


# ---------------------------------------------------------------------------
# 결정 API — 로그 모드 / 라이브 모드 (배포 게이트 2)
# ---------------------------------------------------------------------------
def decide(
    attacker: dict[int, int],
    defender: dict[int, int],
    loot_estimate: float,
    has_recycler: bool,
    att_tech: dict | None = None,
    def_tech: dict | None = None,
    att_class: str | None = None,
    def_class: str | None = None,
    mode: str = "log",
    win_threshold: float = WIN_THRESHOLD,
    min_ev: float = MIN_EV,
    samples: int = DEFAULT_SAMPLES,
    seed: int | None = None,
) -> dict:
    """시뮬 기반 공격 권고를 계산한다. 로그/라이브 모드 공용(배포 게이트 2).

    ## 왜 두 모드인가 (사용자 지시)
    시뮬 승률이 서버와 조금만 어긋나도 레이더가 요새에 자살할 수 있어, 통합 첫 형태는
    **로그 모드**로 몇 시간 적중률을 관측한 뒤 스위치 온한다.
      - mode="log" : 공격 결정은 호출부의 기존 "방어 0" 게이트가 그대로 한다. 이 함수는
        "나라면: 승률 X%, 예상손실 Y, EV Z → 공격/회피" 를 log 문자열로만 돌려준다(비집행).
      - mode="live": recommend(공격 권고 bool)를 호출부가 실제 집행에 쓴다.
    두 모드의 계산은 동일하고, mode 는 result["mode"] 와 log 문구·집행 여부 해석만 바꾼다.

    반환 dict:
      recommend  : 시뮬 권고(승률≥win_threshold 이고 EV≥min_ev). 라이브에서만 집행에 사용.
      sim        : simulate() 원결과.
      ev         : attack_ev 종합 기댓값.
      reason     : 권고/회피 사유(간단 코드).
      log        : "[battle_sim/log] ..." 관측용 한 줄.
      mode       : "log" | "live".
    """
    sim = simulate(
        attacker, defender, att_tech=att_tech, def_tech=def_tech,
        att_class=att_class, def_class=def_class, samples=samples, seed=seed,
    )
    ev = attack_ev(loot_estimate, sim, has_recycler)
    win = sim["win_prob"]
    if win < win_threshold:
        recommend, reason = False, f"승률 {win:.0%}<{win_threshold:.0%}"
    elif ev < min_ev:
        recommend, reason = False, f"EV {ev:.0f}<{min_ev:.0f}"
    else:
        recommend, reason = True, f"승률 {win:.0%}·EV {ev:.0f}"
    verb = "공격" if recommend else "회피"
    log = (f"[battle_sim/{mode}] 나라면: 승률 {win:.0%}, 예상손실 "
           f"{sim['attacker_loss_value']:.0f}, EV {ev:.0f} → {verb} ({reason})")
    return {"recommend": recommend, "sim": sim, "ev": ev, "reason": reason,
            "log": log, "mode": mode}


# ---------------------------------------------------------------------------
# 안전핀 — 예측 대비 실손실 폭주 시 자동 방어0 게이트 복귀 (배포 게이트 3)
# ---------------------------------------------------------------------------
# 상태파일(계정별 JSON) 스키마: storage.py 백오프 패턴 참고.
#   {"recent": [{"pred":.., "actual":.., "overrun":bool, "ts":..}, ...],  # 최근 RECENT_CAP 건
#    "streak": int,        # 연속 초과 횟수
#    "tripped": bool,      # 안전핀 발동 여부
#    "tripped_at": float}  # 발동 시각(epoch)
# 스레드 안전: 한 계정은 한 스레드가 전담(run_universe) → 파일 접근이 계정 틱 내 순차적.
def _safety_path(state_key: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in state_key)
    return STATE_DIR / f"{safe}.json"


def _safety_load(state_key: str) -> dict:
    try:
        data = json.loads(_safety_path(state_key).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"recent": [], "streak": 0, "tripped": False, "tripped_at": 0.0}


def _safety_save(state_key: str, data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _safety_path(state_key)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 저장 실패는 다음 전투에서 스트릭이 잠깐 리셋될 뿐 — 조용히 무시


def record_outcome(state_key: str, predicted_loss: float, actual_loss: float) -> dict:
    """라이브 전투 1건의 (예측손실, 실제손실)을 기록하고 안전핀 상태를 갱신한다.

    초과(overrun) 판정: 실제손실 > max(예측손실 × OVERRUN_FACTOR, OVERRUN_ABS_FLOOR).
    (예측이 0~미미할 때 소음으로 트립하지 않도록 절대 하한을 둔다.) 초과가 TRIP_STREAK 회
    연속되면 tripped=True 로 발동한다. 한 번 발동하면 reset_safety 전까지 유지된다.

    반환: {"tripped":bool, "just_tripped":bool, "streak":int, "overrun":bool}.
    just_tripped 가 True 인 순간 호출부가 stall_events(metrics.db) 를 기록하면 된다
    (metrics.db 라이브 배선은 팀리드 담당 — 이 모듈은 상태파일 트립 로직만 제공).
    """
    data = _safety_load(state_key)
    overrun = actual_loss > max(OVERRUN_FACTOR * predicted_loss, OVERRUN_ABS_FLOOR)
    data["streak"] = (data.get("streak", 0) + 1) if overrun else 0
    data.setdefault("recent", []).append({
        "pred": float(predicted_loss), "actual": float(actual_loss),
        "overrun": overrun, "ts": time.time(),
    })
    data["recent"] = data["recent"][-RECENT_CAP:]
    just_tripped = False
    if not data.get("tripped", False) and data["streak"] >= TRIP_STREAK:
        data["tripped"] = True
        data["tripped_at"] = time.time()
        just_tripped = True
    _safety_save(state_key, data)
    return {"tripped": data["tripped"], "just_tripped": just_tripped,
            "streak": data["streak"], "overrun": overrun}


def is_tripped(state_key: str) -> bool:
    """안전핀이 발동돼 있으면 True → 호출부는 시뮬 게이트를 끄고 방어0 게이트로 복귀한다."""
    return bool(_safety_load(state_key).get("tripped", False))


def reset_safety(state_key: str) -> None:
    """안전핀 수동 해제(원인 수정·재배포 후). 상태파일을 초기화한다."""
    _safety_save(state_key, {"recent": [], "streak": 0, "tripped": False, "tripped_at": 0.0})
