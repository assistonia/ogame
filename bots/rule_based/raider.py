"""공격형(aggressive) 규칙봇의 정찰 → 약탈 행동.

miner_bot.tick() 이 자원/시설/연구/함선 건설을 마친 뒤, 공격형 성격 봇에 한해
`raid_tick()` 을 호출해 **한 틱에 함대 행동 1건**을 얹는다. 나머지 성격 봇은
이 모듈을 전혀 타지 않는다.

행동 우선순위(틱당 1건):
  1. 공격 페이로프: 최근 DB 정찰 보고서가 있고 방어 0 · 예상약탈 충분한 타겟 공격.
  2. 정찰: 서버 공개 공식으로 계산한 정찰기 수를 모은 뒤 완전 정보를 목표로 파견.
  3. 안전 폴백: DB/보고서/방어 상세가 없거나 오래됐으면 공격하지 않고 정찰 또는 대기.

설계 근거: docs/strategies.md §2 "레이더". 팀리드 지시(단순화 버전)에 맞춰
정찰 보고서 조회는 best-effort 로 하되, 실패하면 조용히 정찰/대기로 폴백해 루프를 깨지
않는다. compact HTML 의 Defense 표시는 실제 상세가 아니라 하드코딩된 0이므로 공격 판단에
절대 사용하지 않는다.

안전장치:
  - 사용자(사람) 계정 [1:44:5] 은 절대 공격/정찰하지 않는다(USER_EXCLUDE).
  - admin/banned/vacation/newbie/strong 플레이어는 후보에서 제외한다.
    (제외를 놓쳐도 fleet.send_fleet(check=True) 가 서버측에서 한 번 더 거른다.)
  - 진행 중인 공격 미션이 있으면 새 공격을 보내지 않는다(과공격 방지).
  - 같은 타겟 연속 타격 금지: 상태 파일에 정찰/공격 시각을 기록해 쿨다운을 둔다.
  - 모든 HTTP 조회는 예외를 삼키고 degrade 한다. raid_tick 은 절대 예외를 올리지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import pathlib
from collections.abc import Mapping

from bots.ai_agents import diplomacy
from bots.shared import fleet
from bots.shared import db_reader
from bots.rule_based import (
    attack_funnel,
    battle_cache,
    battle_guard,
    battle_sim,
    strategic_growth,
)
from bots.shared.game_objects import (
    NAME_TO_ID,
    SHIPS,
    DEFENSES,
    RESEARCH,
    LIGHT_FIGHTER,      # 204
    SMALL_CARGO,        # 202
    ESPIONAGE_PROBE,    # 210
)

HEAVY_FIGHTER = 205
LARGE_CARGO = 203
CRUISER = 206
BATTLESHIP = 207
BATTLECRUISER = 215
BOMBER = 211
DESTROYER = 213
DEATHSTAR = 214
REAPER = 218
RECYCLER = fleet.SHIP_RECYCLER              # 209
SOLAR_SATELLITE = fleet.SHIP_SOLAR_SATELLITE  # 212
CRAWLER = 217

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
STATE_DIR = pathlib.Path(__file__).resolve().parent / ".raider_state"

# 이웃 스캔 범위: 자기 시스템 s 를 중심으로 s-NEIGHBOR_SPAN .. s+NEIGHBOR_SPAN.
NEIGHBOR_SPAN = 2

# 사용자 요청(2026-07-13): 사람 계정도 시뮬레이션의 포식자/선택압 대상이다. 오래된
# 1:44 하드 제외를 제거한다. 필요하면 정책 targeting.excluded_systems로 다시 지정한다.
USER_EXCLUDE_SYSTEMS: set[tuple[int, int]] = set()

# 예상 약탈(자원합의 절반) 이 이 값 이상일 때만 공격할 가치가 있다고 본다.
# (v2: 여기에 "격침 시 데브리 가치"를 더한 종합 기댓값으로 판정한다 — 아래 데브리 설정 참고.)
MIN_EXPECTED_LOOT = 5000

# --- 데브리 기댓값 (위성 사냥 메타) ---------------------------------------
# 방어 0 타겟을 치면 그 행성에 방치된 함선(특히 태양광 위성)이 전멸하며 데브리가 된다.
# 위성은 방어력 0이라 소수 전투기로도 전멸시킬 수 있어, 자원이 적어도 데브리만으로
# 약탈 가치가 생긴다. 그래서 종합 기댓값 = 예상약탈 + (격침 데브리 가치 × 회수계수).
#
# 데브리 비율은 실서버 값 그대로다:
#   ships→debris 30% (SettingsService.debrisFieldFromShips 기본값),
#   defense→debris 0% (debrisFieldFromDefense), deuterium 데브리 off
#   (debrisFieldDeuteriumOn=0) → 메탈+크리스탈 단가에만 30% 적용.
# 서버 공식(BattleEngine.calculateDebris): 함선별 floor(단가 × 수량 × 30%).
DEBRIS_FROM_SHIPS_PCT = 0.30

# 함선별 데브리 산정용 (메탈, 크리스탈) 단가. 서버 CivilShipObjects.php 실측값.
#   solar_satellite(212): price(0, 2000, 500) → 데브리는 크리스탈 2000 에만 30%.
# 군용 함선은 의도적으로 제외(=목록에 없으면 0). 소수 정예 레이더(전투기 4기)가 절대
# 이길 수 없는 대형 함대를 데브리 미끼로 오판해 자살 공격하는 것을 막는 보수적 처리다.
# "무방비로 방치되는" 값싼 민간 함선(위성/화물/정찰기/크롤러)만 데브리 유인으로 센다.
SHIP_DEBRIS_COST: dict[int, tuple[int, int]] = {
    SMALL_CARGO: (2000, 2000),      # 202  소형화물
    LARGE_CARGO: (6000, 6000),      # 203  대형화물
    RECYCLER: (10000, 6000),        # 209  재활용선
    ESPIONAGE_PROBE: (0, 1000),     # 210  정찰기
    SOLAR_SATELLITE: (0, 2000),     # 212  태양광 위성 — 레이더 핵심 표적
    CRAWLER: (2000, 2000),          # 217  크롤러
}

# 기본 log/안전핀 폴백의 방어0 게이트가 허용하는 비전투 표적 함선.
# 군용함·식민선·파스파인더 등 이 목록 밖의 알려진 함선이 1기라도 있으면 공격하지 않는다.
SAFE_RAID_TARGET_SHIPS = frozenset(SHIP_DEBRIS_COST)

# 데브리는 리사이클러 수확 미션으로만 회수 가능하다. raid_tick 은 공격만 보내고 수확을
# 자동으로 잇지 않으므로, 리사이클러 미보유 레이더에겐 데브리 가치를 크게 할인한다
# (당장 못 줍는 자원이라 유인 효과만 약하게 남긴다). 리사이클러 보유 레이더는 회수 여력이
# 있다고 보고 전액 반영한다.
DEBRIS_NO_RECYCLER_FACTOR = 0.3

# --- 데브리 수거 / 리사이클러 커리큘럼 -------------------------------------
# 리사이클러(209) 선행조건. personality.REQUIREMENTS 에도 공용 큐 사전체크용으로
# 같은 서버 소스 실측값을 둥다. 이 목록은 데브리 추가 건조 경로가 독립적으로
# fail-closed 확인하는 지역 안전핀이다(app/GameObjects/CivilShipObjects.php:112).
# raider 커리큘럼은 research_lab 6 + laser 3 + shielding 3 + combustion 6을 순서대로
# 열도록 맞춰져 있다. 이 목록은 최종 서버측 함선 요구조건을 한 번 더 fail-closed 확인한다.
RECYCLER_REQUIREMENTS: list[tuple[int, int]] = [
    (21, 4),   # shipyard 4
    (115, 6),  # combustion_drive 6
    (110, 2),  # shielding_technology 2
]

# 리사이클러 1기 화물 용량(서버 CivilShipObjects.php GameObjectProperties capacity 인자).
# 파견 리사이클러 수 = ceil(데브리가치 / 이 값). 클래스/연구 보너스로 실제 용량이 더 크면
# 살짝 과다 파견될 뿐(무해)이라 기본값을 쓴다.
RECYCLER_CAPACITY = 20000

# 데브리 수거 가치가 있을 때 한 번에 큐잉하는 리사이클러 수(소량 — 커리큘럼 함선과 경합 최소).
RECYCLER_BUILD_BATCH = 2

# 데브리 필드 수거 대상 판정 임계(메탈+크리스탈). 이 값 미만이면 파견 안 함.
HARVEST_MIN_VALUE = 5000

# 데브리 탐색 범위(자기 시스템 기준 ±). 리사이클러는 느리므로 근처만 본다(이웃 스캔과 동일).
HARVEST_DEBRIS_SPAN = NEIGHBOR_SPAN

# 같은 데브리 필드 재파견 최소 간격(초). 수거는 필드를 고갈시키므로 곧바로 다시 보내지 않는다.
HARVEST_COOLDOWN_SEC = 1200

# 동시 진행 수거 미션 상한. 공격 슬롯과 경합하지 않게 1건으로 제한한다.
HARVEST_MAX_CONCURRENT = 1

# 수거를 보낸 뒤에도 공격/정찰용 여유 슬롯을 이만큼 남긴다. max_active=3이면 진행중 함대가
# 0~1건일 때만 수거를 새로 보내고, 2건이면 데브리가 커도 raid 슬롯을 굶기지 않는다.
HARVEST_RESERVED_RAID_SLOTS = 1

# 혼합 수확 함대가 화물선을 전부 가져가 다음 약탈/긴급 대피를 굶기지 않게 우선 보존하는
# 편성. 다만 주기장에 이 예약분만 남아도 사용자 계약상 최소 1대는 수확에 동행한다.
HARVEST_RESERVED_SMALL_CARGO = 3
HARVEST_RESERVED_LARGE_CARGO = 1

# 피격 예정 좌표의 리사이클러를 다른 데브리에 보내지 않고 ETA까지 보존한다. 전투 정산이
# 요청 시 지연될 수 있어 도착 뒤 이 시간 동안 자기 좌표 데브리를 기다린다.
COUNTER_HARVEST_GRACE_SEC = 1200

# 쿨다운(초). 실루프 간격이 30분이라, 한 바퀴 안에서 같은 타겟을 다시 안 건드리게 한다.
SPY_COOLDOWN_SEC = 1200
ATTACK_COOLDOWN_SEC = 1500

# 공격에 쓸 수 있는 정찰 보고서의 최대 나이. ×100 우주에서는 20분 안에도 방어가 빠르게
# 늘 수 있으므로 재정찰 쿨다운과 같은 보수적 창만 인정한다. 이보다 오래되면 defense 상세가
# 0으로 저장돼 있어도 공격 불가다.
SPY_REPORT_MAX_AGE_SEC = 1200

# OGameX createEspionageReport() 공식:
#   extra = max(0, defender_esp - attacker_esp)^2
#   remaining = probes - extra
#   연구 공개 = remaining >= 7 OR attacker_esp - 4 >= defender_esp
# 함선/방어/연구가 모두 있어야 battle_sim live 표본이 되므로 기본 7기부터 시작하고, 이전
# 시도가 부족했다면 기술차 제곱 경계(7 -> 8 -> 11 -> 16 -> 23 -> ...)로 올린다.
# 종전 12기 인공 상한은 높은 정탐 기술의 표적을 영원히 정찰만 하게 만들었으므로 두지 않는다.
ESPIONAGE_TECH = 106
SPY_PROBES_FULL_INTEL_BASE = 7

# 좌표별 20분 쿨다운과 별도로 계정 전체 정찰을 10분에 1건으로 제한한다. 종전에는 매 4분
# 틱마다 새 좌표를 골라 24시간 2,700건 이상을 만들었지만 완전 정보율은 1% 미만이었다.
SPY_GLOBAL_COOLDOWN_SEC = 600

# full autonomy에서도 내려갈 수 없는 집행 계층 안전선. 정책 JSON이 0/음수/과대
# 배싱 상한을 써도 아래 값으로 닫힌다.
HARD_MIN_SPY_GLOBAL_COOLDOWN_SEC = SPY_GLOBAL_COOLDOWN_SEC
HARD_MIN_SPY_TARGET_COOLDOWN_SEC = SPY_COOLDOWN_SEC
HARD_MIN_ATTACK_TARGET_COOLDOWN_SEC = ATTACK_COOLDOWN_SEC

# 같은 좌표에서 정찰 함대가 연속 소실되면 지수 백오프한다. 마지막 단계는 하루이며,
# 새 정책이 같은 타겟을 계속 우선해도 이 상태는 정책 파일과 독립적으로 유지된다.
SPY_LOSS_BACKOFF_SEC = (1800, 7200, 28800, 86400)
SPY_LOSS_LOOKBACK_SEC = 86400
SPY_LOSS_MATCH_WINDOW_SEC = 21600

# --- 공격 직전 안전 preflight (LIVE / kill switch) ----------------------------
# 실전대조에서 239~962초 된 정찰 보고서를 쓴 3건 모두, 공격 출발 전에
# 방어측 이동함대가 귀환했지만 lazy settlement로 보고서에 반영되지 않았다.
# 이 플래그를 켜도 같은 틱에서 polling하지 않는다: 1단계는 재정찰만 파견하고,
# 다음 틱에 requested_at 이후의 full-intel+최근24h 합법 정찰 peak envelope로
# 재시뮬한 뒤만 기존 방어0 공격을 허용한다. 실전 사이징과는 독립이며 fail-closed다.
RAID_ATTACK_PREFLIGHT_ENABLED = True
PREFLIGHT_BATTLE_GUARD_COHORT = "preflight_v2_dynamic_cap"
PREFLIGHT_REQUEST_TTL_SEC = 900
PREFLIGHT_HISTORY_WINDOW_HOURS = 24
# A full-intel report collected immediately before a preflight request remains a
# valid input. Reusing it avoids an endless spy loop when the follow-up report is
# delayed, while the normal fresh-report and launch-time revalidation gates remain.
PREFLIGHT_REUSE_FRESH_REPORT_SEC = 300
# 성장하는 우주에서 절대 함대가치 상한은 빠르게 낡는다. 위험은 절대 금액이 아니라
# 계정 전체 함대에서 잃을 수 있는 비율로 제한한다. 이후 가치/수량 상한을 추가할 때도
# "우주 성장에 따라 낡는가?"를 먼저 확인하고, 그렇다면 비율/동적 기준을 쓴다.
PREFLIGHT_FLEET_VALUE_RISK_PERCENT = 15
PREFLIGHT_MIN_FLEET_VALUE_LIMIT = 50_000
PREFLIGHT_MAX_ETA_SEC = 75
PREFLIGHT_COMBAT_SHIPS = frozenset({
    LIGHT_FIGHTER, HEAVY_FIGHTER, CRUISER, BATTLESHIP, BATTLECRUISER,
    BOMBER, DESTROYER, DEATHSTAR, REAPER,
})

# 기본 전투 시뮬은 관측 전용이다. 전역 상수를 live로 바꾸지 않고 raid_tick의 명시적
# battle_mode="canary_live" 인자만 단계 배포에 쓴다. 이 모드도 다행성+2슬롯 계정이 아니거나
# 안전핀이 트립됐으면 자동으로 log/기존 방어0 게이트로 폴백한다.
BATTLE_SIM_MODE = "log"
BATTLE_SIM_CANARY_MODE = "canary_live"
# 첫 단계는 이미 다행성·full-intel 표본이 있는 ai06 한 계정만. 모드를 명시하더라도 이
# allowlist 밖이면 BLOCKED/log 폴백한다. 확대는 관측 후 코드 리뷰로만 한다.
BATTLE_SIM_CANARY_EMAILS = frozenset({"ai06@bots.local"})

# 배싱 가드(오게임 배싱룰 모사). ×100 배속에선 25분 쿨다운만으론 한 채굴러가 하루에
# 수십 번 털려 영구 농장이 되고, 생태계가 레이더 독주로 조기 수렴한다. 그래서 실서버처럼
# 같은 좌표에 대한 공격을 24시간 롤링 윈도우 기준으로 상한을 둔다.
# (OGameX 서버는 bashingSystemLimitReached 를 항상 false 로 두어 배싱을 강제하지 않으므로
#  봇이 클라이언트에서 자율 제한한다.) 타임스탬프는 기존 쿨다운과 같은 실시간(time.time()) 기준.
BASH_WINDOW_SEC = 86400   # 24시간 롤링 윈도우
BASH_MAX_ATTACKS = 6
HARD_MAX_BASH_ATTACKS = BASH_MAX_ATTACKS

# 진행 중(귀환 아님) 자기 함대가 이 수 이상이면 이번 틱은 함대 행동을 쉰다.
MAX_ACTIVE_MISSIONS = 3

# 은하 스캔 공유 캐시 TTL(초). 레이더 23명이 매 틱 ±2 시스템(5페이지)을 각자 긁는 게
# 바퀴의 주요 부하라, 스캔 결과를 봇 공용 파일(STATE_DIR/scan_cache_<g>_<s>.json)에
# 캐시해 서로 재사용한다. 행성 배치는 거의 안 변하므로 30분이면 충분하다.
# 활동/비활성 최신성은 정찰 보고서(read_spy_reports)가 담당하므로 캐시로 문제없다.
SCAN_CACHE_TTL_SEC = 1800

# 스캔 캐시 적중/미스 카운터(검증·로그용). 프로세스 수명 동안 누적된다.
#   http : 실제 fleet.scan_galaxy HTTP 호출 횟수 (= miss)
#   hit  : 캐시 파일 재사용 횟수
scan_cache_stats: dict[str, int] = {"http": 0, "hit": 0, "miss": 0}

# 소형화물선 1기 적재량(OGameX 클래식 기본). 화물 규모 산정용 근사치.
SMALL_CARGO_CAPACITY = 5000
LARGE_CARGO_CAPACITY = 25000
DEFAULT_LOOT_PERCENT = 50
DISCOVERER_LOOT_PERCENT = 75
DISCOVERER_CLASS_ID = 3
_LOOT_PERCENT_CACHE: dict[str, int] = {}


def raid_loot_percent(email: str) -> int:
    """서버 캐릭터 클래스와 같은 공격 약탈률(Discoverer 75%, 나머지 50%)."""
    normalized = str(email).lower()
    cached = _LOOT_PERCENT_CACHE.get(normalized)
    if cached is not None:
        return cached
    percent = DEFAULT_LOOT_PERCENT
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute("SELECT character_class FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
        if row and int(row.get("character_class") or 0) == DISCOVERER_CLASS_ID:
            percent = DISCOVERER_LOOT_PERCENT
    except Exception:  # noqa: BLE001 - 불명이면 서버 기본 50%로 보수 계산
        pass
    _LOOT_PERCENT_CACHE[normalized] = percent
    return percent


def expected_raid_loot(report: Mapping, loot_percent: int) -> int:
    """정찰 자원에서 서버 클래스 약탈권을 정수로 계산한다."""
    return max(0, int(report.get("res_sum") or 0)) * max(
        0, min(100, int(loot_percent))
    ) // 100

# 사이징에서 전투 후보로 취급하는 함종. 데스스타는 기본 속도가 100이고
# battle_sim의 연료 근사 테이블에도 의도적으로 없어 후보에서 제외한다. 순양함 이상은
# 레이더 테크트리가 직접 건조하고, 폭격기/구축함/리퍼는 원정 등 다른 경로로 보유하게
# 되더라도 같은 사이징 엔진이 버리지 않도록 포함한다.
SIZING_COMBAT_SHIPS: tuple[int, ...] = (
    LIGHT_FIGHTER, HEAVY_FIGHTER, CRUISER, BATTLESHIP, BATTLECRUISER,
    BOMBER, DESTROYER, REAPER,
)

# 공격 시 데려갈 전투기 상한(소수 정예 — 손실 최소화). 방어 0 확인 타겟엔 이 정도면 충분.
# ※ 라이브 집행은 계속 이 상수(_attack_force)로만 편대를 만든다. 아래 SIZING_* 는 "방어
#   비례 사이징이라면 어떤 편대였을지"를 계산·로그만 하는 shadow 경로 전용이다(미집행).
ATTACK_FIGHTERS = 4

# --- 편대 사이징 (SHADOW / 로그 전용) 상수 --------------------------------
# 설계: docs/FLEET_SIZING_DESIGN.md. 라이브 4대 편대는 그대로 두고, 타겟 방어에 비례한
# 편대·순EV를 계산해 로그로만 남긴다(실전대조 통과 전까지 미집행).
# 2026-07-12 채점은 실전대조 N=57을 충족했지만, 정찰 뒤 방어측 이동함대가
# 복귀한 2건에서 예측손실0→실제248k가 나와 aggregate error 100% FAIL이다.
# 따라서 아래 후보가 아무리 좋아도 절대 fleet.attack에 연결하지 않는다. 공격 직전
# 재정찰→새 report id/created_at 확인→같은 스냅샷 재시뮬 상태머신이 먼저 필요하다.
#
# 상한: 1회 출격 ≤ **함종별** 보유 전투함대의 60~70%. 전량 출격을 금지하는 이유 —
#   (1) 몬테카를로 시뮬 ±오차로 편대가 통째로 전멸하는 걸 막는 마지막 안전핀,
#   (2) 집에 남긴 전투함대는 플릿세이브가 커버(전량 보내면 피습 시 방어 공백).
# 자본함 1척을 LF 1기와 같이 세면 스캔 그리드가 왜곡되므로, 실제 후보는
# 함종별 65% 수량 상한을 먼저 적용하고 건조가치 노출 예산으로 스캔한다.
SIZING_MAX_COMBAT_PERCENT = 65
SIZING_MAX_COMBAT_FRACTION = SIZING_MAX_COMBAT_PERCENT / 100
# 사이징은 2단계로 스캔한다. 6라운드 전투는 준결정적이라(전력이 문턱을 넘으면 승률이
# ~0→~1로 급변) 균등 그리드만으로는 80/90/95% 밴드가 한 점으로 뭉친다. 그래서
#   1단계(coarse): 상한(cap)까지 SIZING_SCAN_STEPS 균등 스캔 → 순EV 최대 pick + 전이 구간 위치,
#   2단계(fine)  : 전이 구간을 SIZING_FINE_STEPS 로 촘촘히·표본 늘려 재스캔 → 밴드 분해.
SIZING_SCAN_STEPS = 16
SIZING_FINE_STEPS = 8
# 몬테카를로 표본 수. coarse 는 가볍게, 밴드 분해용 fine 은 승률 granularity 를 위해 늘린다.
# 24면 80/90/95%가 20·22·23/24 로 서로 구분되고, 실측 최대(ai01 LF530)에서도 틱당 ~0.5s 내.
SIZING_SAMPLES = 12
SIZING_BAND_SAMPLES = 24
# 재현성 고정 시드 — 같은 intel이면 같은 티어 로그가 나오게 한다(틱 간 노이즈 억제).
SIZING_SEED = 1234
# 관측 로그로 뽑을 승률 밴드(≈80/90/95%). 각 밴드를 처음 넘는 최소 편대를 로그한다.
SIZING_WIN_BANDS = (0.80, 0.90, 0.95)

# Shadow sizing is observation-only and used to run on every eligible account tick.
# With 102 accounts that work dominated the universe critical path even though the
# result could not affect an attack.  Keep a deterministic, thread-order-independent
# sample instead: roughly 1/64 eligible account/tick/target tuples.  Live canary and
# preflight attack decisions never pass through this budget.
SHADOW_SAMPLE_DENOMINATOR = 64
SHADOW_SAMPLE_BUCKET_SEC = 240

_COORD_RE = re.compile(r"\[(\d+):(\d+):(\d+)\]")


# ---------------------------------------------------------------------------
# 자기 좌표
# ---------------------------------------------------------------------------
def own_coords(client) -> tuple[int, int, int] | None:
    """현재 행성 좌표 (galaxy, system, position). 조회 실패 시 None.

    읽기 전용 DB의 users.planet_current를 우선해 틱마다 /galaxy+
    /overview를 렌더링하는 비용을 없앰다. DB 장애 때만 기존 HTTP 파싱으로
    폴백한다.
    """
    email = getattr(client, "email", None)
    if email:
        try:
            planets = db_reader.list_planets_db(email)
            current = next((planet for planet in planets if planet.get("is_current")), None)
            if current is not None:
                return (
                    int(current["galaxy"]),
                    int(current["system"]),
                    int(current["position"]),
                )
        except Exception:  # noqa: BLE001 - DB 장애 때 기존 HTTP 폴백
            pass
    try:
        html = client.session.get(f"{client.base_url}/galaxy").text
        gm = re.search(r"var galaxy = (\d+)", html)
        sm = re.search(r"var system = (\d+)", html)
        if not (gm and sm):
            return None
        g, s = int(gm.group(1)), int(sm.group(1))
        pos = 0
        ov = client.overview_html()
        cm = _COORD_RE.search(ov)
        if cm:
            pos = int(cm.group(3))
        return g, s, pos
    except Exception:  # noqa: BLE001 - 좌표 조회 실패는 치명적이지 않음
        return None


# ---------------------------------------------------------------------------
# 상태 파일 (타겟 쿨다운)
# ---------------------------------------------------------------------------
def _state_path(email: str) -> pathlib.Path:
    return STATE_DIR / f"{email.replace('/', '_')}.json"


def _load_state(email: str) -> dict:
    f = _state_path(email)
    if f.exists():
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {"targets": {}}


def _save_state(email: str, state: dict) -> None:
    try:
        STATE_DIR.mkdir(exist_ok=True)
        _state_path(email).write_text(json.dumps(state))
    except OSError:
        pass  # 상태 저장 실패는 다음 틱 중복 정찰 정도의 손해뿐이라 무시


def _counter_harvest_reservation(state: dict) -> dict | None:
    """피격 뒤 자기 데브리 수거를 위해 저장한 예약 상태(형식 오류면 None)."""
    value = state.get("counter_harvest")
    if not isinstance(value, dict):
        return None
    coord = value.get("coord")
    if not isinstance(coord, list) or len(coord) != 3:
        return None
    try:
        return {
            "mission_id": int(value["mission_id"]),
            "coord": tuple(int(part) for part in coord),
            "arrival_ts": int(value["arrival_ts"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _set_counter_harvest_reservation(state: dict, attack: dict) -> None:
    """incoming 공격의 좌표/ETA를 다음 틱까지 보존한다."""
    state["counter_harvest"] = {
        "mission_id": int(attack["mission_id"]),
        "coord": [int(part) for part in attack["coord"]],
        "arrival_ts": int(attack["arrival_ts"]),
    }


def _clear_counter_harvest_reservation(state: dict) -> None:
    state.pop("counter_harvest", None)


def _mark(state: dict, coord: tuple[int, int, int], kind: str, now: float) -> None:
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    ent = state.setdefault("targets", {}).setdefault(key, {})
    ent[kind] = now


def _mark_spy(
    state: dict, coord: tuple[int, int, int], now: float, probes: int,
) -> None:
    """좌표/계정 쿨다운과 다음 동적 증량에 필요한 직전 정찰기 수를 함께 기록."""
    _mark(state, coord, "spy", now)
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    target = state.setdefault("targets", {}).setdefault(key, {})
    target["spy_probes"] = int(probes)
    spy_times = target.get("spies")
    if not isinstance(spy_times, list):
        spy_times = []
    spy_times.append(float(now))
    target["spies"] = [
        float(item) for item in spy_times
        if isinstance(item, (int, float)) and now - float(item) < BASH_WINDOW_SEC
    ]
    state["last_spy"] = float(now)


def _last(state: dict, coord: tuple[int, int, int], kind: str) -> float:
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    return float(state.get("targets", {}).get(key, {}).get(kind, 0.0))


def _last_spy_probes(state: dict, coord: tuple[int, int, int]) -> int:
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    try:
        return max(0, int(state.get("targets", {}).get(key, {}).get("spy_probes", 0)))
    except (TypeError, ValueError):
        return 0


def _spy_times(state: dict, coord: tuple[int, int, int]) -> list[float]:
    value = _target_state(state, coord).get("spies")
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _spy_quarantined(state: dict, coord: tuple[int, int, int], now: float) -> bool:
    try:
        blocked_until = float(_target_state(state, coord).get("spy_block_until", 0))
    except (TypeError, ValueError):
        blocked_until = 0
    return now < blocked_until


def _apply_spy_loss(
    state: dict,
    coord: tuple[int, int, int],
    event_ts: float,
    *,
    message_id: int | None = None,
) -> int:
    """Record one confirmed/probable probe-fleet loss and return its backoff.

    Message IDs make replay idempotent. The rolling counter decays after a day so
    an old hostile target does not remain permanently banned.
    """
    target = _target_state(state, coord)
    processed = target.get("spy_loss_message_ids")
    if not isinstance(processed, list):
        processed = []
    if message_id is not None and int(message_id) in processed:
        try:
            return max(0, int(float(target.get("spy_block_until", 0)) - event_ts))
        except (TypeError, ValueError):
            return 0

    events = target.get("spy_losses")
    if not isinstance(events, list):
        events = []
    recent: list[float] = []
    for item in events:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if event_ts - value < SPY_LOSS_LOOKBACK_SEC:
            recent.append(value)
    recent.append(float(event_ts))
    target["spy_losses"] = recent[-16:]
    index = min(len(recent) - 1, len(SPY_LOSS_BACKOFF_SEC) - 1)
    backoff = int(SPY_LOSS_BACKOFF_SEC[index])
    target["spy_block_until"] = max(
        float(target.get("spy_block_until", 0) or 0),
        float(event_ts) + backoff,
    )
    if message_id is not None:
        processed.append(int(message_id))
        target["spy_loss_message_ids"] = processed[-32:]
    return backoff


def _coordinates_from_message_params(raw) -> tuple[int, int, int] | None:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(value, Mapping):
        return None
    text = str(value.get("coordinates") or "")
    match = re.search(r"(\d+):(\d+):(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _sync_spy_loss_messages(email: str, state: dict, now: float) -> int:
    """Best-effort map recent fleet-lost messages to this account's spy sends.

    OGameX emits a normal espionage report even when every probe is destroyed and
    separately emits ``fleet_lost_contact`` without a mission type. We therefore
    attribute a loss only when the same coordinate has a newer spy dispatch than
    attack dispatch within a bounded window. Ambiguous attack losses are ignored.
    """
    cursor = int(state.get("last_loss_message_id", 0) or 0)
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, m.params, UNIX_TIMESTAMP(m.created_at) event_ts "
                "FROM messages m JOIN users u ON u.id=m.user_id "
                "WHERE u.email=%s AND m.key='fleet_lost_contact' "
                "AND m.id>%s AND m.created_at>=DATE_SUB(NOW(), INTERVAL 1 DAY) "
                "ORDER BY m.id",
                (email, cursor),
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - loss learning cannot stop the raid tick
        return 0

    applied = 0
    for row in rows:
        try:
            message_id = int(row["id"])
            event_ts = float(row["event_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        state["last_loss_message_id"] = max(
            int(state.get("last_loss_message_id", 0) or 0), message_id
        )
        coord = _coordinates_from_message_params(row.get("params"))
        if coord is None:
            continue
        last_spy = _last(state, coord, "spy")
        last_attack = _last(state, coord, "attack")
        if (
            last_spy <= last_attack
            or event_ts < last_spy
            or event_ts - last_spy > SPY_LOSS_MATCH_WINDOW_SEC
        ):
            continue
        _apply_spy_loss(state, coord, event_ts, message_id=message_id)
        applied += 1
    if rows:
        _save_state(email, state)
    return applied


def _target_state(state: dict, coord: tuple[int, int, int]) -> dict:
    """좌표별 persistent 상태 엔트리(없으면 생성)."""
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    return state.setdefault("targets", {}).setdefault(key, {})


def _preflight_request(state: dict, coord: tuple[int, int, int]) -> dict | None:
    """좌표의 미완료 preflight 정찰 요청 상태."""
    value = _target_state(state, coord).get("attack_preflight")
    return value if isinstance(value, dict) else None


def _set_preflight_request(
    state: dict, coord: tuple[int, int, int], *, requested_at: float,
    report_id_before: int | None, probes: int,
) -> None:
    """재정찰 출발 스냅샷을 다음 틱 소비용으로 저장."""
    _target_state(state, coord)["attack_preflight"] = {
        "requested_at": float(requested_at),
        "report_id_before": report_id_before,
        "probes": int(probes),
    }


def _clear_preflight_request(state: dict, coord: tuple[int, int, int]) -> None:
    """좌표의 preflight 요청을 소비/폐기."""
    _target_state(state, coord).pop("attack_preflight", None)


def _attack_times(state: dict, coord: tuple[int, int, int]) -> list[float]:
    """해당 좌표의 공격 타임스탬프 목록(배싱 가드용). 없으면 빈 리스트."""
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    ts = state.get("targets", {}).get(key, {}).get("attacks")
    if not isinstance(ts, list):
        return []
    out: list[float] = []
    for t in ts:
        try:
            out.append(float(t))
        except (TypeError, ValueError):
            continue
    return out


def _player_attack_times(state: dict, player_id: int | None) -> list[float]:
    if player_id is None:
        return []
    raw = state.get("players", {}).get(str(int(player_id)), {}).get("attacks")
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _record_attack(
    state: dict,
    coord: tuple[int, int, int],
    now: float,
    *,
    player_id: int | None = None,
) -> None:
    """공격 성공 기록. 두 가지를 함께 갱신한다.
      - "attack": 마지막 공격 시각(25분 재타격 쿨다운용, _last 가 읽음).
      - "attacks": 공격 타임스탬프 목록(배싱 가드용). 24h 지난 항목은 잘라내
        상태 파일이 무한정 커지지 않게 한다.
    """
    key = f"{coord[0]}:{coord[1]}:{coord[2]}"
    ent = state.setdefault("targets", {}).setdefault(key, {})
    ent["attack"] = now
    times = _attack_times(state, coord)
    times.append(now)
    ent["attacks"] = [t for t in times if now - t < BASH_WINDOW_SEC]
    if player_id is not None:
        player = state.setdefault("players", {}).setdefault(str(int(player_id)), {})
        player_times = _player_attack_times(state, player_id)
        player_times.append(now)
        player["attacks"] = [
            item for item in player_times if now - item < BASH_WINDOW_SEC
        ]


def _bashing_limited(
    state: dict,
    coord: tuple[int, int, int],
    now: float,
    max_attacks: int = BASH_MAX_ATTACKS,
    *,
    player_id: int | None = None,
) -> bool:
    """24h 롤링 윈도우 내 공격 횟수가 상한(BASH_MAX_ATTACKS) 이상이면 True.

    True 인 타겟은 이번 바퀴 후보에서 제외해 영구 농장이 되는 것을 막는다.
    """
    hard_cap = min(HARD_MAX_BASH_ATTACKS, max(1, int(max_attacks)))
    coordinate_recent = [
        t for t in _attack_times(state, coord) if now - t < BASH_WINDOW_SEC
    ]
    if len(coordinate_recent) >= hard_cap:
        return True
    if player_id is None:
        return False
    player_recent = [
        t for t in _player_attack_times(state, player_id)
        if now - t < BASH_WINDOW_SEC
    ]
    return len(player_recent) >= hard_cap


# ---------------------------------------------------------------------------
# 정찰 보고서 (DB 직독)
# ---------------------------------------------------------------------------
def _decode_json_container(raw):
    """DB JSON 값을 dict 로 풀되 NULL/손상은 None, Laravel 빈 배열([])은 {}로 보존."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, dict):
        return raw
    # PHP 의 빈 associative array 는 JSON [] 로 저장된다. 이는 "공개됐고 0기"다.
    if isinstance(raw, list) and not raw:
        return {}
    return None


def _nonnegative_int(value) -> int | None:
    """JSON 정수값을 손실 없이 non-negative int 로 정규화. bool/소수/음수는 거부."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _parse_resources_json(raw) -> dict[str, int] | None:
    """resources JSON을 파싱. 약탈 3자원 중 하나라도 없거나 손상되면 미상(None)."""
    data = _decode_json_container(raw)
    if data is None:
        return None
    out: dict[str, int] = {}
    for key in ("metal", "crystal", "deuterium"):
        value = _nonnegative_int(data.get(key))
        if value is None:
            return None
        out[key] = value
    energy = _nonnegative_int(data.get("energy"))
    if energy is not None:
        out["energy"] = energy
    return out


def _parse_named_counts(raw, allowed_ids: set[int]) -> dict[int, int] | None:
    """machine_name→수량/레벨 JSON을 id→값으로 변환하며 미상과 known-empty를 구분.

    NULL/손상/알 수 없는 양수 유닛은 None 이다. 빈 []/{}는 실제로 상세가 공개됐지만 해당
    유닛이 하나도 없다는 뜻이므로 {}를 반환한다. 모르는 양수 항목을 조용히 버리면 방어를
    0으로 오판할 수 있어 전체 필드를 미상 처리한다.
    """
    data = _decode_json_container(raw)
    if data is None:
        return None
    out: dict[int, int] = {}
    for name, amount in data.items():
        value = _nonnegative_int(amount)
        if value is None:
            return None
        if value == 0:
            continue
        tid = NAME_TO_ID.get(name)
        if tid is None or tid not in allowed_ids:
            return None
        out[tid] = value
    return out


def _parse_ships_json(raw) -> dict[int, int] | None:
    """espionage_reports.ships: NULL(미상)과 []/{}(known zero)를 보존해 변환."""
    return _parse_named_counts(raw, set(SHIPS))


def _parse_defense_json(raw) -> dict[int, int] | None:
    """espionage_reports.defense: NULL(미상)과 []/{}(known zero)를 보존해 변환."""
    return _parse_named_counts(raw, set(DEFENSES))


def _parse_research_json(raw) -> dict[int, int] | None:
    """espionage_reports.research를 전투 시뮬 기술 id→레벨로 변환."""
    return _parse_named_counts(raw, set(RESEARCH))


def _spy_report_email(subject) -> str | None:
    """read_spy_reports 구 호출부(client)와 새 호출부(email)를 함께 지원."""
    if isinstance(subject, str):
        return subject
    email = getattr(subject, "email", None)
    return email if isinstance(email, str) and email else None


def read_spy_reports(
    subject, *, max_age_sec: int = SPY_REPORT_MAX_AGE_SEC,
) -> dict[tuple[int, int, int], dict]:
    """계정이 소유한 좌표별 최신 정찰 보고서를 DB에서 정확히 읽는다.

    compact HTML 템플릿의 Defense 값은 실제 보고서 상세와 무관하게 0으로 렌더되는 스텁이라
    사용하지 않는다. messages.user_id(보고서 수신자)로 계정을 묶고 최신순에서 좌표당 첫
    행만 택한다. **최신 행의 defense가 NULL이면 이전 known-defense 행으로 되돌아가지 않는다.**

    반환 필드:
      resources      실제 공개된 자원 dict 또는 None
      res_sum        metal+crystal+deuterium 또는 None
      ships          공개된 {ship_id: 수량}, known-zero {}, 미상 None
      defense_units  공개된 {defense_id: 수량}, known-zero {}, 미상 None
      defense        공개된 방어 총수, known-zero 0, 미상 None (구 호출부 호환)
      research       공개된 {research_id: 레벨}, known-zero {}, 미상 None
      age_sec/fresh  DB 시계 기준 보고서 나이와 공격 신선도

    DB/JSON 실패 시 빈 dict 또는 해당 상세 None 으로 안전하게 degrade 한다.
    """
    email = _spy_report_email(subject)
    if not email:
        return {}
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT er.id report_id, er.planet_galaxy g, er.planet_system s, "
                "er.planet_position p, er.resources resources, er.ships ships, "
                "er.defense defense, er.research research, er.created_at created_at, "
                "UNIX_TIMESTAMP(er.created_at) created_at_ts, "
                "TIMESTAMPDIFF(SECOND, er.created_at, NOW()) age_sec "
                "FROM messages m "
                "JOIN espionage_reports er ON er.id = m.espionage_report_id "
                "JOIN users u ON u.id = m.user_id "
                "WHERE u.email = %s AND m.espionage_report_id IS NOT NULL "
                "ORDER BY er.created_at DESC, er.id DESC",
                (email,),
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - DB 실패는 공격 불가 + 정찰/대기로 흡수
        return {}

    reports: dict[tuple[int, int, int], dict] = {}
    for row in rows:
        try:
            coord = (int(row["g"]), int(row["s"]), int(row["p"]))
        except (KeyError, TypeError, ValueError):
            continue
        if coord in reports:
            continue  # 최신 행이 미상이더라도 과거 상세로 되돌아가지 않는다.

        resources = _parse_resources_json(row.get("resources"))
        ships = _parse_ships_json(row.get("ships"))
        defenses = _parse_defense_json(row.get("defense"))
        research = _parse_research_json(row.get("research"))
        try:
            age_sec = int(row["age_sec"])
        except (KeyError, TypeError, ValueError):
            age_sec = None
        try:
            created_at_ts = float(row["created_at_ts"])
        except (KeyError, TypeError, ValueError):
            # 구 테스트/여분 DB 드라이버에서 필드가 없어도 age로 복원한다.
            created_at_ts = time.time() - age_sec if age_sec is not None else None
        fresh = age_sec is not None and 0 <= age_sec <= max(0, int(max_age_sec))
        reports[coord] = {
            "report_id": row.get("report_id"),
            "created_at": row.get("created_at"),
            "created_at_ts": created_at_ts,
            "age_sec": age_sec,
            "fresh": fresh,
            "resources": resources,
            "res_sum": sum(resources[k] for k in ("metal", "crystal", "deuterium"))
            if resources is not None else None,
            "ships": ships,
            "defense_units": defenses,
            "defense": sum(defenses.values()) if defenses is not None else None,
            "research": research,
        }
    return reports


def read_spy_combat_peak(
    email: str, coord: tuple[int, int, int],
) -> dict[int, int] | None:
    """공격자 본인의 최근 24h **합법 정찰 보고**에서 함종별 최대를 읽는다.

    이동함대가 행성에 돌아왔지만 lazy settlement 지연 때문에 방금 받은 보고에
    아직 나타나지 않는 TOCTOU를 압력 테스트하는 envelope다. messages.user_id가
    해당 레이더인 espionage_reports만 쓴다. planets/현재 타겟 함대 직독은 없다.

    반환:
      - ``dict``: 관측된 군용 함종별 componentwise peak(이력 없으면 {}).
      - ``None``: DB/손상 오류. preflight는 이를 fail-closed로 차단한다.
    """
    try:
        galaxy, system, position = (int(coord[0]), int(coord[1]), int(coord[2]))
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT er.ships ships FROM messages m "
                "JOIN espionage_reports er ON er.id = m.espionage_report_id "
                "JOIN users u ON u.id = m.user_id "
                "WHERE u.email = %s AND m.espionage_report_id IS NOT NULL "
                "AND er.planet_galaxy = %s AND er.planet_system = %s "
                "AND er.planet_position = %s "
                f"AND er.created_at >= DATE_SUB(NOW(), INTERVAL "
                f"{int(PREFLIGHT_HISTORY_WINDOW_HOURS)} HOUR)",
                (email, galaxy, system, position),
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - envelope 실패는 공격 fail-closed
        return None

    peak: dict[int, int] = {}
    for row in rows:
        raw = row.get("ships") if isinstance(row, dict) else None
        if raw is None:
            continue  # 함선 정보 미공개 보고서는 envelope 증거가 아님.
        parsed = _parse_ships_json(raw)
        if parsed is None:
            return None  # non-NULL 손상 JSON을 빈 함대로 간주하지 않음.
        for ship_id, amount in parsed.items():
            if ship_id in PREFLIGHT_COMBAT_SHIPS:
                peak[ship_id] = max(peak.get(ship_id, 0), int(amount))
    return peak


def read_spy_ships(email: str) -> dict[tuple[int, int, int], dict[int, int]]:
    """구 API 호환 래퍼. 최신 보고서에서 실제 공개된 함선 상세만 반환."""
    return {
        coord: report["ships"]
        for coord, report in read_spy_reports(email).items()
        if report.get("ships") is not None
    }


def desired_spy_probes(
    report: dict | None,
    attacker_espionage: int,
    previous_sent: int = 0,
) -> int:
    """서버 공개 공식으로 full-intel에 필요한 다음 정찰기 수를 계산한다.

    상대 연구가 이미 공개됐으면 정확한 기술차 제곱 공식을 쓴다. 아직 미상이면 직전 시도
    수를 기준으로 가능한 기술차 경계 7→8→11→16→23…을 한 단계씩 올린다. 반환값은 전략
    목표 수량이며 실제 보유량으로 자르거나 인공 상한을 두지 않는다. 호출부는 이 수를
    모으기 전에는 파견하지 않아 저정보 정찰/역정찰 전투를 반복하지 않는다.
    """
    try:
        attacker = max(0, int(attacker_espionage))
    except (TypeError, ValueError):
        attacker = 0

    research = report.get("research") if isinstance(report, dict) else None
    if isinstance(research, dict):
        try:
            defender = max(0, int(research.get(ESPIONAGE_TECH, 0)))
        except (TypeError, ValueError):
            defender = 0
        # 기술 우위만으로 연구가 공개되는 서버 canRevealData의 두 번째 조건.
        if attacker - 4 >= defender:
            return 1
        difference = max(0, defender - attacker)
        return difference * difference + SPY_PROBES_FULL_INTEL_BASE

    try:
        previous = max(0, int(previous_sent))
    except (TypeError, ValueError):
        previous = 0
    if previous < SPY_PROBES_FULL_INTEL_BASE:
        return SPY_PROBES_FULL_INTEL_BASE
    difference = 1
    while difference * difference + SPY_PROBES_FULL_INTEL_BASE <= previous:
        difference += 1
    return difference * difference + SPY_PROBES_FULL_INTEL_BASE


def ship_debris_value(ships: dict[int, int]) -> int:
    """격침 시 생성되는 데브리(메탈+크리스탈) 가치. 서버 calculateDebris 공식 복제.

    함선별 floor(단가 × 수량 × 30%) 합. 듀테륨 데브리 off, 방어 데브리 0.
    SHIP_DEBRIS_COST 에 없는(군용) 함선은 0 으로 본다(보수적 — 위 상수 주석 참고).
    """
    total = 0
    for tid, amt in ships.items():
        cost = SHIP_DEBRIS_COST.get(tid)
        if not cost:
            continue
        metal, crystal = cost
        total += int(metal * amt * DEBRIS_FROM_SHIPS_PCT)
        total += int(crystal * amt * DEBRIS_FROM_SHIPS_PCT)
    return total


def all_ship_debris_value(ships: dict[int, int]) -> int:
    """full-intel 전투 후보용 상대 **전체 함선** 최대 데브리 가치.

    기존 ship_debris_value는 방어0 레거시 게이트가 군함을 안전한 민간함으로 오인하지 않게
    일부 함종만 센다. 이 함수는 전투 시뮬을 통과해야 하는 방어 표적의 사전 가치 필터에서
    군함까지 모두 세며, 최종 EV는 battle_sim이 양측 실제 예상 격파분으로 다시 계산한다.
    """
    total = 0
    for raw_tid, raw_amount in ships.items():
        try:
            tid, amount = int(raw_tid), max(0, int(raw_amount))
        except (TypeError, ValueError):
            continue
        cost = strategic_growth.SHIP_COST.get(tid)
        if cost is None:
            continue
        total += int((cost[0] + cost[1]) * amount * DEBRIS_FROM_SHIPS_PCT)
    return total


# ---------------------------------------------------------------------------
# 타겟 선정
# ---------------------------------------------------------------------------
def _is_biddable(
    row: dict,
    *,
    allow_strong_targets: bool = True,
    excluded_systems: set[tuple[int, int]] | None = None,
) -> bool:
    """스캔 행이 공격/정찰 후보로 적합한지(사람·보호계정 제외).

    newbie 는 제외하지 않는다: OGameX 는 강자→신참 공격만 막고(AttackMission 은
    본인/휴가/관리자(Legor) 행성만 거부), 비슷한 규모의 봇끼리 교전은 정상 허용된다.
    strong(대형 플레이어)만 리스크가 커서 회피한다.
    """
    g, s = row.get("galaxy"), row.get("system")
    protected = USER_EXCLUDE_SYSTEMS if excluded_systems is None else excluded_systems
    if (g, s) in protected:
        return False
    if row.get("admin") or row.get("banned") or row.get("vacation"):
        return False
    if row.get("strong") and not allow_strong_targets:
        return False
    if row.get("player_id") in (None, fleet._DEEP_SPACE_PLAYER_ID):
        return False
    if not row.get("position"):
        return False
    return True


def _activity_minutes(row: dict) -> int:
    """galaxy 행의 activity dict 에서 '몇 분 전 활동' 근사치. 최근 활동 표시 없으면 큰 값.

    scan_galaxy 의 activity 는 {'showActivity': int|False, ...} 형태다.
    showActivity 가 int 면 그만큼 분 전 활동(작을수록 최근=온라인=위험).
    False 면 최근(1시간 내) 활동 없음 → 안전한 편이므로 큰 값으로 취급한다.
    """
    act = row.get("activity")
    if isinstance(act, dict):
        sa = act.get("showActivity")
        if isinstance(sa, int) and not isinstance(sa, bool):
            return sa
    return 10 ** 6


# ---------------------------------------------------------------------------
# 은하 스캔 공유 캐시 (봇 간 파일 캐시)
# ---------------------------------------------------------------------------
def _scan_cache_path(galaxy: int, system: int) -> pathlib.Path:
    """봇 공용 스캔 캐시 파일 경로. email 이 아닌 (galaxy, system) 기준이라
    모든 레이더가 같은 파일을 공유한다."""
    return STATE_DIR / f"scan_cache_{galaxy}_{system}.json"


def _write_scan_cache(path: pathlib.Path, rows: list[dict]) -> None:
    """스캔 결과를 원자적으로 저장(temp → os.replace).

    여러 레이더가 스레드/프로세스로 동시에 같은 파일을 쓸 수 있으므로, 반쯤 쓰인
    파일을 읽는 것을 막기 위해 임시 파일에 쓴 뒤 원자적으로 교체한다. temp 이름에
    pid+thread id 를 넣어 동시 writer 간 충돌을 피한다. 저장 실패는 무시한다
    (다음 틱에 재스캔하는 손해뿐)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        STATE_DIR.mkdir(exist_ok=True)
        tmp.write_text(json.dumps(rows))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def cached_scan_galaxy(
    client, galaxy: int, system: int, now: float | None = None
) -> list[dict]:
    """fleet.scan_galaxy 의 봇 공용 파일 캐시 래퍼.

    - 캐시 파일이 TTL(SCAN_CACHE_TTL_SEC) 안이면 그대로 반환(HTTP 스캔 생략, hit++).
    - 없거나 오래됐거나 손상됐으면 실제 scan_galaxy 를 호출(http++/miss++)하고 저장한다.
    scan_galaxy 실패(예외)는 그대로 전파한다 — 호출부(scan_targets)가 시스템 단위로 흡수한다.
    """
    now = time.time() if now is None else now
    path = _scan_cache_path(galaxy, system)
    try:
        if now - path.stat().st_mtime < SCAN_CACHE_TTL_SEC:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                scan_cache_stats["hit"] += 1
                return data
    except (OSError, ValueError):
        pass  # 캐시 없음/손상/파싱 실패 → 실제 스캔으로

    rows = fleet.scan_galaxy(client, galaxy, system)
    scan_cache_stats["http"] += 1
    scan_cache_stats["miss"] += 1
    _write_scan_cache(path, rows)
    return rows


def scan_targets(
    client,
    galaxy: int,
    system: int,
    own: tuple[int, int, int],
    *,
    span: int = NEIGHBOR_SPAN,
    allow_strong_targets: bool = True,
    excluded_systems: set[tuple[int, int]] | None = None,
    priority_targets: set[tuple[int, int, int]] | None = None,
) -> list[dict]:
    """이웃 시스템을 스캔해 공격/정찰 후보를 반환. 비활성 우선 정렬.

    각 항목: scan_galaxy 행 + coord 튜플. 자기 행성/보호계정은 제외.
    스캔 실패 시스템은 조용히 건너뛴다.
    """
    out: list[dict] = []
    scan_span = max(0, int(span))
    systems = set(range(system - scan_span, system + scan_span + 1))
    # 사용자/실험 표적은 전체 은하를 499페이지 스캔하지 않고 그 시스템만 추가 조회한다.
    systems.update(
        coord[1] for coord in (priority_targets or set()) if coord[0] == galaxy
    )
    for s in sorted(systems):
        if s < 1 or s > 499:
            continue
        try:
            rows = cached_scan_galaxy(client, galaxy, s)
        except Exception:  # noqa: BLE001 - 시스템 스캔 실패는 건너뛴다
            continue
        for row in rows:
            coord = (row["galaxy"], row["system"], row["position"])
            if coord == own:
                continue  # 자기 행성
            if not _is_biddable(
                row,
                allow_strong_targets=allow_strong_targets,
                excluded_systems=excluded_systems,
            ):
                continue
            row["coord"] = coord
            out.append(row)
    # 비활성(장기 우선) → 활성 순. 같은 등급이면 최근 활동이 오래된(안전한) 쪽 우선.
    def _rank(r: dict) -> tuple:
        inactivity = 2 if r.get("long_inactive") else (1 if r.get("inactive") else 0)
        return (-inactivity, -_activity_minutes(r))
    out.sort(key=_rank)
    return out


def _prioritize_targets(
    candidates: list[dict],
    state: dict,
    priority_targets: set[tuple[int, int, int]],
    email: str | None = None,
    now: float | None = None,
) -> list[dict]:
    """Rotate targets by saturation, inactivity and an account-specific tie break.

    The stable hash prevents 12 agents that observe the same galaxy page from all
    choosing its first row. Recent spy/attack counts dominate the hash, so this is
    target rotation rather than random wandering.
    """
    current = time.time() if now is None else float(now)

    def _recent_count(values: list[float]) -> int:
        return sum(1 for value in values if current - value < BASH_WINDOW_SEC)

    def _jitter(coord: tuple[int, int, int]) -> int:
        identity = (email or "anonymous").lower()
        raw = f"{identity}|{coord[0]}:{coord[1]}:{coord[2]}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")

    def _key(row: dict) -> tuple:
        coord = row["coord"]
        player_id = _row_player_id(row)
        attacks = (
            _recent_count(_player_attack_times(state, player_id))
            if player_id is not None
            else _recent_count(_attack_times(state, coord))
        )
        spies = _recent_count(_spy_times(state, coord))
        inactivity = 2 if row.get("long_inactive") else (
            1 if row.get("inactive") else 0
        )
        return (
            0 if coord in priority_targets else 1,
            1 if _spy_quarantined(state, coord, current) else 0,
            attacks,
            spies,
            -inactivity,
            _last(state, coord, "spy"),
            _jitter(coord),
        )

    return sorted(candidates, key=_key)


def _row_player_id(row: Mapping) -> int | None:
    value = row.get("player_id")
    if isinstance(value, bool):
        return None
    try:
        player_id = int(value)
    except (TypeError, ValueError):
        return None
    return player_id if player_id > 0 else None


# ---------------------------------------------------------------------------
# 진행 중 함대
# ---------------------------------------------------------------------------
def _active_fleet_info(client) -> tuple[int, bool, int]:
    """(진행중 비귀환 미션 수, 공격 미션 진행중 여부, 진행중 수거(type8) 미션 수).

    읽기 전용 DB로 출발 레그(parent_id IS NULL)를 우선 집계한다. 이는
    eventlist의 return_flight 행을 건너뛰던 기존 의미와 같고, eventbox+
    eventlist HTML/JSON 요청을 매 계정 틱에서 제거한다. DB 장애 때만 HTTP
    폴백. 두 경로 모두 실패하면 기존처럼 (0, False, 0).
    """
    email = getattr(client, "email", None)
    if email:
        try:
            conn = db_reader._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) active, "
                    "COALESCE(SUM(fm.mission_type=%s),0) attacking, "
                    "COALESCE(SUM(fm.mission_type=%s),0) harvesting "
                    "FROM fleet_missions fm JOIN users u ON u.id=fm.user_id "
                    "WHERE u.email=%s AND fm.processed=0 AND fm.canceled=0 "
                    "AND fm.parent_id IS NULL AND fm.mission_type<>%s",
                    (
                        fleet.MISSION_ATTACK,
                        fleet.MISSION_RECYCLE,
                        email,
                        fleet.MISSION_MISSILE,
                    ),
                )
                row = cur.fetchone()
            if row is not None:
                return (
                    int(row.get("active") or 0),
                    int(row.get("attacking") or 0) > 0,
                    int(row.get("harvesting") or 0),
                )
        except Exception:  # noqa: BLE001 - DB 장애 때 HTTP 폴백
            pass
    try:
        ev = fleet.fleet_events(client)
    except Exception:  # noqa: BLE001
        return 0, False, 0
    active = 0
    attacking = False
    harvesting = 0
    for e in ev.get("events", []):
        if e.get("return_flight"):
            continue
        active += 1
        mt = e.get("mission_type")
        if mt == fleet.MISSION_ATTACK:
            attacking = True
        elif mt == fleet.MISSION_RECYCLE:
            harvesting += 1
    return active, attacking, harvesting


def _harvest_slot_safe(
    active: int,
    max_active: int,
    reserved_raid_slots: int = HARVEST_RESERVED_RAID_SLOTS,
) -> bool:
    """수거 1건 파견 뒤에도 raid 예약 슬롯이 남는지."""
    return int(active) + 1 <= int(max_active) - max(0, int(reserved_raid_slots))


def _harvest_concurrency_limit(configured: int, max_active: int) -> int:
    """정책값 0은 금지가 아니라 서버 함대 슬롯까지의 인공 상한 해제다."""
    configured = max(0, int(configured))
    return max(0, int(max_active)) if configured == 0 else configured


def incoming_attack_to_coord(
    email: str, coord: tuple[int, int, int], *, now: float | None = None,
) -> dict | None:
    """현재 좌표로 오는 가장 빠른 적 공격과 ETA를 읽는다.

    반환은 ``mission_id/coord/arrival_ts/eta_sec``. 취소·처리된 공격과 자기 함대는 제외한다.
    DB 실패는 ``None``으로 내려 기존 레이더 행동을 깨지 않는다(플릿세이브 쪽 위협 조회는
    별도로 fail-closed 한다).
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fm.id mission_id, fm.time_arrival arrival_ts, "
                "fm.galaxy_to g, fm.system_to s, fm.position_to p "
                "FROM fleet_missions fm "
                "JOIN planets p ON p.id=fm.planet_id_to "
                "JOIN users u ON u.id=p.user_id "
                "WHERE u.email=%s AND fm.user_id<>u.id "
                "AND fm.processed=0 AND fm.canceled=0 AND fm.mission_type=1 "
                "AND fm.galaxy_to=%s AND fm.system_to=%s AND fm.position_to=%s "
                "ORDER BY fm.time_arrival, fm.id LIMIT 1",
                (email, int(coord[0]), int(coord[1]), int(coord[2])),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - 공격 엔진은 DB 장애 시 기존 안전게이트 유지
        return None
    if not row:
        return None
    try:
        arrival = int(row["arrival_ts"])
        target = (int(row["g"]), int(row["s"]), int(row["p"]))
        mission_id = int(row["mission_id"])
    except (KeyError, TypeError, ValueError):
        return None
    current = time.time() if now is None else float(now)
    return {
        "mission_id": mission_id,
        "coord": target,
        "arrival_ts": arrival,
        "eta_sec": max(0, int(math.ceil(arrival - current))),
    }


# ---------------------------------------------------------------------------
# 함대 규모 산정
# ---------------------------------------------------------------------------
def _attack_force(
    expected_loot: int,
    have_lf: int,
    have_hf: int,
    have_sc: int,
    have_lc: int = 0,
) -> dict:
    """방어 0 타겟용 소수 전투기 + 최소가치 SC/LC 화물 편성."""
    fighters = min(have_lf, ATTACK_FIGHTERS)
    ships: dict[int, int] = {}
    if fighters > 0:
        ships[LIGHT_FIGHTER] = fighters
    elif have_hf > 0:
        ships[HEAVY_FIGHTER] = min(have_hf, ATTACK_FIGHTERS)
    ships.update(_sizing_cargo_split(expected_loot, have_sc, have_lc))
    return ships


# ---------------------------------------------------------------------------
# 편대 사이징 (SHADOW / 로그 전용) — 라이브 편대는 위 _attack_force 그대로다.
# ---------------------------------------------------------------------------
def _fmt_k(value: float) -> str:
    """자원가치를 k 단위 정수로 축약(로그 가독성). |값|<1000 이면 원값."""
    v = float(value)
    if abs(v) >= 1000:
        return f"{v / 1000:.0f}k"
    return f"{v:.0f}"


def _fmt_fleet(fleet: dict[int, int]) -> str:
    """편대의 전투함선 구성을 'LF120' / 'CR12+BS3' 형태로 축약(화물 제외)."""
    parts: list[str] = []
    labels = {
        LIGHT_FIGHTER: "LF", HEAVY_FIGHTER: "HF", CRUISER: "CR",
        BATTLESHIP: "BS", BATTLECRUISER: "BC", BOMBER: "BB",
        DESTROYER: "DD", REAPER: "RP",
    }
    for ship_id in SIZING_COMBAT_SHIPS:
        if fleet.get(ship_id):
            parts.append(f"{labels[ship_id]}{fleet[ship_id]}")
    return "+".join(parts) if parts else "none"


_SIZING_COMBAT_PRIORITY: tuple[int, ...] = (
    REAPER, DESTROYER, BATTLESHIP, BOMBER, BATTLECRUISER, CRUISER,
    HEAVY_FIGHTER, LIGHT_FIGHTER,
)
# 함종 1척의 건조가치(메탈+크리스탈+듀테륨). battle_sim._COST와 같은
# OGameX GameObjectPrice 실측값이다. 사이징의 ``combat_budget`` 단위와 함종별
# 65% 노출 상한을 정하는데 쓰고, 실제 승률/손실은 battle_sim이 함종 스탯으로 판정한다.
_SIZING_COMBAT_VALUE: dict[int, int] = {
    LIGHT_FIGHTER: 4000,
    HEAVY_FIGHTER: 10000,
    CRUISER: 29000,
    BATTLESHIP: 60000,
    BATTLECRUISER: 85000,
    BOMBER: 90000,
    DESTROYER: 125000,
    REAPER: 160000,
}
_CARGO_BUILD_VALUE = {SMALL_CARGO: 4000, LARGE_CARGO: 12000}


def _sizing_combat_inventory(
    have_lf: int, have_hf: int, combat_inventory: Mapping[int, int] | None,
) -> dict[int, int]:
    """사이징에 쓸 전투함 보유량만 양의 정수로 정규화한다.

    LF/HF는 기존 위치 인자를 권위값으로 두어 기존 호출과 하위호환한다.
    상위 함종은 levels 전체를 combat_inventory로 넘겨도 허용 목록만 필터링된다.
    """
    raw = combat_inventory or {}
    out = {ship_id: max(0, int(raw.get(ship_id, 0) or 0))
           for ship_id in SIZING_COMBAT_SHIPS}
    out[LIGHT_FIGHTER] = max(0, int(have_lf))
    out[HEAVY_FIGHTER] = max(0, int(have_hf))
    return {ship_id: amount for ship_id, amount in out.items() if amount > 0}


def _sizing_combat_split(
    combat_budget: int, have_lf: int, have_hf: int,
    combat_inventory: Mapping[int, int] | None = None,
    max_combat_percent: int = SIZING_MAX_COMBAT_PERCENT,
) -> dict[int, int]:
    """건조가치 ``combat_budget`` 안에서 강한 함종부터 결정적으로 채운다.

    자본함을 LF와 1:1 기체수로 세지 않고 함선의 실제 건조가치를 예산으로 쓴다.
    또 각 함종 출격 수는 ``floor(보유×65%)``를 절대 넘지 않는다. 따라서 희소
    순양전함 1척을 100% 출격시키는 총 기체수 cap 왜곡도 없다. 우선순위는 기본
    공격력이 큰 함종 순이고, 승률·연사·손실가치의 최종 판정은 후단 시뮬레이터가 한다.
    이 함수는 여전히 shadow 후보 생성에서만 쓸 수 있다.
    """
    available = _sizing_combat_inventory(have_lf, have_hf, combat_inventory)
    budget = max(0, int(combat_budget))
    if budget <= 0 or not available:
        return {}
    exposure = max(0, min(100, int(max_combat_percent)))
    caps = {
        ship_id: amount * exposure // 100
        for ship_id, amount in available.items()
    }
    out: dict[int, int] = {}
    for ship_id in _SIZING_COMBAT_PRIORITY:
        unit_value = _SIZING_COMBAT_VALUE[ship_id]
        amount = min(caps.get(ship_id, 0), budget // unit_value)
        if amount <= 0:
            continue
        out[ship_id] = amount
        budget -= amount * unit_value
    return out


def _sizing_max_combat_budget(
    have_lf: int, have_hf: int, combat_inventory: Mapping[int, int] | None,
    max_combat_percent: int = SIZING_MAX_COMBAT_PERCENT,
) -> int:
    """함종별 65% 수량 상한을 모두 쓸 때의 최대 건조가치 노출액."""
    available = _sizing_combat_inventory(have_lf, have_hf, combat_inventory)
    exposure = max(0, min(100, int(max_combat_percent)))
    return sum(
        (amount * exposure // 100) * _SIZING_COMBAT_VALUE[ship_id]
        for ship_id, amount in available.items()
    )


def _sizing_cargo_split(
    loot: int, have_sc: int, have_lc: int,
) -> dict[int, int]:
    """약탈량을 싣는 최소가치 소형/대형화물 조합(보유 상한)을 고른다.

    대형화물은 소형 5대 적재량을 3배 건조가치로 운반하므로 큰 약탈에 효율적이지만,
    작은 약탈에는 소형화물이 더 싸다. 가능한 대형화물 수를 모두 비교해
    (1) 실현 적재량 최대, (2) 건조가치 최소, (3) 과적재/함선수 최소 순으로 선택한다.
    """
    loot = max(0, int(loot))
    have_sc = max(0, int(have_sc))
    have_lc = max(0, int(have_lc))
    if loot <= 0 or have_sc + have_lc <= 0:
        return {}

    max_use_lc = min(have_lc, -(-loot // LARGE_CARGO_CAPACITY))
    best: tuple[tuple[int, int, int, int], int, int] | None = None
    for lc in range(max_use_lc + 1):
        remainder = max(0, loot - lc * LARGE_CARGO_CAPACITY)
        sc = min(have_sc, -(-remainder // SMALL_CARGO_CAPACITY))
        capacity = lc * LARGE_CARGO_CAPACITY + sc * SMALL_CARGO_CAPACITY
        realized = min(loot, capacity)
        value = (lc * _CARGO_BUILD_VALUE[LARGE_CARGO]
                 + sc * _CARGO_BUILD_VALUE[SMALL_CARGO])
        over = max(0, capacity - loot)
        score = (realized, -value, -over, -(lc + sc))
        if best is None or score > best[0]:
            best = (score, sc, lc)

    if best is None:
        return {}
    _, sc, lc = best
    out: dict[int, int] = {}
    if sc > 0:
        out[SMALL_CARGO] = sc
    if lc > 0:
        out[LARGE_CARGO] = lc
    return out


def _sizing_cargo_capacity(fleet: Mapping[int, int]) -> int:
    """사이징 EV에 반영할 기본 화물 적재량."""
    return (max(0, int(fleet.get(SMALL_CARGO, 0))) * SMALL_CARGO_CAPACITY
            + max(0, int(fleet.get(LARGE_CARGO, 0))) * LARGE_CARGO_CAPACITY)


def _sizing_fleet(
    combat_budget: int, loot: int, have_lf: int, have_hf: int, have_sc: int,
    *, have_lc: int = 0, combat_inventory: Mapping[int, int] | None = None,
    max_combat_percent: int = SIZING_MAX_COMBAT_PERCENT,
) -> dict[int, int]:
    """사이징 후보 편대: ``combat_budget`` 내 전투함 + 약탈 맞춤 화물.

    전투함은 LF/HF에 더해 순양함·전함·순양전함 등 SIZING_COMBAT_SHIPS 전체를
    후보로 쓴다. 화물은 소형/대형 조합으로 적재량을 맞춘다. 보유 적재량이
    부족하면 호출부가 실현 가능한 약탈량만 EV에 넣는다. 전투함이 없으면 빈 편대.
    """
    fleet = _sizing_combat_split(
        combat_budget,
        have_lf,
        have_hf,
        combat_inventory=combat_inventory,
        max_combat_percent=max_combat_percent,
    )
    if not fleet:
        return {}
    fleet.update(_sizing_cargo_split(loot, have_sc, have_lc))
    return fleet


def _sizing_shadow_log(
    coord: tuple[int, int, int], defender: dict[int, int], research: dict | None,
    loot: int, *, have_lf: int, have_hf: int, have_sc: int, have_rec: int,
    levels: dict[int, int], own: tuple[int, int, int] | None,
) -> str | None:
    """방어 비례 사이징을 shadow 로 계산해 한 줄 로그를 만든다(라이브 미집행).

    함종별 보유량의 SIZING_MAX_COMBAT_FRACTION 상한 안에서 건조가치 예산을
    스캔해 후보 편대를 만들고
    battle_sim.simulate 로 승률·손실을 재서
      순EV = 승률×약탈 + 데브리 − 자기손실 − 왕복연료
    를 battle_sim.attack_ev(fuel_cost 포함)로 평가한다. 위 2단계(coarse→fine) 스캔으로
    ≈80/90/95% 승률 밴드 티어와 순EV 최대 pick 을 한 줄로 로그한다. 평가할 편대가 없으면 None.

    ※ cached_simulate 로 동일한 battle_sim.simulate 결과를 재사용한다(decide가 아님).
      samples/seed/전투 공식은 그대로이며 라이브 집행과 완전히 독립이다.
    """
    distance = battle_sim.flight_distance(own, coord)
    cap = _sizing_max_combat_budget(have_lf, have_hf, levels)
    if cap <= 0:
        return None
    has_rec = have_rec > 0
    have_lc = int(levels.get(LARGE_CARGO, 0) or 0)
    grid: dict[int, dict] = {}  # combat_budget -> 평가 결과(중복 스캔 방지)

    def _eval(combat_budget: int, samples: int) -> dict | None:
        combat_budget = max(1, min(cap, int(combat_budget)))
        cached = grid.get(combat_budget)
        if cached is not None and cached["samples"] >= samples:
            return cached
        fleet = _sizing_fleet(
            combat_budget, loot, have_lf, have_hf, have_sc,
            have_lc=have_lc, combat_inventory=levels,
        )
        if not fleet:
            return None
        sim = battle_cache.cached_simulate(
            fleet, defender, att_tech=levels, def_tech=research,
            samples=samples, seed=SIZING_SEED,
        )
        fuel = battle_sim.fuel_estimate(fleet, distance)
        realized_loot = min(int(loot), _sizing_cargo_capacity(fleet))
        entry = {
            "combat_budget": combat_budget, "fleet": fleet,
            "win": sim.get("win_prob", 0.0),
            "loss": sim.get("attacker_loss_value", 0.0), "fuel": fuel,
            "ev": battle_sim.attack_ev(
                realized_loot, sim, has_rec, fuel_cost=fuel,
            ),
            "samples": samples,
        }
        grid[combat_budget] = entry
        return entry

    # 1단계 coarse: 전 구간 균등 스캔.
    for i in range(1, SIZING_SCAN_STEPS + 1):
        _eval(cap * i // SIZING_SCAN_STEPS, SIZING_SAMPLES)
    if not grid:
        return None

    # 2단계 fine: 최저 밴드 미만 최대 편대 ~ 최고 밴드 이상 최소 편대 사이(전이 구간)를
    # 표본을 늘려 촘촘히 재스캔해 80/90/95% 밴드가 서로 다른 최소 편대로 분해되게 한다.
    lo_band, hi_band = min(SIZING_WIN_BANDS), max(SIZING_WIN_BANDS)
    below = [c for c, e in grid.items() if e["win"] < lo_band]
    above = [c for c, e in grid.items() if e["win"] >= hi_band]
    lo = max(below) if below else 1
    hi = min(above) if above else cap
    if hi - lo >= 2:
        step = max(1, (hi - lo) // SIZING_FINE_STEPS)
        for combat_budget in range(lo, hi + 1, step):
            _eval(combat_budget, SIZING_BAND_SAMPLES)

    ordered = [grid[c] for c in sorted(grid)]

    def _band_tier(threshold: float) -> dict | None:
        # combat 오름차순 → 승률 threshold 를 처음 넘는 최소 편대.
        for g in ordered:
            if g["win"] >= threshold:
                return g
        return None

    pick = max(ordered, key=lambda g: g["ev"])
    parts: list[str] = []
    band_by_budget: dict[int, str] = {}
    for band in SIZING_WIN_BANDS:
        label = f"tier{int(band * 100)}"
        t = _band_tier(band)
        if t is None:
            parts.append(f"{label}=none")
            continue
        band_by_budget.setdefault(t["combat_budget"], label)
        parts.append(
            f"{label}=({_fmt_fleet(t['fleet'])},"
            f"loss{_fmt_k(t['loss'])},EV{_fmt_k(t['ev'])})"
        )
    # pick 라벨: 밴드 티어와 편대가 같으면 그 이름, 아니면 자체 튜플.
    pick_str = band_by_budget.get(pick["combat_budget"])
    if pick_str is None:
        pick_str = (f"({_fmt_fleet(pick['fleet'])},"
                    f"loss{_fmt_k(pick['loss'])},EV{_fmt_k(pick['ev'])})")
    return (f"[sizing/shadow] target={coord[0]}:{coord[1]}:{coord[2]} "
            f"dist={distance} {' '.join(parts)} pick={pick_str}")


def _safe_zero_defense_report(report: dict | None) -> bool:
    """기존 live '방어0' 게이트에 쓸 수 있는 실제·최신 DB 보고서인지.

    defense_units/ships 중 하나라도 None 이면 HTML 등에 0 표시가 있어도 미상이다. fresh 가
    명시적으로 True 이고 DB 함선 상세도 공개됐으며 defense가 known-empty 일 때만 허용한다.
    기본 방어0 게이트는 알려진 군용/위협 함선도 회피한다. 명시적 canary는 이 함수 대신
    full-intel simulation gate를 탄다.
    """
    if not isinstance(report, dict) or report.get("fresh") is not True:
        return False
    if report.get("defense_units") is None or report.get("ships") is None:
        return False
    if any(uid not in SAFE_RAID_TARGET_SHIPS for uid in report["ships"]):
        return False
    return report.get("defense") == 0 and isinstance(report.get("res_sum"), int)


def _merge_defender_units(report: dict) -> dict[int, int]:
    """공개된 함선+방어 상세을 battle_sim defender 입력 하나로 합친다."""
    defender: dict[int, int] = {}
    for units_key in ("ships", "defense_units"):
        for uid, amount in (report.get(units_key) or {}).items():
            defender[int(uid)] = defender.get(int(uid), 0) + int(amount)
    return defender


def _autonomous_sized_attack(
    defender: dict[int, int],
    defender_research: dict[int, int],
    loot: int,
    levels: dict[int, int],
    *,
    have_rec: int,
    exposure_percent: float,
) -> tuple[dict[int, int], dict] | None:
    """full autonomy에서 방어 보유 표적까지 평가할 최소 권고 편대를 고른다.

    고정 LF 4기나 방어0 allowlist를 쓰지 않고 현재 보유한 전 함종·대형화물까지 후보로
    만든다. 노출률은 LLM의 fleet_risk_percent를 그대로 쓰며, 각 후보는 battle_sim의
    승률·순EV 권고를 통과해야 한다. 이 함수는 게임 상태를 바꾸지 않는 순수 계산이다.
    """
    exposure = max(0, min(100, int(exposure_percent)))
    have_lf = max(0, int(levels.get(LIGHT_FIGHTER, 0)))
    have_hf = max(0, int(levels.get(HEAVY_FIGHTER, 0)))
    have_sc = max(0, int(levels.get(SMALL_CARGO, 0)))
    have_lc = max(0, int(levels.get(LARGE_CARGO, 0)))
    cap = _sizing_max_combat_budget(
        have_lf,
        have_hf,
        levels,
        max_combat_percent=exposure,
    )
    if cap <= 0:
        return None

    choices: list[tuple[int, float, dict[int, int], dict]] = []
    for step in range(1, SIZING_SCAN_STEPS + 1):
        fleet = _sizing_fleet(
            max(1, cap * step // SIZING_SCAN_STEPS),
            loot,
            have_lf,
            have_hf,
            have_sc,
            have_lc=have_lc,
            combat_inventory=levels,
            max_combat_percent=exposure,
        )
        if not fleet:
            continue
        realized_loot = min(int(loot), _sizing_cargo_capacity(fleet))
        decision = battle_cache.cached_decide(
            attacker=fleet,
            defender=defender,
            loot_estimate=realized_loot,
            has_recycler=have_rec > 0,
            att_tech=levels,
            def_tech=defender_research,
            mode="log",
            samples=SIZING_BAND_SAMPLES,
            seed=SIZING_SEED,
        )
        if decision.get("recommend") is True:
            choices.append((
                int(battle_sim.fleet_cost_value(fleet)),
                -float(decision.get("ev", 0.0)),
                fleet,
                decision,
            ))
    if not choices:
        return None
    _, _, fleet, decision = min(choices, key=lambda item: (item[0], item[1]))
    return fleet, decision


def _canary_live_ready(email: str, levels: dict[int, int]) -> bool:
    """live canary의 최소 생존 조건: 자기 행성 2개 이상 + 보수적 함대 슬롯 2개 이상.

    두 번째 자기 행성이 있어야 공격 함대 귀환 뒤 남은/복귀 함대를 외부 메시지 없이
    Transport 플릿세이브할 수 있다. computer technology 기준 기본 1+레벨 슬롯만 인정하고
    General의 표시상 +2는 보수적으로 세지 않는다. DB 실패/불완전 행성 목록은 False다.
    """
    if email not in BATTLE_SIM_CANARY_EMAILS:
        return False
    try:
        planets = db_reader.list_planets_db(email)
        own_planets = {
            int(p["id"]) for p in planets
            if isinstance(p, dict) and p.get("id") is not None
        }
        slots = 1 + max(0, int(levels.get(108, 0)))  # computer_technology
    except Exception:  # noqa: BLE001 - canary는 fail-closed
        return False
    return len(own_planets) >= 2 and slots >= 2


def _preflight_shadow_candidates(
    candidates: list[dict],
    reports: dict[tuple[int, int, int], dict],
    state: dict,
    now: float,
    levels: dict[int, int],
    *,
    have_lf: int,
    have_hf: int,
    have_sc: int,
    have_ep: int,
    min_loot: int,
    debris_factor: float,
    attack_cooldown_sec: int,
    spy_cooldown_sec: int,
    allow_defended_targets: bool,
    loot_percent: int = DEFAULT_LOOT_PERCENT,
) -> list[dict]:
    """Return cheap-gate survivors that could request a real attack preflight now.

    This intentionally performs no battle simulation, account-fleet DB read or ETA
    calculation.  It only prevents observation-only shadow work for accounts that cannot
    currently advance toward an attack (spy-only policy, stale intel, cooldown, missing
    probes, pending preflight, or insufficient value).
    """
    if any(_preflight_request(state, row["coord"]) is not None for row in candidates):
        # At most one fleet action can be completed by this tick.  A pending request has
        # priority and may run the full safety-critical sizing path, so spend no shadow
        # budget on alternate targets in the same account tick.
        return []
    eligible: list[dict] = []
    for row in candidates:
        coord = row["coord"]
        report = reports.get(coord)
        report_eligible = (
            _full_intel_report(report)
            if allow_defended_targets
            else _safe_zero_defense_report(report)
        )
        if not report_eligible:
            continue
        loot = expected_raid_loot(report, loot_percent)
        debris_fn = all_ship_debris_value if allow_defended_targets else ship_debris_value
        debris = int(debris_fn(report.get("ships") or {}) * debris_factor)
        if loot + debris < min_loot:
            continue
        if now - _last(state, coord, "attack") < attack_cooldown_sec:
            continue
        if now - _last(state, coord, "preflight_blocked") < spy_cooldown_sec:
            continue
        if not allow_defended_targets and not _attack_force(
            loot, have_lf, have_hf, have_sc, int(levels.get(LARGE_CARGO, 0))
        ):
            continue
        desired = desired_spy_probes(
            report,
            levels.get(ESPIONAGE_TECH, 0),
            _last_spy_probes(state, coord),
        )
        if have_ep < desired:
            continue
        eligible.append(row)
    return eligible


def _shadow_budget_allows(
    email: str,
    candidates: list[dict],
    reports: dict[tuple[int, int, int], dict],
    now: float,
) -> bool:
    """Low-frequency deterministic budget for observation-only shadow sizing."""
    if not candidates:
        return False
    coord = candidates[0]["coord"]
    report = reports.get(coord) or {}
    return battle_cache.deterministic_sample(
        "raider-shadow",
        {
            "email": email,
            "bucket": int(float(now) // SHADOW_SAMPLE_BUCKET_SEC),
            "coord": coord,
            "report_id": report.get("report_id"),
        },
        denominator=SHADOW_SAMPLE_DENOMINATOR,
    )


def _battle_sim_shadow(
    candidates: list[dict], reports: dict[tuple[int, int, int], dict],
    levels: dict[int, int], *, have_lf: int, have_hf: int, have_sc: int,
    have_rec: int, min_loot: int, debris_factor: float,
    mode: str = BATTLE_SIM_MODE, own: tuple[int, int, int] | None = None,
    loot_percent: int = DEFAULT_LOOT_PERCENT,
) -> dict | None:
    """틱당 최대 1개 타겟을 battle_sim으로 평가한다(기본 log, canary에서만 live).

    반환 dict는 log와 (완전한 intel이면) target/attacker/decision을 담는다. log 모드 호출부는
    동일 타겟이 기존 방어0 게이트로 실제 파견됐을 때 관측 예측만 기록한다. 명시적 live
    canary 호출부만 decision.recommend를 읽어 동일 좌표·함대 집행에 연결한다.

    research가 NULL이면 0레벨로 꾸며 simulate하지 않는다. 대신 partial/conservative 로그를
    남기고 평가를 생략한다. defense/ships 미상이나 stale도 같은 보수적 원칙으로 생략한다.
    """
    mode_tag = "[CANARY LIVE]" if mode == "live" else "[SHADOW ONLY]"
    full: list[tuple[int, int, tuple[int, int, int], dict, dict[int, int]]] = []
    partial: list[tuple[tuple[int, int, int], list[str]]] = []

    for row in candidates:
        coord = row["coord"]
        report = reports.get(coord)
        if not isinstance(report, dict):
            continue
        reasons: list[str] = []
        if report.get("fresh") is not True:
            reasons.append("stale")
        if report.get("res_sum") is None:
            reasons.append("resources 미상")
        if report.get("ships") is None:
            reasons.append("ships 미상")
        if report.get("defense_units") is None:
            reasons.append("defense 미상")
        if report.get("research") is None:
            reasons.append("research 미상")
        if reasons:
            partial.append((coord, reasons))
            continue

        loot = expected_raid_loot(report, loot_percent)
        debris = int(all_ship_debris_value(report["ships"]) * debris_factor)
        value = loot + debris
        if value < min_loot:
            continue
        attacker = _attack_force(
            loot, have_lf, have_hf, have_sc, int(levels.get(LARGE_CARGO, 0))
        )
        if not attacker:
            continue
        # 실제 공격으로 이어질 수 있는 방어0 타겟을 우선해 battle_guard 상관 표본을 쌓고,
        # 동률이면 관측 가치가 큰 타겟을 고른다. 그래도 live 집행 판정과는 연결하지 않는다.
        live_candidate = 1 if _safe_zero_defense_report(report) else 0
        full.append((live_candidate, value, coord, report, attacker))

    if full:
        _, _, coord, report, attacker = max(full, key=lambda item: (item[0], item[1]))
        try:
            decision = battle_cache.cached_decide(
                attacker=attacker,
                defender=_merge_defender_units(report),
                loot_estimate=expected_raid_loot(report, loot_percent),
                has_recycler=have_rec > 0,
                att_tech=levels,
                def_tech=report["research"],
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 - shadow 실패는 live 방어0 경로와 독립
            return {
                "log": (f"[battle_sim/{mode}] partial/conservative target "
                        f"{coord[0]}:{coord[1]}:{coord[2]} 평가오류 "
                    f"{type(exc).__name__} {mode_tag}")
            }
        log = decision.get("log") if isinstance(decision, dict) else None
        if not isinstance(log, str):
            log = f"[battle_sim/{mode}] partial/conservative 결과 형식 오류"
        # 선택된 full-intel 타겟에 한해 방어 비례 사이징을 shadow 로 함께 계산해 로그로만
        # 남긴다(라이브 4대 편대·집행은 위 그대로 유지). 실패는 삼켜 라이브 경로와 격리.
        sizing_log = None
        try:
            sizing_log = _sizing_shadow_log(
                coord, _merge_defender_units(report), report["research"],
                expected_raid_loot(report, loot_percent),
                have_lf=have_lf, have_hf=have_hf, have_sc=have_sc,
                have_rec=have_rec, levels=levels, own=own,
            )
        except Exception:  # noqa: BLE001 - 사이징 shadow 실패는 라이브/기존 로그와 독립
            sizing_log = None
        return {
            "log": (f"{log} target={coord[0]}:{coord[1]}:{coord[2]} "
                    f"intel_age={report.get('age_sec')}s {mode_tag}"),
            "target": coord,
            "attacker": attacker,
            "decision": decision,
            "sizing_log": sizing_log,
        }

    if partial:
        coord, reasons = partial[0]
        return {
            "log": (f"[battle_sim/{mode}] partial/conservative target="
                    f"{coord[0]}:{coord[1]}:{coord[2]} 평가생략 "
                    f"({', '.join(reasons)}) {mode_tag}")
        }
    return None


# ---------------------------------------------------------------------------
# 공격 직전 2단계 preflight (feature flag OFF)
# ---------------------------------------------------------------------------
_SHIP_SPEED_SPEC: dict[int, tuple[int, int, int]] = {
    # ship_id: (기본속도, 엔진 연구 id, 레벨당 보너스 %)
    SMALL_CARGO: (5000, 115, 10),
    LARGE_CARGO: (7500, 115, 10),
    LIGHT_FIGHTER: (12500, 115, 10),
    HEAVY_FIGHTER: (10000, 117, 20),
    CRUISER: (15000, 117, 20),
    BATTLESHIP: (10000, 118, 30),
    BATTLECRUISER: (10000, 118, 30),
    BOMBER: (4000, 117, 20),
    DESTROYER: (5000, 118, 30),
    DEATHSTAR: (100, 118, 30),
    REAPER: (7000, 118, 30),
}


def _full_intel_report(report: dict | None) -> bool:
    """재시뮬·집행에 쓸 수 있는 최신 full-intel 보고서인지."""
    return bool(
        isinstance(report, dict)
        and report.get("fresh") is True
        and isinstance(report.get("res_sum"), int)
        and report.get("ships") is not None
        and report.get("defense_units") is not None
        and report.get("research") is not None
    )


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _report_after_preflight(
    report: dict | None, request: dict, now: float,
) -> bool:
    """보고서가 반드시 이 preflight 재정찰 **이후** 생성됐는지.

    report id와 created_at 둘 다 확인한다. id는 DB 시계 스큐/초 단위
    timestamp 동률을 막고, created_at은 다른 좌표의 새 id를 잘못 소비하지 않게 한다.
    """
    if not _full_intel_report(report):
        return False
    try:
        requested_at = float(request["requested_at"])
    except (KeyError, TypeError, ValueError):
        return False
    report_id = _int_or_none(report.get("report_id"))
    before_id = _int_or_none(request.get("report_id_before"))
    try:
        created_at = float(report.get("created_at_ts"))
    except (TypeError, ValueError):
        try:
            created_at = float(now) - float(report["age_sec"])
        except (KeyError, TypeError, ValueError):
            return False
    if before_id is not None:
        if report_id is None or report_id < before_id:
            return False
        if report_id == before_id:
            return created_at >= requested_at - PREFLIGHT_REUSE_FRESH_REPORT_SEC
    return created_at >= requested_at


def _merge_combat_peak(
    defender: Mapping[int, int], peak: Mapping[int, int],
) -> dict[int, int]:
    """현재 full-intel 방어자와 24h 함종별 peak를 componentwise max-merge."""
    merged = {int(uid): max(0, int(amount)) for uid, amount in defender.items()}
    for uid, amount in peak.items():
        uid, amount = int(uid), max(0, int(amount))
        if uid in PREFLIGHT_COMBAT_SHIPS and amount > merged.get(uid, 0):
            merged[uid] = amount
    return {uid: amount for uid, amount in merged.items() if amount > 0}


def _read_fleet_speed_war() -> int | None:
    """공개 우주 설정 fleet_speed_war. 타겟 상태가 아닌 서버 설정 직독."""
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE `key` = %s", ("fleet_speed_war",))
            row = cur.fetchone()
        speed = int(row["value"]) if row else 0
    except Exception:  # noqa: BLE001 - ETA를 모르면 fail-closed
        return None
    return speed if speed > 0 else None


def _ship_speed(ship_id: int, levels: Mapping[int, int]) -> int | None:
    """OGameX SpeedPropertyService의 엔진 보너스(클래스 보너스는 안전하게 생략)."""
    spec = _SHIP_SPEED_SPEC.get(int(ship_id))
    if spec is None:
        return None
    base, drive_id, bonus_per_level = spec
    # 소형화물은 임펄스 5부터 기본속도 10,000+임펄스 보너스로 전환.
    if ship_id == SMALL_CARGO and int(levels.get(117, 0) or 0) >= 5:
        base, drive_id, bonus_per_level = 10000, 117, 20
    drive_level = max(0, int(levels.get(drive_id, 0) or 0))
    return base * (100 + bonus_per_level * drive_level) // 100


def _attack_eta_seconds(
    own: tuple[int, int, int] | None, coord: tuple[int, int, int],
    ships: Mapping[int, int], levels: Mapping[int, int],
    *, fleet_speed_war: int | None = None,
) -> int | None:
    """100% 속도 공격의 보수적 ETA. 서버 공식을 ceil로 복제한다.

    빈/비활성 시스템 거리 축소와 General/Collector 속도 보너스를 생략하므로
    실제보다 길게 계산된다. 알 수 없는 함종/설정은 None으로 fail-closed한다.
    """
    speed_setting = _read_fleet_speed_war() if fleet_speed_war is None else fleet_speed_war
    try:
        speed_setting = int(speed_setting)
    except (TypeError, ValueError):
        return None
    if speed_setting <= 0:
        return None
    speeds = []
    for ship_id, amount in ships.items():
        if int(amount) <= 0:
            continue
        speed = _ship_speed(int(ship_id), levels)
        if speed is None or speed <= 0:
            return None
        speeds.append(speed)
    if not speeds:
        return None
    distance = battle_sim.flight_distance(own, coord)
    if distance <= 0:
        return None
    slowest = min(speeds)
    seconds = (35000 / 10 * math.sqrt(distance * 10 / slowest) + 10) / speed_setting
    return max(1, int(math.ceil(seconds)))


def _request_attack_preflight(
    client, email: str, state: dict, coord: tuple[int, int, int], report: dict | None,
    levels: dict[int, int], have_ep: int, now: float,
) -> str:
    """재정찰만 파견하고 persistent request를 남긴다(같은 틱 polling 없음)."""
    desired = desired_spy_probes(
        report, levels.get(ESPIONAGE_TECH, 0), _last_spy_probes(state, coord),
    )
    if have_ep < desired:
        return f"raid PREFLIGHT HOLD (정찰기 축적 {have_ep}/{desired})"
    res = fleet.spy(client, coord, probes=desired)
    if not res.get("ok"):
        return "raid PREFLIGHT HOLD (재정찰 파견 실패)"
    _mark_spy(state, coord, now, desired)
    _set_preflight_request(
        state, coord, requested_at=now,
        report_id_before=_int_or_none((report or {}).get("report_id")),
        probes=desired,
    )
    _save_state(email, state)
    return (f"raid PREFLIGHT SPY {coord[0]}:{coord[1]}:{coord[2]} "
            f"(정찰기{desired}, 다음 틱 full-intel 재검증)")


def _block_preflight(
    email: str, state: dict, coord: tuple[int, int, int], now: float,
) -> None:
    """현재 preflight를 폐기하고 좌표 재정찰 스팸을 막는 쿨다운 표시."""
    _clear_preflight_request(state, coord)
    _mark(state, coord, "preflight_blocked", now)
    _save_state(email, state)


def _preflight_fleet_value_budget(
    email: str,
    *,
    risk_percent: float = PREFLIGHT_FLEET_VALUE_RISK_PERCENT,
    minimum_limit: int = PREFLIGHT_MIN_FLEET_VALUE_LIMIT,
) -> tuple[int, int] | None:
    """(동적 공격 한도, 계정 전체 함대가치), 조회 실패 시 ``None``.

    현재 행성만 보면 플릿세이브/공격 중인 함대가 사라져 한도가 출렁인다. 공용 DB
    스냅샷으로 모든 행성·비행 함대·미완료 조선소 큐를 합산해 성장에 따라 한도를
    늘리되, 초보 레이더도 최소 편대 하나는 짤 수 있도록 절대 하한을 둔다.
    """
    try:
        account_ships = db_reader.get_account_ship_totals_db(email)
        account_value = int(battle_sim.fleet_cost_value(account_ships))
    except Exception:  # noqa: BLE001 - 위험 예산을 모르면 공격만 fail-closed
        return None
    limit = max(
        max(0, int(minimum_limit)),
        int(account_value * max(0.0, float(risk_percent)) / 100.0),
    )
    return limit, account_value


def _report_version(report: Mapping | None) -> tuple[int | None, object, object]:
    """Identity of the exact legal report snapshot used for a dispatch decision."""
    if not isinstance(report, Mapping):
        return None, None, None
    return (
        _int_or_none(report.get("report_id")),
        report.get("created_at_ts"),
        report.get("created_at"),
    )


def _fleet_available(ships: Mapping[int, int], levels: Mapping[int, int]) -> bool:
    """Whether the current-planet snapshot still contains the selected parked fleet."""
    return bool(ships) and all(
        int(amount) > 0 and int(levels.get(int(ship_id), 0) or 0) >= int(amount)
        for ship_id, amount in ships.items()
    )


def _revalidate_preflight_dispatch(
    client,
    email: str,
    coord: tuple[int, int, int],
    request: dict,
    evaluated_report: dict,
    evaluated_peak: Mapping[int, int],
    evaluated_decision: dict,
    ships: dict[int, int],
    levels: dict[int, int],
    state: dict,
    now: float,
    *,
    have_rec: int,
    min_loot: int,
    debris_factor: float,
    max_active_missions: int,
    attack_cooldown_sec: int,
    allow_defended_targets: bool,
    loot_percent: int = DEFAULT_LOOT_PERCENT,
) -> tuple[dict, dict] | tuple[None, str]:
    """Repeat every volatile safety gate immediately before ``fleet.attack``.

    Cached Monte Carlo output is reusable only when its complete combat content is
    unchanged.  A newer report, changed 24h peak, missing parked ship, occupied slot or
    changed EV therefore fails closed instead of using a stale recommendation.
    """
    latest_report = read_spy_reports(email).get(coord)
    if not _report_after_preflight(latest_report, request, now):
        return None, "발사 직전 fresh report 재검증 실패"
    if _report_version(latest_report) != _report_version(evaluated_report):
        return None, "발사 직전 report 변경"
    report_eligible = (
        _full_intel_report(latest_report)
        if allow_defended_targets
        else _safe_zero_defense_report(latest_report)
    )
    if not report_eligible:
        return None, "발사 직전 방어/함대 변경"

    latest_peak = read_spy_combat_peak(email, coord)
    if latest_peak is None:
        return None, "발사 직전 24h envelope 조회 실패"
    if dict(latest_peak) != {int(k): int(v) for k, v in evaluated_peak.items()}:
        return None, "발사 직전 24h envelope 변경"

    if not _fleet_available(ships, levels):
        return None, "발사 직전 보유함대 부족"
    active, _attacking, _harvesting = _active_fleet_info(client)
    if active >= max_active_missions:
        return None, "발사 직전 함대슬롯 부족"
    if now - _last(state, coord, "attack") < attack_cooldown_sec:
        return None, "발사 직전 공격 쿨다운"

    loot = expected_raid_loot(latest_report, loot_percent)
    debris_fn = all_ship_debris_value if allow_defended_targets else ship_debris_value
    debris = int(debris_fn(latest_report.get("ships") or {}) * debris_factor)
    if loot + debris < min_loot:
        return None, "발사 직전 가치 미달"
    defender = _merge_combat_peak(_merge_defender_units(latest_report), latest_peak)
    realized_loot = min(loot, _sizing_cargo_capacity(ships))
    try:
        if allow_defended_targets:
            # Autonomous sizing is fixed-seed (24/1234), so this is normally an exact
            # cache hit for the selected fleet and remains bit-identical on a miss.
            decision = battle_cache.cached_decide(
                attacker=ships,
                defender=defender,
                loot_estimate=realized_loot,
                has_recycler=have_rec > 0,
                att_tech=levels,
                def_tech=latest_report["research"],
                mode="log",
                samples=SIZING_BAND_SAMPLES,
                seed=SIZING_SEED,
            )
        else:
            # Guided v1 deliberately used seed=None.  A second simulation would draw a
            # different sample and change the original attack judgment.  Recheck the
            # volatile EV inputs against that exact first sample instead.
            sim = evaluated_decision.get("sim")
            if not isinstance(sim, dict):
                return None, "발사 직전 EV 표본 형식 오류"
            win = float(sim.get("win_prob", 0.0))
            ev = battle_sim.attack_ev(realized_loot, sim, have_rec > 0)
            if win < battle_sim.WIN_THRESHOLD:
                return None, (
                    f"발사 직전 EV 재검증: 승률 {win:.0%}<"
                    f"{battle_sim.WIN_THRESHOLD:.0%}"
                )
            if ev < battle_sim.MIN_EV:
                return None, (
                    f"발사 직전 EV 재검증: EV {ev:.0f}<"
                    f"{battle_sim.MIN_EV:.0f}"
                )
            decision = evaluated_decision
    except Exception as exc:  # noqa: BLE001 - 마지막 검증 실패는 절대 발사하지 않음
        return None, f"발사 직전 EV 재검증 {type(exc).__name__}"
    if not isinstance(decision, dict) or decision.get("recommend") is not True:
        reason = decision.get("reason", "형식 오류") if isinstance(decision, dict) else "형식 오류"
        return None, f"발사 직전 EV 재검증: {reason}"
    return latest_report, decision


def _attack_preflight_step(
    client, email: str, levels: dict[int, int], state: dict, now: float,
    own: tuple[int, int, int], candidates: list[dict],
    reports: dict[tuple[int, int, int], dict], *,
    have_lf: int, have_hf: int, have_sc: int, have_ep: int, have_rec: int,
    min_loot: int, debris_factor: float,
    attack_cooldown_sec: int = ATTACK_COOLDOWN_SEC,
    spy_cooldown_sec: int = SPY_COOLDOWN_SEC,
    fleet_risk_percent: float = PREFLIGHT_FLEET_VALUE_RISK_PERCENT,
    min_fleet_value_limit: int = PREFLIGHT_MIN_FLEET_VALUE_LIMIT,
    max_attack_eta_sec: int = PREFLIGHT_MAX_ETA_SEC,
    max_active_missions: int = MAX_ACTIVE_MISSIONS,
    allow_defended_targets: bool = False,
    loot_percent: int = DEFAULT_LOOT_PERCENT,
) -> str | None:
    """기존 방어0 공격의 비동기 2단계 안전 preflight.

    pending 요청을 먼저 소비한다. 없으면 공격할 가치가 있는 기존
    known-zero 타겟에 재정찰만 보내고 즉시 반환한다. 어떤 경우에도 polling/
    sleep/타겟 현재 함대 DB 직독은 하지 않는다.
    """
    # 2단계: 지난 틱의 재정찰 요청을 새 full-intel로 소비.
    for row in candidates:
        coord = row["coord"]
        request = _preflight_request(state, coord)
        if request is None:
            continue
        try:
            requested_at = float(request["requested_at"])
        except (KeyError, TypeError, ValueError):
            requested_at = 0.0
        if requested_at <= 0 or now - requested_at > PREFLIGHT_REQUEST_TTL_SEC:
            _clear_preflight_request(state, coord)
            _save_state(email, state)
            return _request_attack_preflight(
                client, email, state, coord, reports.get(coord), levels, have_ep, now,
            )

        report = reports.get(coord)
        if not _report_after_preflight(report, request, now):
            return (f"raid PREFLIGHT WAIT {coord[0]}:{coord[1]}:{coord[2]} "
                    "(요청 이후 full-intel 대기)")

        # guided는 종전 defense0 게이트, full autonomy는 full-intel 방어 표적도 시뮬한다.
        report_eligible = (
            _full_intel_report(report)
            if allow_defended_targets
            else _safe_zero_defense_report(report)
        )
        if not report_eligible:
            _block_preflight(email, state, coord, now)
            return "raid PREFLIGHT HOLD (재정찰에 방어/복귀함대 탐지)"

        loot = expected_raid_loot(report, loot_percent)
        debris_fn = all_ship_debris_value if allow_defended_targets else ship_debris_value
        debris = int(debris_fn(report.get("ships") or {}) * debris_factor)
        if loot + debris < min_loot:
            _block_preflight(email, state, coord, now)
            return "raid PREFLIGHT HOLD (재정찰 가치 미달)"
        if now - _last(state, coord, "attack") < attack_cooldown_sec:
            _block_preflight(email, state, coord, now)
            return "raid PREFLIGHT HOLD (공격 쿨다운)"

        # Account-wide risk budget is a cheap DB gate compared with 16×24 defended
        # simulations.  Fail here before spending any Monte Carlo CPU.
        if (
            fleet_risk_percent == PREFLIGHT_FLEET_VALUE_RISK_PERCENT
            and min_fleet_value_limit == PREFLIGHT_MIN_FLEET_VALUE_LIMIT
        ):
            fleet_budget = _preflight_fleet_value_budget(email)
        else:
            fleet_budget = _preflight_fleet_value_budget(
                email,
                risk_percent=fleet_risk_percent,
                minimum_limit=min_fleet_value_limit,
            )
        if fleet_budget is None:
            _block_preflight(email, state, coord, now)
            return "raid PREFLIGHT HOLD (계정 함대가치 조회 실패)"
        fleet_value_limit, account_fleet_value = fleet_budget

        decision = None
        peak: dict[int, int]
        if allow_defended_targets:
            peak = read_spy_combat_peak(email, coord)
            if peak is None:
                _block_preflight(email, state, coord, now)
                return "raid PREFLIGHT HOLD (24h 정찰 envelope 조회 실패)"
            defender = _merge_combat_peak(_merge_defender_units(report), peak)
            sized = _autonomous_sized_attack(
                defender,
                report["research"],
                loot,
                levels,
                have_rec=have_rec,
                exposure_percent=fleet_risk_percent,
            )
            if sized is None and _safe_zero_defense_report(report):
                fallback = _attack_force(
                    loot,
                    have_lf,
                    have_hf,
                    have_sc,
                    int(levels.get(LARGE_CARGO, 0)),
                )
                if fallback:
                    sized = (fallback, None)
            if sized is None:
                strategic_growth.record_raid_opportunity(
                    email,
                    potential_loot=loot,
                    realized_loot=min(
                        loot,
                        have_sc * SMALL_CARGO_CAPACITY
                        + int(levels.get(LARGE_CARGO, 0)) * LARGE_CARGO_CAPACITY,
                    ),
                    target=coord,
                    outcome="no-profitable-fleet",
                )
                _block_preflight(email, state, coord, now)
                return "raid PREFLIGHT HOLD (방어 비례 수익 편대 없음)"
            ships, decision = sized
        else:
            ships = _attack_force(
                loot, have_lf, have_hf, have_sc, int(levels.get(LARGE_CARGO, 0))
            )
            if not ships:
                _block_preflight(email, state, coord, now)
                return "raid PREFLIGHT HOLD (공격 함대 없음)"
        strategic_growth.record_raid_opportunity(
            email,
            potential_loot=loot,
            realized_loot=min(loot, _sizing_cargo_capacity(ships)),
            target=coord,
            outcome="profitable-fleet" if decision else "zero-defense-fleet",
        )
        fleet_value = int(battle_sim.fleet_cost_value(ships))
        if fleet_value > fleet_value_limit:
            _block_preflight(email, state, coord, now)
            return (f"raid PREFLIGHT HOLD (함대가치 {fleet_value}>"
                    f"동적한도{fleet_value_limit}; 계정{account_fleet_value}, "
                    f"위험{fleet_risk_percent:g}%)")
        eta = _attack_eta_seconds(own, coord, ships, levels)
        if eta is None or (max_attack_eta_sec > 0 and eta > max_attack_eta_sec):
            _block_preflight(email, state, coord, now)
            return (f"raid PREFLIGHT HOLD (ETA {eta if eta is not None else 'unknown'}>"
                    f"{max_attack_eta_sec}s)")

        realized_loot = min(loot, _sizing_cargo_capacity(ships))
        if decision is None:
            peak = read_spy_combat_peak(email, coord)
            if peak is None:
                _block_preflight(email, state, coord, now)
                return "raid PREFLIGHT HOLD (24h 정찰 envelope 조회 실패)"
            defender = _merge_combat_peak(_merge_defender_units(report), peak)
            try:
                decision = battle_cache.cached_decide(
                    attacker=ships,
                    defender=defender,
                    loot_estimate=realized_loot,
                    has_recycler=have_rec > 0,
                    att_tech=levels,
                    def_tech=report["research"],
                    mode="log",
                )
            except Exception as exc:  # noqa: BLE001 - preflight 시뮬 실패 = 미집행
                _block_preflight(email, state, coord, now)
                return f"raid PREFLIGHT HOLD (재시뮬 {type(exc).__name__})"
        if not isinstance(decision, dict) or decision.get("recommend") is not True:
            _block_preflight(email, state, coord, now)
            reason = decision.get("reason", "형식 오류") if isinstance(decision, dict) else "형식 오류"
            return f"raid PREFLIGHT HOLD (24h peak stress: {reason})"

        # Re-read every volatile safety input immediately before the POST.  The exact
        # calculation cache makes the unchanged case a cheap hit; changed content cannot
        # inherit an old recommendation because it hashes to a different key and is also
        # rejected by the exact report/envelope checks.
        latest_report, revalidated = _revalidate_preflight_dispatch(
            client,
            email,
            coord,
            request,
            report,
            peak,
            decision,
            ships,
            levels,
            state,
            now,
            have_rec=have_rec,
            min_loot=min_loot,
            debris_factor=debris_factor,
            max_active_missions=max_active_missions,
            attack_cooldown_sec=attack_cooldown_sec,
            allow_defended_targets=allow_defended_targets,
            loot_percent=loot_percent,
        )
        if latest_report is None:
            _block_preflight(email, state, coord, now)
            return f"raid PREFLIGHT HOLD ({revalidated})"
        # An unchanged exact-cache result is value-identical.  Keep the original object
        # so battle_guard records precisely the decision paired with the selected fleet;
        # changed content would already have failed the report/envelope checks above.
        if revalidated != decision:
            decision = revalidated
        realized_loot = min(
            expected_raid_loot(latest_report, loot_percent),
            _sizing_cargo_capacity(ships),
        )

        res = fleet.attack(client, coord, ships)
        _clear_preflight_request(state, coord)  # 파견 실패도 다음 시도에는 새 정찰 필수.
        _save_state(email, state)
        if not res.get("ok"):
            return "raid PREFLIGHT HOLD (공격 파견 실패)"
        try:
            # 기존 old shadow가 아니라 방금 소비한 full-intel+peak stress 결정을 실전과 대조.
            battle_guard.record_prediction(
                email, coord, ships, decision, mode="log", sent_at=now,
                cohort=PREFLIGHT_BATTLE_GUARD_COHORT,
            )
        except Exception:  # noqa: BLE001 - 관측 기록 실패가 파견을 뒤집지 않음
            pass
        _record_attack(
            state, coord, now, player_id=_row_player_id(row)
        )
        _save_state(email, state)
        return (f"raid ATTACK[PREFLIGHT] {coord[0]}:{coord[1]}:{coord[2]} "
                f"(함대가치{fleet_value}/동적한도{fleet_value_limit}, ETA{eta}s, "
                f"24h-peak, 예상약탈~{realized_loot})")

    # 1단계: 기존 방어0/가치/쿨다운 하드게이트를 통과한 타겟에 재정찰 요청.
    for row in candidates:
        coord = row["coord"]
        report = reports.get(coord)
        report_eligible = (
            _full_intel_report(report)
            if allow_defended_targets
            else _safe_zero_defense_report(report)
        )
        if not report_eligible:
            continue
        loot = expected_raid_loot(report, loot_percent)
        debris_fn = all_ship_debris_value if allow_defended_targets else ship_debris_value
        debris = int(debris_fn(report.get("ships") or {}) * debris_factor)
        if loot + debris < min_loot:
            continue
        if now - _last(state, coord, "attack") < attack_cooldown_sec:
            continue
        if now - _last(state, coord, "preflight_blocked") < spy_cooldown_sec:
            continue
        if not allow_defended_targets and not _attack_force(
            loot, have_lf, have_hf, have_sc, int(levels.get(LARGE_CARGO, 0))
        ):
            continue
        return _request_attack_preflight(
            client, email, state, coord, report, levels, have_ep, now,
        )
    return None


# ---------------------------------------------------------------------------
# 데브리 필드 수거 (리사이클러)
# ---------------------------------------------------------------------------
def _recycler_prereqs_met(levels: dict[int, int]) -> bool:
    """리사이클러(209) 선행조건(RECYCLER_REQUIREMENTS)이 모두 충족됐는지."""
    return all(levels.get(tid, 0) >= lv for tid, lv in RECYCLER_REQUIREMENTS)


def pending_recycler_build(email: str) -> bool:
    """조선소 큐(unit_queues)에 아직 완료되지 않은 리사이클러 건조가 있으면 True.

    have_rec(행성 보유 수)는 큐 대기 중인 함선을 세지 않으므로, 이 체크가 없으면 데브리가
    있는 동안 매 틱 리사이클러를 중복 큐잉하게 된다. db_reader 의 읽기전용 커넥션 재사용.
    조회 실패 시 False(보수적 — 최악의 경우 한 번 더 큐잉될 뿐).
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) c FROM unit_queues uq "
                "JOIN planets p ON p.id = uq.planet_id "
                "JOIN users u ON u.id = p.user_id "
                "WHERE u.email = %s AND uq.object_id = %s AND uq.processed = 0",
                (email, RECYCLER),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - DB 실패는 중복 큐잉 방지 실패로 흡수
        return False
    return bool(row and int(row["c"]) > 0)


def read_debris_fields(galaxy: int, system: int, span: int) -> list[dict]:
    """자기 은하 galaxy 의 system±span 범위 데브리 필드를 DB 직독으로 읽는다.

    반환: [{"coord": (g,s,p), "metal": int, "crystal": int, "value": metal+crystal}]
    (듀테륨 데브리는 서버 설정상 off 라 가치에서 제외). DB 실패 시 빈 리스트.
    debris_fields 테이블 컬럼: galaxy, system, planet, metal, crystal (add_debris_fields_table).
    """
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT galaxy g, system s, planet p, metal, crystal "
                "FROM debris_fields "
                "WHERE galaxy = %s AND system BETWEEN %s AND %s",
                (int(galaxy), int(system) - int(span), int(system) + int(span)),
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - DB 실패는 수거 생략으로 흡수
        return []
    out: list[dict] = []
    for r in rows:
        try:
            metal = int(r["metal"])
            crystal = int(r["crystal"])
            coord = (int(r["g"]), int(r["s"]), int(r["p"]))
        except (TypeError, ValueError, KeyError):
            continue
        out.append({"coord": coord, "metal": metal, "crystal": crystal,
                    "value": metal + crystal})
    return out


def find_debris_targets(
    galaxy: int, system: int, own: tuple[int, int, int], span: int | None = None
) -> list[dict]:
    """수거 후보 데브리 필드를 가치 내림차순(동률이면 가까운 순)으로 반환.

    제외: 사람 계정 시스템(USER_EXCLUDE_SYSTEMS), 원정 데브리(position 16 — 파스파인더
    전용이라 리사이클러로 수거 불가), 유효 범위 밖 위치, 가치 < HARVEST_MIN_VALUE.
    자기 좌표는 방어전 잔해 카운터 수거 대상이므로 포함한다.
    """
    span = HARVEST_DEBRIS_SPAN if span is None else span
    out: list[dict] = []
    for r in read_debris_fields(galaxy, system, span):
        g, s, p = r["coord"]
        if (g, s) in USER_EXCLUDE_SYSTEMS:
            continue
        if not (1 <= p <= 15):   # 16=원정 데브리(파스파인더 전용), 그 외는 무효 위치
            continue
        if r["value"] < HARVEST_MIN_VALUE:
            continue
        out.append(r)
    out.sort(key=lambda r: (-r["value"], abs(r["coord"][1] - system)))
    return out


def _recyclers_for_value(value: int, have_rec: int) -> int:
    """데브리 가치를 다 실어올 리사이클러 수 = ceil(value/용량), 보유량으로 클램프."""
    needed = -(-int(value) // RECYCLER_CAPACITY)  # ceil
    return max(0, min(int(have_rec), needed))


def _harvest_support_cargo(
    value: int, recyclers: int, have_sc: int, have_lc: int,
) -> dict[int, int]:
    """리사이클러와 동행할 가용 화물 편성.

    약탈/긴급 대피용 최소 수량을 우선 남기고 그 밖의 화물선으로 리사이클러 용량 부족분을
    채운다. 부족분이 0이거나 예약분만 남았어도 주기장 화물선 중 가장 작은 1대는 반드시
    동행한다. 화물선이 0대면 빈 dict이며 호출부가 수거 자체를 보류한다.
    """
    parked_sc = max(0, int(have_sc))
    parked_lc = max(0, int(have_lc))
    if parked_sc + parked_lc <= 0:
        return {}
    available_sc = max(0, parked_sc - HARVEST_RESERVED_SMALL_CARGO)
    available_lc = max(0, parked_lc - HARVEST_RESERVED_LARGE_CARGO)

    shortfall = max(0, int(value) - max(0, int(recyclers)) * RECYCLER_CAPACITY)
    cargo = _sizing_cargo_split(shortfall, available_sc, available_lc)
    if cargo:
        return cargo
    if available_sc > 0:
        return {SMALL_CARGO: 1}
    if available_lc > 0:
        return {LARGE_CARGO: 1}
    # 예약분만 남았더라도 혼합 수확 계약이 우선이다. 기회비용이 작은 소형 1대를 먼저 쓴다.
    if parked_sc > 0:
        return {SMALL_CARGO: 1}
    return {LARGE_CARGO: 1}


def _parked_harvest_inventory(
    email: str, fallback_levels: Mapping[int, int],
) -> dict[int, int]:
    """파견 직전 실제 주기장 수량. DB 장애 때만 틱 스냅샷으로 폴백한다."""
    try:
        parked = db_reader.get_levels_db(email)
    except Exception:  # noqa: BLE001 - HTTP 파견이 최종 보유량 검증
        parked = fallback_levels
    return {
        RECYCLER: max(0, int(parked.get(RECYCLER, 0) or 0)),
        SMALL_CARGO: max(0, int(parked.get(SMALL_CARGO, 0) or 0)),
        LARGE_CARGO: max(0, int(parked.get(LARGE_CARGO, 0) or 0)),
    }


def _queue_recycler_build(client, email: str) -> str | None:
    """조선소 큐에 리사이클러를 소량(RECYCLER_BUILD_BATCH) 건조. 이미 큐에 있으면 스킵.

    선행조건/데브리 가치 판정은 호출부에서 마친 상태로 들어온다. build_ships 성공 시
    로그 문자열, 실패/중복이면 None. 어떤 예외도 올리지 않는다.
    """
    if pending_recycler_build(email):
        return None  # 이미 조선소 큐에 리사이클러 있음 → 중복 큐잉 방지
    try:
        res = client.build_ships(RECYCLER, RECYCLER_BUILD_BATCH)
    except Exception:  # noqa: BLE001 - 건조 실패는 다음 틱 재시도
        return None
    ok = isinstance(res, dict) and (
        res.get("status") == "success" or res.get("success") is True
    )
    if ok:
        return f"raid BUILD 리사이클러 x{RECYCLER_BUILD_BATCH} (데브리 수거용)"
    return None


def _harvest_step(
    client, email: str, debris_targets: list[dict], have_rec: int,
    state: dict, now: float, *, have_sc: int = 0, have_lc: int = 0,
    ignore_cooldown_coords: set[tuple[int, int, int]] | None = None,
    cooldown_sec: int = HARVEST_COOLDOWN_SEC,
) -> str | None:
    """수거 후보를 훑어 첫 파견 가능한 데브리에 혼합 수확 함대를 보낸다.

    쿨다운(HARVEST_COOLDOWN_SEC) 안이면 건너뛴다. 파견 성공 시 로그 문자열, 없으면 None.
    """
    bypass = ignore_cooldown_coords or set()
    for t in debris_targets:
        coord = t["coord"]
        if (coord not in bypass
                and now - _last(state, coord, "harvest") < max(0, int(cooldown_sec))):
            continue
        send = _recyclers_for_value(t["value"], have_rec)
        if send <= 0:
            continue
        support = _harvest_support_cargo(t["value"], send, have_sc, have_lc)
        if not support:
            return (f"raid HARVEST WAIT {coord[0]}:{coord[1]}:{coord[2]} "
                    "(동행 화물선 0, 귀환/건조 대기)")
        res = fleet.harvest(client, coord, recyclers=send, support_ships=support)
        if res.get("ok"):
            _mark(state, coord, "harvest", now)
            _save_state(email, state)
            sc = support.get(SMALL_CARGO, 0)
            lc = support.get(LARGE_CARGO, 0)
            cargo_note = f", 소형화물{sc}+대형화물{lc}" if support else ""
            return (f"raid HARVEST {coord[0]}:{coord[1]}:{coord[2]} "
                    f"(리사이클러{send}{cargo_note}, 데브리~{t['value']})")
        # check-target 거부(고스트 필드 소진 등) → 다음 후보로
    return None


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------
def raid_tick(
    client, email: str, levels: dict[int, int],
    *, min_loot: int | None = None, max_active_missions: int | None = None,
    battle_mode: str = BATTLE_SIM_MODE,
    allow_spy: bool = True,
    allow_attack: bool = True,
    allow_harvest: bool = True,
    controls: dict | None = None,
) -> str | None:
    """공격형 봇의 함대 행동 1건. 로그 문자열 또는 None(행동 없음) 반환.

    levels 는 miner_bot 이 이미 조회한 건물/함선 수량 dict(함선 id 포함).
    이 함수는 어떤 경우에도 예외를 올리지 않는다(루프 보호).

    min_loot/max_active_missions 는 AI 정책(policy_engine)이 넘기는 선택적 오버라이드다.
    None 이면 모듈 기본값(MIN_EXPECTED_LOOT/MAX_ACTIVE_MISSIONS)을 쓴다 — 규칙봇 호출부는
    이 인자를 주지 않으므로 동작이 그대로 유지된다. 전역이 아니라 인자로만 전달돼 스레드
    안전하다. 사람 계정(USER_EXCLUDE_SYSTEMS) 제외는 오버라이드와 무관한 하드가드로 유지.

    battle_mode 기본값은 "log"다. "canary_live"는 운영자가 특정 계정을 명시적으로 한 번
    실행할 때만 허용하며, 내부에서 다행성/함대슬롯/안전핀을 다시 확인한다. 다른 문자열은
    log로 닫힌다. 전역 live 스위치나 이메일 allowlist는 의도적으로 없다.
    """
    # 지난 log-mode 예측과 도착한 battle_report를 틱 시작에 대조한다. 관측 배선의 DB/파일
    # 오류가 레이더 행동을 막아서는 안 되므로 raid_tick 전체 예외 가드와 별도로 흡수한다.
    try:
        battle_guard.reconcile(email)
    except Exception:  # noqa: BLE001 - 사후 관측은 best-effort
        pass
    try:
        return _raid_tick_inner(
            client, email, levels,
            min_loot=min_loot, max_active_missions=max_active_missions,
            battle_mode=battle_mode,
            allow_spy=allow_spy,
            allow_attack=allow_attack,
            allow_harvest=allow_harvest,
            controls=controls,
        )
    except Exception as exc:  # noqa: BLE001 - 레이더 버그가 빌드틱/루프를 깨지 않게
        return f"raid err {type(exc).__name__}: {exc}"


def _raid_tick_inner(
    client, email: str, levels: dict[int, int],
    *, min_loot: int | None = None, max_active_missions: int | None = None,
    battle_mode: str = BATTLE_SIM_MODE,
    allow_spy: bool = True,
    allow_attack: bool = True,
    allow_harvest: bool = True,
    controls: dict | None = None,
) -> str | None:
    min_loot = MIN_EXPECTED_LOOT if min_loot is None else min_loot
    max_active = MAX_ACTIVE_MISSIONS if max_active_missions is None else max_active_missions
    control_map = controls if isinstance(controls, dict) else {}
    limits = control_map.get("limits") if isinstance(control_map.get("limits"), dict) else {}
    targeting = (
        control_map.get("targeting")
        if isinstance(control_map.get("targeting"), dict)
        else {}
    )
    scan_span = int(limits.get("scan_span", NEIGHBOR_SPAN))
    spy_global_cooldown = max(
        HARD_MIN_SPY_GLOBAL_COOLDOWN_SEC,
        int(limits.get("spy_global_cooldown_sec", SPY_GLOBAL_COOLDOWN_SEC)),
    )
    spy_target_cooldown = max(
        HARD_MIN_SPY_TARGET_COOLDOWN_SEC,
        int(limits.get("spy_target_cooldown_sec", SPY_COOLDOWN_SEC)),
    )
    attack_target_cooldown = max(
        HARD_MIN_ATTACK_TARGET_COOLDOWN_SEC,
        int(limits.get("attack_target_cooldown_sec", ATTACK_COOLDOWN_SEC)),
    )
    bashing_max_attacks = min(
        HARD_MAX_BASH_ATTACKS,
        max(1, int(limits.get("bashing_max_attacks", BASH_MAX_ATTACKS))),
    )
    fleet_risk_percent = float(
        limits.get("fleet_risk_percent", PREFLIGHT_FLEET_VALUE_RISK_PERCENT)
    )
    min_fleet_value_limit = int(
        limits.get("min_fleet_value_limit", PREFLIGHT_MIN_FLEET_VALUE_LIMIT)
    )
    max_attack_eta_sec = int(limits.get("max_attack_eta_sec", PREFLIGHT_MAX_ETA_SEC))
    configured_harvest_max = max(
        0, int(limits.get("harvest_max_concurrent", HARVEST_MAX_CONCURRENT))
    )
    # 0은 "수확 금지"가 아니라 인공 동시 수확 상한 해제다. 실제 상한은 같은
    # 계정의 서버 함대 슬롯(max_active)이므로, full LLM이 0을 선택해도 가능한
    # 모든 남는 슬롯에서 데브리를 회수할 수 있다.
    harvest_max_concurrent = _harvest_concurrency_limit(
        configured_harvest_max, max_active
    )
    harvest_cooldown_sec = max(
        0, int(limits.get("harvest_cooldown_sec", HARVEST_COOLDOWN_SEC))
    )
    harvest_reserved_raid_slots = max(
        0,
        int(
            limits.get(
                "harvest_reserved_raid_slots", HARVEST_RESERVED_RAID_SLOTS
            )
        ),
    )
    loot_percent = raid_loot_percent(email)
    allow_strong_targets = bool(targeting.get("allow_strong_targets", True))
    allow_defended_targets = bool(targeting.get("allow_defended_targets", False))
    priority_targets: set[tuple[int, int, int]] = set()
    for item in targeting.get("priority_targets", []):
        if not isinstance(item, str):
            continue
        try:
            coord = tuple(int(part) for part in item.split(":"))
        except ValueError:
            continue
        if len(coord) == 3:
            priority_targets.add(coord)
    excluded_raw = targeting.get("excluded_systems")
    excluded_systems = None
    if isinstance(excluded_raw, list):
        excluded_systems = {
            (int(item[0]), int(item[1]))
            for item in excluded_raw
            if isinstance(item, (list, tuple)) and len(item) == 2
        }
    have_lf = levels.get(LIGHT_FIGHTER, 0)
    have_hf = levels.get(HEAVY_FIGHTER, 0)
    have_sc = levels.get(SMALL_CARGO, 0)
    have_lc = levels.get(LARGE_CARGO, 0)
    have_ep = levels.get(ESPIONAGE_PROBE, 0)
    have_rec = levels.get(RECYCLER, 0)
    # 리사이클러가 있으면 데브리 회수 여력이 있다고 보고 전액, 없으면 크게 할인.
    debris_factor = 1.0 if have_rec > 0 else DEBRIS_NO_RECYCLER_FACTOR

    # 1) 함대·리사이클러 모두 없음 → 스킵 (리사이클러만 있어도 수거 가능하므로 포함)
    if have_lf + have_hf + have_sc + have_lc + have_ep + have_rec == 0:
        return None

    active, attacking, harvesting = _active_fleet_info(client)
    now = time.time()
    state = _load_state(email)
    _sync_spy_loss_messages(email, state, now)

    # 이번 틱의 행동 로그. 리사이클러 건조(조선소 큐)는 함대 행동과 병행되므로 누적한다.
    notes: list[str] = []

    # live는 함수 호출에서 명시한 canary만 가능하다. 다행성/슬롯 조건이나 안전핀 중 하나라도
    # 실패하면 계산·집행 모두 log + 기존 방어0 게이트로 즉시 돌아간다.
    requested_canary = battle_mode == BATTLE_SIM_CANARY_MODE
    live_ready = requested_canary and _canary_live_ready(email, levels)
    safety_tripped = requested_canary and battle_sim.is_tripped(email)
    # preflight를 켠 동안은 두 틱 상태머신을 우회하는 기존 canary live도 금지.
    # 현재 preflight의 유일 집행 계약은 아래 defense0 경로다.
    preflight_blocks_canary = requested_canary and RAID_ATTACK_PREFLIGHT_ENABLED
    live_enabled = bool(live_ready and not safety_tripped and not preflight_blocks_canary)
    sim_mode = "live" if live_enabled else BATTLE_SIM_MODE
    if preflight_blocks_canary:
        notes.append("[battle_sim/canary_live] BLOCKED (2단계 preflight 우선; log 폴백)")
    elif requested_canary and not live_ready:
        notes.append("[battle_sim/canary_live] BLOCKED (다행성+함대슬롯2 필요; log 폴백)")
    elif safety_tripped:
        notes.append("[battle_sim/canary_live] SAFETY TRIPPED (방어0 게이트 폴백)")

    def _done(note: str | None) -> str | None:
        if note:
            attack_funnel.record_result(
                email,
                note,
                active_fleets=active,
                max_active_missions=max_active,
                allow_attack=allow_attack,
                allow_spy=allow_spy,
                allow_defended_targets=allow_defended_targets,
            )
            notes.append(note)
        return " | ".join(notes) if notes else None

    # own 좌표는 필요할 때 한 번만 조회해 재사용한다(데브리 조회·이웃 스캔 공용). 리사이클러
    # 관련 조회가 필요 없고 슬롯이 꽉 찬 흔한 경우엔 아예 조회하지 않아 부하가 늘지 않는다.
    _own_cache: list = []

    def _get_own():
        if not _own_cache:
            _own_cache.append(own_coords(client))
        return _own_cache[0]

    # 데브리 타겟(건조 게이트 + 수거 공용). 리사이클러가 있거나(수거) 선행조건을 갖춘
    # 경우(건조 판단)에만 좌표·DB 를 조회한다 — 초기 레이더는 조회 부하도 없다.
    prereq_ok = _recycler_prereqs_met(levels)
    debris_targets: list[dict] = []
    if have_rec > 0 or prereq_ok:
        own = _get_own()
        if own is not None:
            debris_targets = find_debris_targets(
                own[0], own[1], own, span=max(0, scan_span)
            )

    # ── 방어 매복: 적 공격 ETA까지 전투함·리사이클러·화물을 주기장에 보존 ──
    # 공격 도착 전에는 기존 데브리가 보여도 수거를 보내지 않는다. 도착 후 공격 row가
    # 처리되면 예약 좌표의 새 데브리를 일반 약탈보다 먼저 수거한다.
    own = _get_own()
    incoming = incoming_attack_to_coord(email, own, now=now) if own is not None else None
    if incoming is not None:
        if _counter_harvest_reservation(state) != {
            "mission_id": incoming["mission_id"],
            "coord": incoming["coord"],
            "arrival_ts": incoming["arrival_ts"],
        }:
            _set_counter_harvest_reservation(state, incoming)
            _save_state(email, state)

    # ── 리사이클러 커리큘럼: 데브리 수거 가치가 있고 미보유면 조선소 큐에 건조 ──
    # 조선소 큐는 함대 슬롯과 무관하므로 슬롯 게이트 앞에서 처리한다(수거 여력 선확보).
    if have_rec == 0 and prereq_ok and (debris_targets or incoming is not None):
        bn = _queue_recycler_build(client, email)
        if bn:
            notes.append(bn)

    if incoming is not None:
        coord = incoming["coord"]
        return _done(
            f"raid COUNTER HOLD {coord[0]}:{coord[1]}:{coord[2]} "
            f"(ETA{incoming['eta_sec']}s, 방어함대+수확함대 예약)"
        )

    reservation = _counter_harvest_reservation(state)
    if reservation is not None:
        reserved_coord = reservation["coord"]
        expires_at = reservation["arrival_ts"] + COUNTER_HARVEST_GRACE_SEC
        if reserved_coord != own or now > expires_at:
            _clear_counter_harvest_reservation(state)
            _save_state(email, state)
        else:
            counter_target = next(
                (target for target in debris_targets if target["coord"] == reserved_coord),
                None,
            )
            if (now >= reservation["arrival_ts"] and counter_target is not None
                    and allow_harvest and harvesting < harvest_max_concurrent
                    and active < max_active):
                parked = _parked_harvest_inventory(email, levels)
                hn = _harvest_step(
                    client,
                    email,
                    [counter_target],
                    parked[RECYCLER],
                    state,
                    now,
                    have_sc=parked[SMALL_CARGO],
                    have_lc=parked[LARGE_CARGO],
                    ignore_cooldown_coords={reserved_coord},
                    cooldown_sec=harvest_cooldown_sec,
                )
                if hn:
                    if "HARVEST WAIT" not in hn:
                        _clear_counter_harvest_reservation(state)
                        _save_state(email, state)
                    return _done(hn.replace("raid HARVEST", "raid COUNTER HARVEST", 1))
            eta = max(0, int(math.ceil(reservation["arrival_ts"] - now)))
            return _done(
                f"raid COUNTER WAIT {reserved_coord[0]}:{reserved_coord[1]}:{reserved_coord[2]} "
                f"(ETA{eta}s, 전투 정산/데브리 대기)"
            )

    # 과공격 방지: 진행 중 함대가 많으면 이번 틱 함대 행동(공격/수거/정찰)은 쉰다.
    if active >= max_active:
        return _done(f"raid idle (진행중 함대 {active}건)")

    own = _get_own()
    if own is None:
        return _done("raid idle (좌표 조회 실패)")
    galaxy, system, _ = own

    candidates = scan_targets(
        client,
        galaxy,
        system,
        own,
        span=scan_span,
        allow_strong_targets=allow_strong_targets,
        excluded_systems=excluded_systems,
        priority_targets=priority_targets,
    )

    # 모든 후보를 계정별로 분산한다. 명시 표적도 그 그룹 안에서 포화도 순환을 지킨다.
    if candidates:
        candidates = _prioritize_targets(
            candidates, state, priority_targets, email=email, now=now
        )

    # 최근 정찰기 전멸 좌표는 백오프가 끝날 때까지 정찰/공격 후보에서 통째로 뺀다.
    # fresh report를 빌미로 preflight가 재정찰을 우회하는 것도 함께 막는다.
    if candidates:
        candidates = [
            row for row in candidates
            if not _spy_quarantined(state, row["coord"], now)
        ]

    # 활성 NAP는 LLM의 설명문이 아니라 집행 계층에서 강제한다. 외교 저장소/DB가
    # 일시 실패하면 기존 공격 안전핀은 유지하되 이번 필터만 best-effort로 건너뛴다.
    try:
        nap_player_ids = diplomacy.blocked_player_ids(email, now=now)
    except Exception:  # noqa: BLE001 - diplomacy read failure must not break universe loop
        nap_player_ids = set()
    if nap_player_ids and candidates:
        candidates = [
            row for row in candidates
            if _row_player_id(row) not in nap_player_ids
        ]

    # 배싱 가드: 24h 내 공격 상한(BASH_MAX_ATTACKS)에 도달한 타겟은 이번 바퀴 후보에서 제외.
    # (정찰 포함 통째로 제외 — 더 못 칠 타겟을 정찰할 이유도 없다.)
    if candidates:
        bashed = sum(
            1 for r in candidates
            if _bashing_limited(
                state,
                r["coord"],
                now,
                bashing_max_attacks,
                player_id=_row_player_id(r),
            )
        )
        if bashed:
            candidates = [
                r for r in candidates
                if not _bashing_limited(
                    state,
                    r["coord"],
                    now,
                    bashing_max_attacks,
                    player_id=_row_player_id(r),
                )
            ]

    # 공격 후보가 있을 때만 최신 정찰 intel을 DB 1회로 읽는다. compact HTML의 하드코딩
    # Defense:0은 절대 보지 않는다. 실패 시 {} → 공격 불가, 정찰/대기로 안전 degrade.
    reports = read_spy_reports(email) if candidates else {}

    # 전투 시뮬 관측은 실제 공격 판단과 분리한다. preflight가 활성인 운영 경로에서는
    # cheap gate를 통과해 지금 재정찰을 보낼 수 있는 후보만 남기고, 결정적 1/N budget에
    # 든 틱만 shadow sizing을 계산한다. pending preflight의 안전-critical 계산은 아래
    # _attack_preflight_step이 항상 수행하므로 이 관측 budget의 영향을 받지 않는다.
    shadow = None
    shadow_candidates = candidates
    shadow_allowed = True
    if RAID_ATTACK_PREFLIGHT_ENABLED and not live_enabled:
        shadow_candidates = _preflight_shadow_candidates(
            candidates,
            reports,
            state,
            now,
            levels,
            have_lf=have_lf,
            have_hf=have_hf,
            have_sc=have_sc,
            have_ep=have_ep,
            min_loot=min_loot,
            debris_factor=debris_factor,
            attack_cooldown_sec=attack_target_cooldown,
            spy_cooldown_sec=spy_target_cooldown,
            allow_defended_targets=allow_defended_targets,
            loot_percent=loot_percent,
        )
        shadow_allowed = allow_spy and _shadow_budget_allows(
            email, shadow_candidates, reports, now
        )
    if (allow_attack and shadow_allowed and shadow_candidates
            and (have_lf > 0 or have_hf > 0)):
        shadow = _battle_sim_shadow(
            shadow_candidates, reports, levels,
            have_lf=have_lf, have_hf=have_hf, have_sc=have_sc,
            have_rec=have_rec, min_loot=min_loot, debris_factor=debris_factor,
            mode=sim_mode, own=own, loot_percent=loot_percent,
        )
        if shadow and shadow.get("log"):
            notes.append(shadow["log"])
            # 사이징 shadow 로그는 기존 battle_sim 로그와 별개 줄로 나란히 남긴다(관측 전용).
            if shadow.get("sizing_log"):
                notes.append(shadow["sizing_log"])

    # ── 1순위-A: 명시적 live canary ──
    # 완전 정보로 시뮬한 **바로 그 좌표·바로 그 함대**가 recommend=True일 때만 보낸다.
    # 다른 후보를 루프 돌며 대체하지 않아 예측과 실전 타겟이 어긋날 여지를 없앤다.
    if (allow_attack and live_enabled and candidates and shadow
            and isinstance(shadow.get("decision"), dict)
            and shadow["decision"].get("recommend") is True):
        coord = shadow.get("target")
        rep = reports.get(coord) if isinstance(coord, tuple) else None
        ships = shadow.get("attacker")
        full_intel = (
            isinstance(rep, dict) and rep.get("fresh") is True
            and isinstance(rep.get("res_sum"), int)
            and rep.get("ships") is not None
            and rep.get("defense_units") is not None
            and rep.get("research") is not None
        )
        if (full_intel and isinstance(ships, dict) and ships
                and now - _last(state, coord, "attack") >= attack_target_cooldown):
            loot = expected_raid_loot(rep, loot_percent)
            debris = int(all_ship_debris_value(rep.get("ships") or {}) * debris_factor)
            if loot + debris >= min_loot:
                res = fleet.attack(client, coord, ships)
                if res.get("ok"):
                    try:
                        battle_guard.record_prediction(
                            email, coord, ships, shadow["decision"],
                            mode="live", sent_at=now,
                        )
                    except Exception:  # noqa: BLE001 - 기록 실패가 파견을 뒤집지 않음
                        pass
                    target_row = next(
                        (row for row in candidates if row.get("coord") == coord),
                        {},
                    )
                    _record_attack(
                        state,
                        coord,
                        now,
                        player_id=_row_player_id(target_row),
                    )
                    _save_state(email, state)
                    sc = ships.get(SMALL_CARGO, 0)
                    fg = ships.get(LIGHT_FIGHTER, ships.get(HEAVY_FIGHTER, 0))
                    return _done(
                        f"raid ATTACK[CANARY] {coord[0]}:{coord[1]}:{coord[2]} "
                        f"(전투기{fg}+화물{sc}, 예상약탈~{loot}+데브리~{debris}, full-intel)"
                    )

    # ── 1순위-B: 기본 log 또는 안전핀 폴백의 기존 방어0 게이트 ──
    # live canary가 활성화된 틱에는 이 느슨한 대체 타겟 경로를 타지 않는다.
    if (allow_attack and allow_spy and RAID_ATTACK_PREFLIGHT_ENABLED and not live_enabled and candidates
            and (have_lf > 0 or have_hf > 0)):
        preflight_note = _attack_preflight_step(
            client, email, levels, state, now, own, candidates, reports,
            have_lf=have_lf, have_hf=have_hf, have_sc=have_sc,
            have_ep=have_ep, have_rec=have_rec, min_loot=min_loot,
            debris_factor=debris_factor,
            attack_cooldown_sec=attack_target_cooldown,
            spy_cooldown_sec=spy_target_cooldown,
            fleet_risk_percent=fleet_risk_percent,
            min_fleet_value_limit=min_fleet_value_limit,
            max_attack_eta_sec=max_attack_eta_sec,
            max_active_missions=max_active,
            allow_defended_targets=allow_defended_targets,
            loot_percent=loot_percent,
        )
        if preflight_note is not None:
            return _done(preflight_note)

    if (allow_attack and not live_enabled and candidates
            and not RAID_ATTACK_PREFLIGHT_ENABLED
            and (have_lf > 0 or have_hf > 0)):
        for row in candidates:
            coord = row["coord"]
            rep = reports.get(coord)
            if not _safe_zero_defense_report(rep):
                # defense NULL·손상·stale·실제 방어>0은 모두 공격 불가.
                continue
            # 약탈(화물로 실어오는 몫)과 데브리(격침 함선 → 리사이클러 수확 몫)를 분리한다.
            # 종합 기댓값으로 공격 여부를 판정하되, 화물 규모는 약탈량만으로 잡는다
            # (데브리는 화물이 아니라 리사이클러 수확 미션으로만 회수되기 때문).
            loot = expected_raid_loot(rep, loot_percent)
            debris = int(ship_debris_value(rep.get("ships") or {}) * debris_factor)
            value = loot + debris
            if value < min_loot:
                continue
            if now - _last(state, coord, "attack") < attack_target_cooldown:
                continue
            ships = _attack_force(loot, have_lf, have_hf, have_sc, have_lc)
            if not ships:
                continue
            res = fleet.attack(client, coord, ships)
            if res.get("ok"):
                # log 권고는 집행에 쓰지 않는다. 같은 타겟·함대 full-intel 예측만 관측 저장.
                if (shadow and shadow.get("target") == coord
                        and shadow.get("attacker") == ships
                        and isinstance(shadow.get("decision"), dict)):
                    try:
                        battle_guard.record_prediction(
                            email, coord, ships, shadow["decision"],
                            mode="log", sent_at=now,
                        )
                    except Exception:  # noqa: BLE001 - 관측 기록 실패가 파견 성공을 뒤집지 않음
                        pass
                _record_attack(
                    state, coord, now, player_id=_row_player_id(row)
                )
                _save_state(email, state)
                sc = ships.get(SMALL_CARGO, 0)
                fg = ships.get(LIGHT_FIGHTER, ships.get(HEAVY_FIGHTER, 0))
                dtag = f"+데브리~{debris}" if debris else ""
                return _done(f"raid ATTACK {coord[0]}:{coord[1]}:{coord[2]} "
                             f"(전투기{fg}+화물{sc}, 예상약탈~{loot}{dtag}, 방어0)")
            # check-target 거부 등 → 다음 후보로

    # ── 2순위: 데브리 수거 (공격 다음) ──
    # 파견 후에도 최소 1개 raid 슬롯을 남겨 새 full-intel 공격/정찰을 굶기지 않는다.
    harvest_slot_safe = _harvest_slot_safe(
        active, max_active, harvest_reserved_raid_slots
    )
    if (allow_harvest and have_rec > 0 and harvesting < harvest_max_concurrent and debris_targets
            and harvest_slot_safe):
        parked = _parked_harvest_inventory(email, levels)
        hn = _harvest_step(
            client,
            email,
            debris_targets,
            parked[RECYCLER],
            state,
            now,
            have_sc=parked[SMALL_CARGO],
            have_lc=parked[LARGE_CARGO],
            cooldown_sec=harvest_cooldown_sec,
        )
        if hn and "HARVEST WAIT" not in hn:
            return _done(hn)

    # ── 3순위: 동적 full-intel 정찰 ──
    if allow_spy and candidates and have_ep > 0:
        global_last = float(state.get("last_spy", 0.0) or 0.0)
        if now - global_last < spy_global_cooldown:
            return _done("raid idle (계정 정찰 쿨다운)")
        waiting_for = 0
        for row in candidates:
            coord = row["coord"]
            if now - _last(state, coord, "spy") < spy_target_cooldown:
                continue
            desired = desired_spy_probes(
                reports.get(coord), levels.get(ESPIONAGE_TECH, 0),
                _last_spy_probes(state, coord),
            )
            if have_ep < desired:
                waiting_for = max(waiting_for, desired)
                continue  # 목표 수량을 모아 저정보 1~3기 정찰 반복을 없앤다.
            res = fleet.spy(client, coord, probes=desired)
            if res.get("ok"):
                _mark_spy(state, coord, now, desired)
                _save_state(email, state)
                tag = "장기비활성" if row.get("long_inactive") else (
                    "비활성" if row.get("inactive") else "활성")
                return _done(f"raid SPY {coord[0]}:{coord[1]}:{coord[2]} "
                             f"(정찰기{desired}, full-intel 목표, {tag})")
        if waiting_for:
            return _done(f"raid idle (정찰기 축적 {have_ep}/{waiting_for})")

    # 무정찰 ATTACK* 폴백은 제거했다. 정찰기가 없거나 DB intel을 검증할 수 없으면 공격하지
    # 않는 것이 유일한 안전 폴백이다(비활성 표시는 방어 0을 보장하지 않는다).

    return _done("raid idle (조건 미충족)")
