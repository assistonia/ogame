"""AI 하이브리드 정책 집행 엔진.

설계 요지(팀리드 지시):
  AI(codex)는 드물게 JSON '정책'만 출력하고, 규칙봇 엔진이 그 정책을 읽어 매 틱
  집행한다. 이 모듈은 후자(집행)를 담당한다.

  policy_tick(email, password) 는
    1) policies/<계정>.json 정책을 방어적으로 로드하고(없거나 깨졌거나 TTL 만료면
       계정별 고유 성격 폴백 — AI 가 죽어도 개성을 유지하며 성장한다),
    2) 정책의 personality 로 기존 규칙봇 성장 로직(miner_bot.plan_primary/plan_secondary,
       raider/turtle/colony 훅)을 그대로 재사용하되,
    3) build/research/ship '우선순위'를 정책이 오버라이드하도록 후보 목록을 재정렬한다.

집행은 miner_bot / personality 의 기존 함수를 재사용한다(그 파일들은 실가동 중이라
수정을 최소화). 정책은 공용 성장 후보의 순서를 바꾸되 연구는 유한한 공용 목표 안에서만,
추가 함선은 경제 행동 성공+최소 식민 완료 뒤에만 허용한다. plan_primary/plan_secondary 가
적용한 선행조건·에너지·저장고·시설 진행 안전장치는 정책보다 우선한다.

스레드 안전:
  run_universe 는 --parallel 12 ThreadPoolExecutor 로 이 함수를 동시 호출한다. 따라서
  이 모듈은 전역 상태를 변경하지 않는다(정책은 매 호출마다 파일에서 새로 읽고, raider/
  turtle 파라미터 오버라이드는 전역이 아니라 함수 인자로만 전달한다).
"""
from __future__ import annotations

import datetime
import hashlib
import inspect
import json
import pathlib
from collections.abc import Mapping
from typing import Any

from bots.shared.client import OGameClient
from bots.shared import db_reader
from bots.shared import planets as planets_mod
from bots.ai_agents import action_feedback, action_reconciler, diplomacy
from bots.shared.game_objects import (
    BUILDINGS,
    FACILITIES,
    NAME_TO_ID,
    SHIPS,
    SOLAR_SATELLITE,
    name_of,
    object_cost,
    ship_cost,
)
from bots.rule_based import colony
from bots.rule_based import colony_dev
from bots.rule_based import empire_scheduler
from bots.rule_based import miner_bot
from bots.rule_based import moon_development
from bots.rule_based import post_actions
from bots.rule_based import production_capacity
from bots.rule_based import strategic_growth
from bots.rule_based.tick_context import build_tick_context
from bots.ai_agents.action_catalog import (
    AI_STRUCTURED_CONTEXT_KEY,
    action_enabled,
    clean_action_plan,
    effective_action_plan,
)
from bots.rule_based.personality import (
    MINE_BUILDINGS,
    PERSONALITIES,
    STORAGE_BUILDINGS,
    plan_primary,
    plan_secondary,
    prereqs_met,
)

POLICY_DIR = pathlib.Path(__file__).resolve().parent / "policies"

# 정책이 personality 를 안 주거나 모르는 값을 줄 때의 안전 기본값.
DEFAULT_PERSONALITY = "miner"

# 정책 파일이 잠시 만료/손상돼도 등록 AI 고유 캐릭터는 바뀌지 않는다. 종전에는 모든
# 폴백이 miner라서, 한 시간짜리 정책이 다음 AI 갱신 전에 만료되면 레이더/탐험가까지
# 채굴러로 변했다(최근 6시간 실측 25틱). 미등록 테스트/추가 계정만 miner를 쓴다.
AI_PERSONALITY_FALLBACK: dict[str, str] = {
    "ai01": "raider",
    "ai02": "turtle",
    "ai03": "miner",
    "ai04": "explorer",
    "ai05": "hybrid",
    "ai06": "raider",
    "ai07": "miner",
    "ai08": "miner",
    "ai09": "hybrid",
    "ai10": "raider",
    "pirate01": "raider",
    "explorer01": "explorer",
}

# 정책 지정 함선 배치의 틱당 상한(폭주 방지). ship_priority 의 batch 가 이보다 크면 자른다.
MAX_SHIP_BATCH = 20

POLICY_PLANS_VERSION = 1
MAX_STRUCTURED_PLAN_ENTRIES = 64
MAX_STRUCTURED_RESOURCE_AMOUNT = 1_000_000_000_000
MAX_STRUCTURED_PRODUCTION_AMOUNT = 1_000_000
MAX_STRUCTURED_OBJECT_LEVEL = 100

# LLM이 실제 집행을 통제하는 aiNN 계정은 기본 full autonomy다. 자율성은 전략과
# 자원배분을 고르는 권한이지, 반복 정찰·배싱 안전핀을 해제하는 권한이 아니다.
# 아래 값은 정책이 생략되거나 0을 쓰더라도 raider 집행기가 다시 강제한다.
FULL_AUTONOMY_LIMITS: dict[str, int | float | bool] = {
    "scan_span": 2,
    "spy_global_cooldown_sec": 600,
    "spy_target_cooldown_sec": 1200,
    "attack_target_cooldown_sec": 1500,
    "bashing_max_attacks": 6,
    "fleet_risk_percent": 100,
    "min_fleet_value_limit": 0,
    "max_attack_eta_sec": 0,
    "max_planets": 99,
    "reserve_fleet_slots": 0,
    "harvest_max_concurrent": 0,
    "harvest_cooldown_sec": 0,
    "harvest_reserved_raid_slots": 0,
    "max_active_fleetsaves": 0,
    "colonize_cooldown_sec": 0,
    "use_server_expedition_slots": True,
    "expedition_risk_percent": 100,
    "expedition_total_exposure_percent": 100,
}
FULL_AUTONOMY_TARGETING: dict[str, object] = {
    "allow_strong_targets": True,
    "allow_defended_targets": True,
    "excluded_systems": [],
    # 정적 사용자 좌표를 모든 AI에 복제하지 않는다. 타겟은 각 계정이 관측한
    # 은하 후보를 최근 정찰/공격 포화도와 계정별 안정 난수로 순환한다.
    "priority_targets": [],
}

HARD_LIMIT_RANGES: dict[str, tuple[int, int | None]] = {
    "spy_global_cooldown_sec": (600, None),
    "spy_target_cooldown_sec": (1200, None),
    "attack_target_cooldown_sec": (1500, None),
    # 배싱은 "최소 간격"이 아니라 24시간 최대 횟수다. 정책은 더 보수적으로
    # 낮출 수 있지만 6보다 높이거나 0으로 해제할 수 없다.
    "bashing_max_attacks": (1, 6),
}

PERSONA_DOCTRINE_ENVELOPES: dict[str, frozenset[str]] = {
    "raider": frozenset({"cargo_raider", "debris_hunter", "balanced"}),
    "turtle": frozenset({"debris_hunter", "balanced"}),
    "miner": frozenset({"cargo_raider", "balanced"}),
    "explorer": frozenset({"expeditioneer", "balanced"}),
    "hybrid": frozenset(strategic_growth.DOCTRINES),
}
PERSONA_DOCTRINE_DEFAULTS: dict[str, str] = {
    "raider": "debris_hunter",
    "turtle": "balanced",
    "miner": "balanced",
    "explorer": "expeditioneer",
    "hybrid": "balanced",
}

# apply_ship_priority를 직접 부르는 옛 코드/단위 테스트는 계정 합계를 전달하지 않는다.
# "인자 생략"은 levels 하위호환, 명시적 None은 실가동 DB 조회 실패(fail-closed)로 구분한다.
_ACCOUNT_SHIP_TOTALS_UNSET = object()
DEATHSTAR_GOAL_ACCOUNTS = frozenset(f"ai{i:02d}" for i in range(1, 11))


# ---------------------------------------------------------------------------
# 정책 로드 (방어적 파싱)
# ---------------------------------------------------------------------------
def _account_name(email: str) -> str:
    """이메일에서 계정 localpart(ai01@bots.local -> ai01). 페르소나/상태와 동일 규칙."""
    return email.split("@", 1)[0]


def _policy_path(email: str) -> pathlib.Path:
    return POLICY_DIR / f"{_account_name(email)}.json"


def _is_expired(raw: dict) -> bool:
    """updated_at + ttl_hours 로 만료 여부. 둘 다 파싱 가능할 때만 판정.

    updated_at 이 없거나 파싱 불가면 신선도를 검증할 수 없으므로 만료로 보지 않는다
    (타임스탬프 오타 하나로 멀쩡한 정책을 버리지 않기 위함 — AI 감독이 어차피 갱신한다).
    """
    ttl = raw.get("ttl_hours")
    updated = raw.get("updated_at")
    if not isinstance(ttl, (int, float)) or ttl <= 0 or not isinstance(updated, str):
        return False
    try:
        ts = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(ts.tzinfo)
    return now > ts + datetime.timedelta(hours=float(ttl))


def _fallback_personality(email: str) -> str:
    """계정 고유 안전 성격. 알려진 AI는 페르소나를, 그 외 계정은 miner를 쓴다."""
    return AI_PERSONALITY_FALLBACK.get(_account_name(email), DEFAULT_PERSONALITY)


def persona_lock_target(
    email: str,
    previous_policy: dict | None = None,
) -> str | None:
    """성적표 정책 갱신 때 유지할 canonical personality를 결정한다.

    등록 aiNN은 ``AI_PERSONALITY_FALLBACK``이 유일한 정본이다. 이미 drift한 직전 정책을
    정본으로 삼으면 오류가 영구 고정되므로 보지 않는다. 미등록/test 계정만 직전 정책의
    유효 personality를 호환 fallback으로 사용하며, 그것도 없으면 lock하지 않는다.
    """
    canonical = AI_PERSONALITY_FALLBACK.get(_account_name(email))
    if canonical in PERSONALITIES:
        return canonical
    if isinstance(previous_policy, dict):
        previous = previous_policy.get("personality")
        if isinstance(previous, str) and previous in PERSONALITIES:
            return previous
    return None


def enforce_persona_lock(
    email: str,
    new_policy: dict,
    previous_policy: dict | None = None,
) -> tuple[dict, str | None]:
    """입력 dict를 보존하며 persona lock이 적용된 정책 사본과 target을 반환한다."""
    locked = dict(new_policy)
    target = persona_lock_target(email, previous_policy)
    if target is not None:
        locked["personality"] = target
    return locked, target


def _default_deathstar_goal(email: str) -> dict:
    managed = _account_name(email) in DEATHSTAR_GOAL_ACCOUNTS
    return {
        "mode": "evaluate" if managed else "pause",
        "reason": (
            "Evaluate as a secondary goal against sustainable growth and risk."
            if managed else "Deathstar goal is only managed for ai01..ai10."
        ),
        "planet_id": None,
        "budget": {
            "max_satellite_batch": 64,
            "satellite_horizon_seconds": 21_600,
            "max_deathstar_batch": 1,
            "deathstar_horizon_seconds": 604_800,
            "target_deathstars": 1,
            "max_resource_commitment_pct": 35,
            "reserve_resources": {
                "metal": 100_000,
                "crystal": 100_000,
                "deuterium": 50_000,
            },
        },
        "risk": {
            "allow_exposed_satellites": False,
            "max_exposed_satellites": 12_000,
            "min_nanite_level": 1,
            "satellites_without_nanite_limit": 1_000,
        },
    }


def _clean_deathstar_goal(value, email: str) -> dict:
    goal = _default_deathstar_goal(email)
    if not isinstance(value, dict):
        return goal
    if value.get("mode") in {"evaluate", "pursue", "pause"}:
        goal["mode"] = value["mode"]
    goal["reason"] = str(value.get("reason", goal["reason"]))[:300]
    planet_id = value.get("planet_id")
    if isinstance(planet_id, int) and not isinstance(planet_id, bool) and planet_id >= 1:
        goal["planet_id"] = planet_id
    budget = value.get("budget")
    if isinstance(budget, dict):
        for key, lower, upper in (
            ("max_satellite_batch", 1, 256),
            ("satellite_horizon_seconds", 1_800, 86_400),
            ("max_deathstar_batch", 1, 5),
            ("deathstar_horizon_seconds", 21_600, 604_800),
            ("target_deathstars", 1, 25),
            ("max_resource_commitment_pct", 1, 100),
        ):
            setting = budget.get(key)
            if isinstance(setting, int) and not isinstance(setting, bool):
                goal["budget"][key] = max(lower, min(upper, setting))
        reserves = budget.get("reserve_resources")
        if isinstance(reserves, dict):
            for resource in ("metal", "crystal", "deuterium"):
                amount = reserves.get(resource)
                if (
                    isinstance(amount, (int, float))
                    and not isinstance(amount, bool)
                    and amount >= 0
                ):
                    goal["budget"]["reserve_resources"][resource] = min(
                        1_000_000_000, int(amount)
                    )
    risk = value.get("risk")
    if isinstance(risk, dict):
        if isinstance(risk.get("allow_exposed_satellites"), bool):
            goal["risk"]["allow_exposed_satellites"] = risk["allow_exposed_satellites"]
        for key, lower, upper in (
            ("max_exposed_satellites", 1, 25_000),
            ("min_nanite_level", 0, 10),
            ("satellites_without_nanite_limit", 0, 25_000),
        ):
            setting = risk.get(key)
            if isinstance(setting, int) and not isinstance(setting, bool):
                goal["risk"][key] = max(lower, min(upper, setting))
    return goal


def _default_policy(source: str, email: str = "") -> dict:
    """정책 없음/깨짐/만료 시에도 계정 고유 성격으로 계속 성장."""
    full = _account_name(email) in AI_PERSONALITY_FALLBACK
    return {
        "personality": _fallback_personality(email),
        "autonomy": "full" if full else "guided",
        "action_plan": None,
        "build_priority": [],
        "research_priority": [],
        "ship_priority": [],
        "ship_strategy": {},
        "diplomacy": {"version": 1, "messages": []},
        "aggression": {},
        "fleetsave": {"mode": "adaptive"},
        "defense_ratio": None,
        "defense_strategy": {
            "mode": "adaptive",
            "stored_loot_multiplier": 0.5,
            "production_hours": 6.0,
            "rebuild_on_attack": True,
        },
        "energy_target": 0,
        "secondary_goals": {"deathstar": _default_deathstar_goal(email)},
        "targeting": dict(FULL_AUTONOMY_TARGETING) if full else {},
        "limits": dict(FULL_AUTONOMY_LIMITS) if full else {},
        "note": "",
        "source": source,
    }


def load_policy(email: str) -> dict:
    """정책 파일을 방어적으로 로드해 정규화된 dict 를 반환한다.

    반환 dict 는 항상 아래 키를 갖는다(값은 검증됨):
      personality(str, PERSONALITIES 의 키), build_priority(list),
      research_priority(list), ship_priority(list[{"id","batch"}]),
      aggression(dict), defense_ratio(float|None), note(str), source(str).
    파일 없음/JSON 오류/dict 아님/TTL 만료 → 계정별 고유 성격 정책 폴백.
    """
    path = _policy_path(email)
    if not path.exists():
        return _ensure_registered_ai_plan_contract(_default_policy("no-file", email), email)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return _ensure_registered_ai_plan_contract(_default_policy("broken", email), email)
    if not isinstance(raw, dict):
        return _ensure_registered_ai_plan_contract(_default_policy("not-dict", email), email)
    if _is_expired(raw):
        return _ensure_registered_ai_plan_contract(_default_policy("expired", email), email)

    pol = _default_policy("policy", email)
    pol["note"] = str(raw.get("note", ""))[:200]
    # Server-issued policy identity must survive runtime normalization so every
    # deterministic action receipt can be attributed to the exact activation.
    for metadata_key in (
        "activation_id",
        "activated_at",
        "updated_at",
        "refreshed_at",
    ):
        metadata_value = raw.get(metadata_key)
        if isinstance(metadata_value, str) and metadata_value.strip():
            pol[metadata_key] = metadata_value.strip()

    autonomy = raw.get("autonomy")
    if autonomy in {"guided", "full"}:
        pol["autonomy"] = autonomy
    # 등록 AI는 전부 전략 행동을 선택할 수 있다. guided는 테스트/미등록 계정의 레거시
    # 호환 모드로만 남기고, 등록 AI에서는 성격별 하드 금지 목록으로 되돌아가지 않는다.
    if _account_name(email) in AI_PERSONALITY_FALLBACK:
        pol["autonomy"] = "full"
    if pol["autonomy"] == "full":
        pol["targeting"] = dict(FULL_AUTONOMY_TARGETING)
        pol["limits"] = dict(FULL_AUTONOMY_LIMITS)
    else:
        pol["targeting"] = {}
        pol["limits"] = {}
    strategy_version = raw.get("strategy_version")
    modern_policy = (
        (isinstance(strategy_version, int) and strategy_version >= 2)
        or _account_name(email) not in AI_PERSONALITY_FALLBACK
    )
    # v1 정책의 action_plan/고정 ship_priority는 안전 실험 시절의 상한을 영구 복제한다.
    # 첫 v2 갱신 전에는 전체 행동+동적 함대 전략으로 즉시 이행한다.
    pol["action_plan"] = clean_action_plan(raw.get("action_plan")) if modern_policy else None

    personality = raw.get("personality")
    if isinstance(personality, str) and personality in PERSONALITIES:
        pol["personality"] = personality

    pol["build_priority"] = _clean_id_list(raw.get("build_priority"))
    pol["research_priority"] = _clean_id_list(raw.get("research_priority"))
    pol["ship_priority"] = (
        _clean_ship_list(
            raw.get("ship_priority"),
            max_batch=None if pol["autonomy"] == "full" else MAX_SHIP_BATCH,
        )
        if modern_policy else []
    )
    pol["ship_strategy"] = strategic_growth.clean_ship_strategy(raw.get("ship_strategy"))
    allowed_doctrines = PERSONA_DOCTRINE_ENVELOPES.get(
        pol["personality"], frozenset(strategic_growth.DOCTRINES)
    )
    configured_doctrine = pol["ship_strategy"].get("doctrine")
    if configured_doctrine not in allowed_doctrines:
        # 원정 자체는 모든 성격이 계속 수행할 수 있다. 이 envelope는 압도적인
        # 원정 수익 때문에 함대 투자 교리까지 전부 expeditioneer로 붕괴하는 것만
        # 막아 레이더/터틀/광부의 전투·수송 정체성을 보존한다.
        fallback_doctrine = strategic_growth.individual_doctrine(
            email, pol["personality"]
        )
        pol["ship_strategy"]["doctrine"] = (
            fallback_doctrine
            if fallback_doctrine in allowed_doctrines
            else PERSONA_DOCTRINE_DEFAULTS.get(pol["personality"], "balanced")
        )
    pol["diplomacy"] = diplomacy.clean_diplomacy_policy(raw.get("diplomacy"))
    # The key is intentionally absent for legacy policies.  A present malformed
    # object is normalized to an explicit rejected/empty object, never to None.
    if "plans" in raw:
        pol["plans"] = clean_policy_plans(raw.get("plans"))
    secondary_goals = raw.get("secondary_goals")
    raw_deathstar = (
        secondary_goals.get("deathstar") if isinstance(secondary_goals, dict) else None
    )
    pol["secondary_goals"]["deathstar"] = _clean_deathstar_goal(raw_deathstar, email)

    agg = raw.get("aggression")
    if modern_policy and isinstance(agg, dict):
        clean_agg: dict = {}
        ml = agg.get("min_loot")
        if isinstance(ml, (int, float)) and ml >= 0:
            clean_agg["min_loot"] = int(ml)
        mam = agg.get("max_active_missions")
        if isinstance(mam, int) and mam >= 1:
            clean_agg["max_active_missions"] = mam
        pol["aggression"] = clean_agg

    fleetsave = raw.get("fleetsave")
    if isinstance(fleetsave, dict):
        mode = fleetsave.get("mode")
        if mode in {"off", "adaptive", "counter", "always"}:
            pol["fleetsave"]["mode"] = mode
        minimum = fleetsave.get("min_value")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum >= 0:
            pol["fleetsave"]["min_value"] = int(minimum)

    targeting = raw.get("targeting")
    if isinstance(targeting, dict):
        allow_strong = targeting.get("allow_strong_targets")
        if isinstance(allow_strong, bool):
            pol["targeting"]["allow_strong_targets"] = allow_strong
        allow_defended = targeting.get("allow_defended_targets")
        if isinstance(allow_defended, bool):
            pol["targeting"]["allow_defended_targets"] = allow_defended
        systems = targeting.get("excluded_systems")
        if isinstance(systems, list):
            cleaned_systems: list[list[int]] = []
            for item in systems:
                if (
                    isinstance(item, (list, tuple)) and len(item) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in item)
                ):
                    pair = [int(item[0]), int(item[1])]
                    if pair not in cleaned_systems:
                        cleaned_systems.append(pair)
            pol["targeting"]["excluded_systems"] = cleaned_systems
        priority_targets = targeting.get("priority_targets")
        if isinstance(priority_targets, list):
            cleaned_targets: list[str] = []
            for item in priority_targets:
                if not isinstance(item, str):
                    continue
                parts = item.strip().split(":")
                if (
                    len(parts) == 3 and all(part.isdigit() for part in parts)
                    and int(parts[0]) >= 1 and int(parts[1]) >= 1
                    and 1 <= int(parts[2]) <= 15
                ):
                    coord = ":".join(str(int(part)) for part in parts)
                    if coord not in cleaned_targets:
                        cleaned_targets.append(coord)
            pol["targeting"]["priority_targets"] = cleaned_targets

    limits = raw.get("limits")
    if isinstance(limits, dict):
        nonnegative_ints = {
            "scan_span", "spy_global_cooldown_sec", "spy_target_cooldown_sec",
            "attack_target_cooldown_sec", "bashing_max_attacks",
            "min_fleet_value_limit", "max_attack_eta_sec", "reserve_fleet_slots",
            "harvest_max_concurrent", "harvest_cooldown_sec",
            "harvest_reserved_raid_slots", "max_active_fleetsaves",
            "colony_scan_span", "colonize_cooldown_sec",
        }
        positive_ints = {"max_planets"}
        percents = {
            "fleet_risk_percent", "expedition_risk_percent",
            "expedition_total_exposure_percent",
        }
        for key in nonnegative_ints:
            value = limits.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                hard_range = HARD_LIMIT_RANGES.get(key)
                if hard_range is None:
                    pol["limits"][key] = value
                else:
                    lower, upper = hard_range
                    bounded = max(lower, value)
                    if upper is not None:
                        bounded = min(upper, bounded)
                    pol["limits"][key] = bounded
        for key in positive_ints:
            value = limits.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                pol["limits"][key] = value
        for key in percents:
            value = limits.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                pol["limits"][key] = float(value)
        server_slots = limits.get("use_server_expedition_slots")
        if isinstance(server_slots, bool):
            pol["limits"]["use_server_expedition_slots"] = server_slots

    # 등록 자율 AI의 정적 공용 표적 목록은 받지 않는다. 특정 실전 요격은 fresh
    # structured intercept plan을 사용하고, 일반 정찰/약탈은 동적 후보 순환을 쓴다.
    if _account_name(email) in AI_PERSONALITY_FALLBACK:
        pol["targeting"]["priority_targets"] = []

    dr = raw.get("defense_ratio")
    if isinstance(dr, (int, float)) and 0 <= dr <= 1:
        pol["defense_ratio"] = float(dr)

    defense_strategy = raw.get("defense_strategy")
    if isinstance(defense_strategy, dict):
        mode = defense_strategy.get("mode")
        if mode in {"off", "adaptive", "fortress"}:
            pol["defense_strategy"]["mode"] = mode
        for key, upper in (
            ("stored_loot_multiplier", 20.0),
            ("production_hours", 168.0),
        ):
            value = defense_strategy.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                pol["defense_strategy"][key] = max(0.0, min(upper, float(value)))
        rebuild = defense_strategy.get("rebuild_on_attack")
        if isinstance(rebuild, bool):
            pol["defense_strategy"]["rebuild_on_attack"] = rebuild

    # energy_target: 유지하고 싶은 최소 에너지 여유(정수). 여유가 이보다 낮으면
    # apply_build_priority 가 solar_plant 를 안전 접두부에 둔다. bool 은 int 서브클래스라 배제.
    et = raw.get("energy_target")
    if isinstance(et, int) and not isinstance(et, bool):
        pol["energy_target"] = et

    return _ensure_registered_ai_plan_contract(pol, email)


def _resolve_id(x) -> int | None:
    """정책 항목(정수 id, 숫자문자열, 또는 machine_name)을 technologyId 로 해석. 미상이면 None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.isdigit():
            return int(s)
        return NAME_TO_ID.get(s)
    return None


def _clean_id_list(val) -> list[int]:
    """build_priority/research_priority 리스트를 유효한 technologyId 리스트로 정규화."""
    if not isinstance(val, list):
        return []
    out: list[int] = []
    for item in val:
        tid = _resolve_id(item)
        if tid is not None and tid not in out:
            out.append(tid)
    return out


_SURPLUS_RESOURCES = ("metal", "crystal", "deuterium")


def _clean_min_surplus(val) -> dict[str, int]:
    """min_surplus 가드를 {"metal"/"crystal"/"deuterium": 음이 아닌 정수} 로 정규화.

    알 수 없는 키/음수/bool 은 버린다. dict 가 아니면 빈 dict(가드 없음).
    """
    if not isinstance(val, dict):
        return {}
    out: dict[str, int] = {}
    for r in _SURPLUS_RESOURCES:
        v = val.get(r)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[r] = int(v)
    return out


def _clean_ship_list(val, *, max_batch: int | None = MAX_SHIP_BATCH) -> list[dict]:
    """ship_priority 를 [{"id","batch",("max_total"),("min_surplus")}] 로 정규화.

    선택적 조건형 가드(있을 때만 키가 붙는다):
      - max_total  : 계정 보유 상한(정수≥1). 모든 행성+비행+미완료 큐 합계에 적용.
      - min_surplus: {자원:임계} — 지정 자원을 이 값 이상 보유할 때만 건조(잉여 조건).
    잘못된 항목/가드 값은 조용히 버린다(가드 없는 옛 정책은 그대로 매 틱 집행 — 하위호환).
    """
    if not isinstance(val, list):
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for item in val:
        if isinstance(item, dict):
            tid = _resolve_id(item.get("id"))
            batch = item.get("batch", 1)
        else:
            tid = _resolve_id(item)
            batch = 1
        if tid is None or tid in seen or tid not in SHIPS:
            continue
        if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:
            batch = 1
        if max_batch is not None:
            batch = min(batch, max_batch)
        entry: dict = {"id": tid, "batch": batch}
        if isinstance(item, dict):
            mt = item.get("max_total")
            if isinstance(mt, int) and not isinstance(mt, bool) and mt >= 1:
                entry["max_total"] = mt
            ms = _clean_min_surplus(item.get("min_surplus"))
            if ms:
                entry["min_surplus"] = ms
        out.append(entry)
        seen.add(tid)
    return out


# ---------------------------------------------------------------------------
# Versioned structured policy plans
# ---------------------------------------------------------------------------
_PLAN_SECTIONS = (
    "colonize",
    "transfers",
    "development",
    "production",
    "moon_development",
    "phalanx_scans",
    "jump_gate",
    "intercepts",
)


def _bounded_plan_int(value, lower: int, upper: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(lower, min(upper, value))


def _clean_plan_command_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    command_id = value.strip()
    return command_id[:160] if command_id else None


def _clean_plan_text(value, *, default: str, limit: int = 64) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else default


def _clean_plan_target(value) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    galaxy = _bounded_plan_int(value.get("galaxy"), 1, 9)
    system = _bounded_plan_int(value.get("system"), 1, 999)
    position = _bounded_plan_int(value.get("position"), 1, 15)
    if galaxy is None or system is None or position is None:
        return None
    return {"galaxy": galaxy, "system": system, "position": position}


def _clean_plan_resources(value) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    if any(key not in _SURPLUS_RESOURCES for key in value):
        return None
    resources: dict[str, int] = {}
    for key in _SURPLUS_RESOURCES:
        amount = value.get(key, 0)
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            return None
        resources[key] = min(MAX_STRUCTURED_RESOURCE_AMOUNT, int(amount))
    return resources if any(resources.values()) else None


def _clean_plan_time(value) -> int | float | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value > 1_000_000_000_000_000
    ):
        return None
    numeric = float(value)
    if numeric != numeric or numeric in {float("inf"), float("-inf")}:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _clean_plan_ships(value) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or not value or len(value) > 64:
        return None
    ships: dict[str, int] = {}
    for raw_id, raw_amount in value.items():
        technology_id = _resolve_id(raw_id)
        amount = _bounded_plan_int(raw_amount, 1, MAX_STRUCTURED_PRODUCTION_AMOUNT)
        key = str(technology_id) if technology_id is not None else ""
        if technology_id not in SHIPS or amount is None or key in ships:
            return None
        ships[key] = amount
    return dict(sorted(ships.items(), key=lambda item: int(item[0])))


def _deterministic_plan_command_id(prefix: str, identity: Mapping) -> str:
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _command_id_matches(value, expected: str) -> bool:
    return value is None or _clean_plan_command_id(value) == expected


def _clean_moon_development_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    moon_id = _bounded_plan_int(value.get("moon_id"), 1, 2_147_483_647)
    coordinates = _clean_plan_target(
        value.get("exact_coordinates", value.get("coordinates"))
    )
    role = value.get("role")
    raw_facility = value.get("facility_id", value.get("facility"))
    facility_id = (
        moon_development.FACILITY_IDS.get(raw_facility)
        if isinstance(raw_facility, str)
        else _bounded_plan_int(raw_facility, 1, 999)
    )
    current_level = _bounded_plan_int(
        value.get("current_level"), 0, MAX_STRUCTURED_OBJECT_LEVEL
    )
    target_level = _bounded_plan_int(
        value.get("target_level"), 1, MAX_STRUCTURED_OBJECT_LEVEL
    )
    cost = _clean_plan_resources(value.get("cost"))
    build_time = _bounded_plan_int(value.get("build_time_seconds"), 1, 315_360_000)
    if moon_id is None or coordinates is None:
        return None, "missing_or_invalid_moon_identity"
    if role not in moon_development.ROLES:
        return None, "invalid_moon_role"
    if facility_id not in moon_development.FACILITY_NAMES:
        return None, "invalid_moon_facility"
    if current_level is None or target_level != current_level + 1:
        return None, "invalid_moon_level_step"
    if cost is None or build_time is None:
        return None, "missing_or_invalid_moon_cost_time"
    facility = moon_development.FACILITY_NAMES[facility_id]
    if "facility" in value and value.get("facility") != facility:
        return None, "moon_facility_identity_mismatch"
    command_id = f"moon-development:{moon_id}:{facility_id}:{target_level}"
    if not _command_id_matches(value.get("command_id"), command_id):
        return None, "non_deterministic_command_id"
    paired_planet_id = None
    if "paired_planet_id" in value:
        paired_planet_id = _bounded_plan_int(
            value.get("paired_planet_id"), 1, 2_147_483_647
        )
        if paired_planet_id is None:
            return None, "invalid_paired_planet_id"
    plan = {
        "command_id": command_id,
        "action": "build_moon_facility",
        "moon_id": moon_id,
        "exact_coordinates": coordinates,
        "role": role,
        "facility_id": facility_id,
        "facility": facility,
        "current_level": current_level,
        "target_level": target_level,
        "cost": cost,
        "build_time_seconds": build_time,
        "affordable": True,
        "eligible": True,
        "payoff": {
            "facility": facility,
            "target_level": target_level,
            "build_time_seconds": build_time,
            "benefit_available_after_completion": True,
        },
    }
    if paired_planet_id is not None:
        plan["paired_planet_id"] = paired_planet_id
    return plan, None


def _clean_phalanx_scan_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    source_moon_id = _bounded_plan_int(
        value.get("source_moon_id", value.get("moon_id")), 1, 2_147_483_647
    )
    source = _clean_plan_target(
        value.get("source_coordinates", value.get("source_moon"))
    )
    target = _clean_plan_target(
        value.get("target_coordinates", value.get("target"))
    )
    level = _bounded_plan_int(value.get("phalanx_level"), 1, MAX_STRUCTURED_OBJECT_LEVEL)
    cost = _bounded_plan_int(value.get("deuterium_cost"), 1, MAX_STRUCTURED_RESOURCE_AMOUNT)
    reserve = _bounded_plan_int(value.get("deuterium_reserve"), 0, MAX_STRUCTURED_RESOURCE_AMOUNT)
    execute_at = _clean_plan_time(value.get("execute_at", value.get("scan_at")))
    latest_at = _clean_plan_time(value.get("latest_at", execute_at))
    if source_moon_id is None or source is None or target is None:
        return None, "missing_or_invalid_scan_identity"
    if level is None or cost is None or reserve is None:
        return None, "missing_or_invalid_scan_level_resources"
    if execute_at is None or latest_at is None or execute_at > latest_at:
        return None, "missing_or_invalid_scan_window"
    if source["galaxy"] != target["galaxy"]:
        return None, "target_outside_phalanx_galaxy"
    if abs(source["system"] - target["system"]) > level * level - 1:
        return None, "target_outside_phalanx_range"
    identity = {
        "source_moon_id": source_moon_id,
        "source_coordinates": source,
        "target_coordinates": target,
        "phalanx_level": level,
        "deuterium_cost": cost,
        "deuterium_reserve": reserve,
        "execute_at": execute_at,
        "latest_at": latest_at,
    }
    command_id = _deterministic_plan_command_id("phalanx-scan", identity)
    if not _command_id_matches(value.get("command_id"), command_id):
        return None, "non_deterministic_command_id"
    return {"command_id": command_id, **identity}, None


def _clean_jump_gate_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    source_moon_id = _bounded_plan_int(value.get("source_moon_id"), 1, 2_147_483_647)
    destination_moon_id = _bounded_plan_int(
        value.get("destination_moon_id", value.get("target_moon_id")),
        1,
        2_147_483_647,
    )
    source = _clean_plan_target(
        value.get("source_coordinates", value.get("source_moon"))
    )
    destination = _clean_plan_target(
        value.get("destination_coordinates", value.get("destination_moon"))
    )
    ships = _clean_plan_ships(value.get("ships"))
    execute_at = _clean_plan_time(value.get("execute_at"))
    latest_at = _clean_plan_time(value.get("latest_at", execute_at))
    if source_moon_id is None or destination_moon_id is None or source is None or destination is None:
        return None, "missing_or_invalid_jump_gate_identity"
    if source_moon_id == destination_moon_id or source == destination:
        return None, "same_jump_gate"
    if ships is None:
        return None, "missing_or_invalid_jump_gate_ships"
    if execute_at is None or latest_at is None or execute_at > latest_at:
        return None, "missing_or_invalid_jump_gate_window"
    identity = {
        "source_moon_id": source_moon_id,
        "destination_moon_id": destination_moon_id,
        "source_coordinates": source,
        "destination_coordinates": destination,
        "ships": ships,
        "execute_at": execute_at,
        "latest_at": latest_at,
    }
    command_id = _deterministic_plan_command_id("jump-gate", identity)
    if not _command_id_matches(value.get("command_id"), command_id):
        return None, "non_deterministic_command_id"
    return {"command_id": command_id, **identity}, None


def _clean_intercept_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    version = _bounded_plan_int(value.get("version", 1), 1, 1)
    source = _clean_plan_target(value.get("source_coordinates"))
    target = _clean_plan_target(value.get("target_coordinates"))
    target_type = _bounded_plan_int(value.get("target_type"), 1, 3)
    ships = _clean_plan_ships(value.get("ships"))
    speed = value.get("speed")
    if (
        not isinstance(speed, (int, float))
        or isinstance(speed, bool)
        or not 1 <= speed <= 100
    ):
        speed = None
    integer_fields = {
        "own_flight_seconds": (1, 31_536_000),
        "dispatch_margin_seconds": (0, 86_400),
        "send_window_seconds": (0, 86_400),
        "max_phalanx_age_seconds": (1, 86_400),
    }
    integers = {
        key: _bounded_plan_int(value.get(key), lower, upper)
        for key, (lower, upper) in integer_fields.items()
    }
    time_fields = (
        "created_at",
        "observed_server_time",
        "observation_expires_at",
        "hostile_return_eta",
        "desired_impact_at",
        "dispatch_at",
    )
    times = {key: _clean_plan_time(value.get(key)) for key in time_fields}
    text_fields = (
        "agent_id",
        "observation_id",
        "movement_id",
        "movement_fingerprint",
        "hostile_owner_id",
    )
    texts = {
        key: _clean_plan_text(value.get(key), default="", limit=160)
        for key in text_fields
    }
    if version != 1 or source is None or target is None or target_type not in {1, 3}:
        return None, "missing_or_invalid_intercept_identity"
    if ships is None or speed is None or any(item is None for item in integers.values()):
        return None, "missing_or_invalid_intercept_fleet"
    if any(item is None for item in times.values()) or any(not item for item in texts.values()):
        return None, "missing_or_invalid_intercept_evidence"
    if not (
        times["observed_server_time"] <= times["created_at"] <= times["observation_expires_at"]
        and times["created_at"] <= times["dispatch_at"] < times["desired_impact_at"]
        and times["hostile_return_eta"] <= times["desired_impact_at"]
    ):
        return None, "invalid_intercept_timing_order"
    expected_impact = (
        float(times["dispatch_at"])
        + integers["own_flight_seconds"]
        + integers["dispatch_margin_seconds"]
    )
    if abs(expected_impact - float(times["desired_impact_at"])) > 0.000001:
        return None, "inconsistent_intercept_flight_time"
    identity = {
        "version": version,
        "agent_id": texts["agent_id"],
        "movement_id": texts["movement_id"],
        "movement_fingerprint": texts["movement_fingerprint"],
        "hostile_owner_id": texts["hostile_owner_id"],
        "hostile_return_eta": times["hostile_return_eta"],
        "source_coordinates": source,
        "target_coordinates": target,
        "target_type": target_type,
        "ships": ships,
        "speed": speed,
        "own_flight_seconds": integers["own_flight_seconds"],
        "desired_impact_at": times["desired_impact_at"],
        "dispatch_at": times["dispatch_at"],
        "dispatch_margin_seconds": integers["dispatch_margin_seconds"],
        "send_window_seconds": integers["send_window_seconds"],
    }
    command_id = _deterministic_plan_command_id("intercept", identity)
    if not _command_id_matches(value.get("command_id"), command_id):
        return None, "non_deterministic_command_id"
    plan = {
        **identity,
        "command_id": command_id,
        "created_at": times["created_at"],
        "observation_id": texts["observation_id"],
        "observed_server_time": times["observed_server_time"],
        "observation_expires_at": times["observation_expires_at"],
        "max_phalanx_age_seconds": integers["max_phalanx_age_seconds"],
    }
    if "jump_gate_staging" in value:
        if value.get("jump_gate_staging") is not None:
            return None, "embedded_jump_gate_staging_not_supported"
        plan["jump_gate_staging"] = None
    return plan, None


def _clean_colonize_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    command_id = _clean_plan_command_id(value.get("command_id"))
    target = _clean_plan_target(value.get("exact_target"))
    if command_id is None:
        return None, "missing_or_invalid_command_id"
    if target is None:
        return None, "missing_or_invalid_exact_target"
    source = value.get("source_planet_id")
    source_id = None if source is None else _bounded_plan_int(source, 1, 2_147_483_647)
    if source is not None and source_id is None:
        return None, "invalid_source_planet_id"
    goal = _clean_plan_text(value.get("goal"), default="colonize", limit=64)
    role = _clean_plan_text(value.get("role"), default="balanced", limit=32)
    phase = _clean_plan_text(value.get("phase"), default="prepare", limit=32)
    if goal is None or role is None or phase not in {"prepare", "prerequisite", "dispatch"}:
        return None, "invalid_text_field"
    plan: dict = {
        "command_id": command_id,
        "exact_target": target,
        "goal": goal,
        "role": role,
        "phase": phase,
    }
    if source_id is not None:
        plan["source_planet_id"] = source_id
    transfer_request = value.get("transfer_request")
    if transfer_request is not None:
        transfer_id = _clean_plan_command_id(transfer_request)
        if transfer_id is None:
            return None, "invalid_transfer_request"
        plan["transfer_request"] = transfer_id
    return plan, None


def _clean_transfer_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    command_id = _clean_plan_command_id(value.get("command_id"))
    source_id = _bounded_plan_int(value.get("source_planet_id"), 1, 2_147_483_647)
    target_id = None
    if "target_planet_id" in value:
        target_id = _bounded_plan_int(value.get("target_planet_id"), 1, 2_147_483_647)
        if target_id is None:
            return None, "invalid_target_planet_id"
    exact_target = None
    if "exact_target" in value:
        exact_target = _clean_plan_target(value.get("exact_target"))
        if exact_target is None:
            return None, "invalid_exact_target"
    resources = _clean_plan_resources(value.get("resources"))
    if command_id is None or source_id is None:
        return None, "missing_or_invalid_identity"
    if target_id is None and exact_target is None:
        return None, "missing_target"
    if target_id == source_id:
        return None, "source_equals_target"
    if resources is None:
        return None, "missing_or_invalid_resources"
    plan = {
        "command_id": command_id,
        "source_planet_id": source_id,
        "resources": resources,
    }
    if target_id is not None:
        plan["target_planet_id"] = target_id
    if exact_target is not None:
        plan["exact_target"] = exact_target
    return plan, None


def _clean_development_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    command_id = _clean_plan_command_id(value.get("command_id"))
    planet_id = _bounded_plan_int(value.get("planet_id"), 1, 2_147_483_647)
    role = _clean_plan_text(value.get("role"), default="balanced", limit=32)
    floors = value.get("minimum_floors", {})
    if command_id is None or planet_id is None or role is None:
        return None, "missing_or_invalid_identity"
    if not isinstance(floors, Mapping) or len(floors) > 32:
        return None, "invalid_minimum_floors"
    clean_floors: dict[int, int] = {}
    for raw_id, raw_level in floors.items():
        technology_id = _resolve_id(raw_id)
        level = _bounded_plan_int(raw_level, 0, MAX_STRUCTURED_OBJECT_LEVEL)
        if technology_id not in BUILDINGS and technology_id not in FACILITIES:
            return None, "invalid_floor_object"
        if level is None:
            return None, "invalid_floor_level"
        clean_floors[technology_id] = level
    return {
        "command_id": command_id,
        "planet_id": planet_id,
        "role": role,
        "minimum_floors": clean_floors,
    }, None


def _clean_production_plan(value) -> tuple[dict | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "entry_not_object"
    command_id = _clean_plan_command_id(value.get("command_id"))
    technology_id = _resolve_id(value.get("technology_id", value.get("id")))
    amount = _bounded_plan_int(value.get("amount"), 1, MAX_STRUCTURED_PRODUCTION_AMOUNT)
    raw_planet_ids = value.get("planet_ids")
    if raw_planet_ids is None and "planet_id" in value:
        raw_planet_ids = [value.get("planet_id")]
    if command_id is None or technology_id not in SHIPS or amount is None:
        return None, "missing_or_invalid_production_request"
    if not isinstance(raw_planet_ids, list) or not raw_planet_ids:
        return None, "missing_or_invalid_planet_ids"
    planet_ids: list[int] = []
    for raw_planet_id in raw_planet_ids[:MAX_STRUCTURED_PLAN_ENTRIES]:
        planet_id = _bounded_plan_int(raw_planet_id, 1, 2_147_483_647)
        if planet_id is None:
            return None, "invalid_planet_id"
        if planet_id not in planet_ids:
            planet_ids.append(planet_id)
    horizon = value.get("queue_horizon_seconds")
    if horizon is None:
        horizon = production_capacity.default_ship_queue_horizon_seconds()
    horizon = _bounded_plan_int(
        horizon,
        production_capacity.MIN_QUEUE_HORIZON_SECONDS,
        production_capacity.MAX_QUEUE_HORIZON_SECONDS,
    )
    if horizon is None:
        return None, "invalid_queue_horizon_seconds"
    return {
        "command_id": command_id,
        "technology_id": technology_id,
        "amount": amount,
        "planet_ids": planet_ids,
        "queue_horizon_seconds": horizon,
    }, None


def clean_policy_plans(value) -> dict:
    """Normalize the strict version-1 structured plans object.

    Missing section keys remain missing while explicit empty lists remain empty.
    A malformed object or unsupported version is an explicit rejected object
    with empty sections, so consumers can never mistake it for legacy absence.
    """
    rejected_empty = {
        "version": POLICY_PLANS_VERSION,
        "status": "rejected",
        **{section: [] for section in _PLAN_SECTIONS},
        "rejections": [],
    }
    if not isinstance(value, Mapping):
        rejected_empty["rejections"].append({"section": "plans", "reason": "not_object"})
        return rejected_empty
    version = value.get("version")
    if version != POLICY_PLANS_VERSION or isinstance(version, bool):
        rejected_empty["rejections"].append({
            "section": "plans",
            "reason": "unsupported_version",
            "received_version": version,
        })
        return rejected_empty

    cleaners = {
        "colonize": _clean_colonize_plan,
        "transfers": _clean_transfer_plan,
        "development": _clean_development_plan,
        "production": _clean_production_plan,
        "moon_development": _clean_moon_development_plan,
        "phalanx_scans": _clean_phalanx_scan_plan,
        "jump_gate": _clean_jump_gate_plan,
        "intercepts": _clean_intercept_plan,
    }
    normalized: dict = {"version": POLICY_PLANS_VERSION, "status": "ready", "rejections": []}
    unknown = sorted(str(key) for key in value if key not in {"version", *_PLAN_SECTIONS})
    if unknown:
        normalized["rejections"].append({
            "section": "plans", "reason": "unknown_keys", "keys": unknown[:16]
        })
    for section, cleaner in cleaners.items():
        if section not in value:
            continue
        raw_entries = value.get(section)
        normalized[section] = []
        if not isinstance(raw_entries, list):
            normalized["rejections"].append({"section": section, "reason": "not_list"})
            continue
        if len(raw_entries) > MAX_STRUCTURED_PLAN_ENTRIES:
            normalized["rejections"].append({
                "section": section,
                "reason": "too_many_entries",
                "limit": MAX_STRUCTURED_PLAN_ENTRIES,
            })
        for index, raw_entry in enumerate(raw_entries[:MAX_STRUCTURED_PLAN_ENTRIES]):
            entry, reason = cleaner(raw_entry)
            if entry is None:
                normalized["rejections"].append({
                    "section": section, "index": index, "reason": reason or "invalid_entry"
                })
            else:
                normalized[section].append(entry)
    if normalized["rejections"]:
        accepted = any(normalized.get(section) for section in _PLAN_SECTIONS)
        normalized["status"] = "partial" if accepted else "rejected"
    return normalized


def _empty_policy_plans() -> dict:
    """Return the complete v1 contract without disabling legacy empty sections."""
    return {
        "version": POLICY_PLANS_VERSION,
        "status": "ready",
        "rejections": [],
        **{section: [] for section in _PLAN_SECTIONS},
    }


def _ensure_registered_ai_plan_contract(policy: dict, email: str) -> dict:
    """Registered AI policies always expose a complete structured-plan contract."""
    if _account_name(email) not in AI_PERSONALITY_FALLBACK:
        return policy
    plans = policy.get("plans")
    if not isinstance(plans, dict):
        plans = _empty_policy_plans()
    else:
        plans = dict(plans)
        plans.setdefault("version", POLICY_PLANS_VERSION)
        plans.setdefault("status", "ready")
        plans.setdefault("rejections", [])
        for section in _PLAN_SECTIONS:
            plans.setdefault(section, [])
    policy["plans_version"] = POLICY_PLANS_VERSION
    policy["plans"] = plans
    return policy


def _seed_automatic_moon_development(policy: dict, empire_state: Mapping) -> None:
    """Fill an empty moon section with the field-safe planner's next AI step."""
    plans = policy.get("plans")
    if not isinstance(plans, dict) or plans.get("moon_development"):
        return
    root = _empire_root(empire_state)
    moons = root.get("moons")
    if not isinstance(moons, list) or not moons:
        return
    moon_ids = sorted({
        int(row.get("moon_id"))
        for row in moons
        if isinstance(row, Mapping)
        and isinstance(row.get("moon_id"), int)
        and not isinstance(row.get("moon_id"), bool)
    })
    if not moon_ids:
        return
    roles = {moon_id: "intel_hub" for moon_id in moon_ids}
    for moon_id in moon_ids[1:]:
        roles[moon_id] = "fleet_hub"
    account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
    economy_speed = account.get("economy_speed", root.get("economy_speed", 1))
    if not isinstance(economy_speed, int) or isinstance(economy_speed, bool) or economy_speed < 1:
        economy_speed = 1
    try:
        generated = moon_development.plan_moon_development(
            empire_state,
            roles=roles,
            default_role="intel_hub",
            economy_speed=economy_speed,
        )
    except Exception as exc:  # noqa: BLE001 - automatic planning remains fail closed
        plans.setdefault("rejections", []).append({
            "section": "moon_development",
            "reason": f"automatic_planner_error:{type(exc).__name__}",
        })
        return
    commands = generated.get("commands") if isinstance(generated, Mapping) else None
    if not isinstance(commands, list):
        return
    accepted: list[dict] = []
    for command in commands[:MAX_STRUCTURED_PLAN_ENTRIES]:
        cleaned, reason = _clean_moon_development_plan(command)
        if cleaned is not None:
            accepted.append(cleaned)
        elif reason:
            plans.setdefault("rejections", []).append({
                "section": "moon_development",
                "reason": f"automatic_plan_rejected:{reason}",
            })
    if accepted:
        plans["moon_development"] = accepted
        plans["status"] = "ready"


# ---------------------------------------------------------------------------
# 우선순위 오버라이드 (후보 재정렬)
# ---------------------------------------------------------------------------
def _reorder(candidates: list[tuple], pri_ids: list[int]) -> list[tuple]:
    """후보를 정책 우선순위(pri_ids) 앞으로 안정 정렬. 정책에 없는 후보는 원래 순서 유지.

    candidates 각 항목의 technologyId 는 tuple 의 index 1(("build",tid)/("research",tid)/
    ("ship",tid,batch) 모두 [1] 이 tid). sorted 는 안정 정렬이라 같은 우선순위 키 안에서
    plan_* 가 준 원래 순서가 보존된다.
    """
    if not pri_ids:
        return candidates
    rank = {tid: i for i, tid in enumerate(pri_ids)}
    return sorted(candidates, key=lambda a: rank.get(a[1], len(pri_ids)))


def _preserve_shared_mine_order(
    shared_candidates: list[tuple], reordered: list[tuple]
) -> list[tuple]:
    """AI 정책 재정렬 뒤에도 공용 플래너가 계산한 광산 ROI 상대순서를 보존한다.

    정책은 태양광/기타 후보와 광산 슬롯의 상대 위치를 바꿀 수 있지만, 메탈·크리스탈·
    듀테륨 광산끼리의 순서는 공용 ``plan_primary`` 결과를 따른다. 이렇게 해야 규칙봇과
    AI가 같은 투자 계산을 실제 첫 건설 후보까지 공유한다.
    """
    shared_mines = [
        action
        for action in shared_candidates
        if action[0] == "build" and action[1] in MINE_BUILDINGS
    ]
    if len(shared_mines) < 2:
        return reordered
    mine_iter = iter(shared_mines)
    return [
        next(mine_iter)
        if action[0] == "build" and action[1] in MINE_BUILDINGS
        else action
        for action in reordered
    ]


def apply_build_priority(
    candidates: list[tuple], pri_ids: list[int], energy: int | None,
    energy_target: int = 0,
    protected_actions: list[tuple] | None = None,
) -> list[tuple]:
    """primary(자원건물/시설) 후보를 정책 우선순위로 재정렬.

    protected_actions 는 공용 plan_primary 가 고른 저장고/선제 에너지/시설 진행 후보다.
    이 접두부는 정책이 뒤로 밀지 못한다. 나머지만 정책 순서로 재정렬한다. 에너지 여유가
    energy_target 미만이면 태양광(4)을 저장고 뒤, 시설 앞에 고정한다. 후보에 없으면
    강제로 만들지는 않는다(공용 선행조건·자원 판단 존중). 세 광산끼리의 상대순서는
    공용 투자 플래너의 ROI 결과를 유지해 AI 정책이 경제 계산을 다시 뒤집지 못하게 한다.
    """
    wanted = protected_actions or []
    protected = [a for a in candidates if a in wanted]
    # 같은 tuple 이 중복돼도 한 번만 집행 후보에 남긴다.
    protected = list(dict.fromkeys(protected))
    shared_unprotected = [a for a in candidates if a not in protected]
    ordered = _reorder(shared_unprotected, pri_ids)
    ordered = _preserve_shared_mine_order(shared_unprotected, ordered)
    if energy is not None and energy < energy_target:
        solar = ("build", 4)
        if solar in candidates and solar not in protected:
            ordered = [a for a in ordered if a != solar]
            # 필요한 저장고는 태양광 비용 자체를 지불하게 해 주므로 항상 먼저 둔다.
            split = 0
            while split < len(protected) and protected[split][1] in STORAGE_BUILDINGS:
                split += 1
            protected.insert(split, solar)
    return protected + ordered


def apply_research_priority(
    candidates: list[tuple], pri_ids: list[int], levels: dict[int, int]
) -> list[tuple]:
    """공용 성장트리의 유한한 연구 후보 안에서만 정책 우선순위를 적용한다.

    종전에는 ID만 적힌 정책 연구를 목표 레벨 없이 매 틱 덧붙여 무기/에너지 연구가
    무한히 올라가고, 뒤에서 실행되는 식민 천체물리 연구가 영구히 큐를 못 잡았다. AI의
    개성은 '어떤 공용 목표를 먼저 할지'로 유지하되 후보 밖 연구는 주입하지 않는다.
    ``levels``는 기존 호출 호환과 향후 목표형 정책 확장을 위해 유지한다.
    """
    del levels
    return _reorder(candidates, pri_ids)


def _ship_guard_ok(
    s: dict,
    levels: dict[int, int],
    resource_state: dict | None,
    energy: int | None,
    energy_target: int,
    ship_inventory: dict[int, int],
    ship_inventory_known: bool,
) -> bool:
    """정책 ship_priority 항목 s 의 조건형 가드를 이번 틱 상태로 평가. 집행 가능하면 True.

    평가 순서(하나라도 걸리면 이번 틱 스킵):
      1) 위성 흑자 스킵(무조건): 태양광 위성(212)은 에너지 적자 해소가 목적이라, 에너지
         여유가 energy_target 을 넘으면(흑자) 정책이 시켜도 건조하지 않는다. 가드 유무 무관.
      2) max_total: 모든 행성+비행+미완료 큐 합계가 상한 이상이면 스킵(폭주 방지).
         계정 합계 조회에 실패하면 max_total 항목은 fail-closed로 스킵한다.
      3) min_surplus: 지정 자원 보유량이 임계 미만이면 스킵(경제를 굶기지 않게). 자원상태를
         모르면(조회 실패) 판정을 건너뛰고 통과시킨다(기존 "모르면 일단 시도" 원칙).
    """
    tid = s["id"]
    if tid == SOLAR_SATELLITE and energy is not None and energy > energy_target:
        return False
    mt = s.get("max_total")
    if isinstance(mt, int):
        if not ship_inventory_known or ship_inventory.get(tid, 0) >= mt:
            return False
    ms = s.get("min_surplus")
    if ms and resource_state:
        for r, threshold in ms.items():
            if resource_state.get(r, 0) < threshold:
                return False
    return True


def apply_ship_priority(
    candidates: list[tuple],
    ship_pri: list[dict],
    levels: dict[int, int],
    resource_state: dict | None = None,
    energy: int | None = None,
    energy_target: int = 0,
    allow_policy_extras: bool = True,
    account_ship_totals=_ACCOUNT_SHIP_TOTALS_UNSET,
    enforce_max_total_on_shared: bool = True,
) -> list[tuple]:
    """함선 후보를 정책 우선순위로 재정렬하고, 조건형 가드를 통과한 정책 함선을 덧붙인다.

    각 정책 함선은 _ship_guard_ok 로 이번 틱 상태(계정 보유/자원/에너지)에 대해 평가된다.
    guided의 기본값에서는 ``max_total``을 정책 추가 후보뿐 아니라 같은 id의 공용 후보에도
    엄격히 적용한다. full autonomy는 ``enforce_max_total_on_shared=False``를 넘겨 수동
    ``ship_priority``의 가드는 보존하되, 같은 함종의 동적 ``ship_strategy`` 성장까지 과거의
    고정 총량으로 끝내지는 않는다.

    account_ship_totals를 생략한 구 호출부는 levels를 써서 하위호환한다. 실가동 호출부가
    명시적으로 None을 넘기면 계정 합계 조회 실패를 뜻하며 max_total 항목을 fail-closed로
    막는다. resource_state/energy 를 안 주면 위성 흑자 스킵/잉여 조건은 비활성이다.
    allow_policy_extras=False 면 공용 후보의 정책 재정렬만 하고 후보 밖 함선은 주입하지 않는다.
    """
    if account_ship_totals is _ACCOUNT_SHIP_TOTALS_UNSET:
        ship_inventory = levels
        ship_inventory_known = True
    elif account_ship_totals is None:
        ship_inventory = {}
        ship_inventory_known = False
    else:
        ship_inventory = account_ship_totals
        ship_inventory_known = True

    # 정책 max_total의 잔여량은 수동 policy extra에는 항상 적용한다. guided에서만 동일
    # id의 공용/동적 후보에도 적용한다. full의 전략 함대 후보는 현재 자원·가치비율로 매
    # 틱 재계산되므로, 수동 배치 가드가 그 성장 전체의 고정 종착점이 되어서는 안 된다.
    remaining_by_id: dict[int, int] = {}
    for s in ship_pri:
        mt = s.get("max_total")
        if not isinstance(mt, int):
            continue
        remaining_by_id[s["id"]] = (
            max(0, mt - ship_inventory.get(s["id"], 0))
            if ship_inventory_known else 0
        )

    bounded_candidates: list[tuple] = []
    for action in candidates:
        tid = action[1]
        if not enforce_max_total_on_shared or tid not in remaining_by_id:
            bounded_candidates.append(action)
            continue
        remaining = remaining_by_id[tid]
        if remaining <= 0:
            continue
        amount = min(int(action[2]), remaining)
        if amount > 0:
            bounded_candidates.append((action[0], tid, amount))

    allowed: list[dict] = []
    for raw in ship_pri:
        if not _ship_guard_ok(
            raw, levels, resource_state, energy, energy_target,
            ship_inventory, ship_inventory_known,
        ):
            continue
        s = dict(raw)
        if s["id"] in remaining_by_id:
            s["batch"] = min(s["batch"], remaining_by_id[s["id"]])
        allowed.append(s)
    pri_ids = [s["id"] for s in allowed]
    existing = {a[1] for a in bounded_candidates}
    extra = []
    if allow_policy_extras:
        extra = [
            ("ship", s["id"], s["batch"])
            for s in allowed
            if s["id"] not in existing and prereqs_met(s["id"], levels)
        ]
    ordered = _reorder(bounded_candidates + extra, pri_ids)
    # 에너지 비상 핀: 적자면 위성(212)을 정책 재정렬보다 위로 고정한다.
    # 함선 건조는 틱당 1건(_try_first)이라 정책이 전투기를 앞세우면 plan_secondary 가
    # 주입한 비상 위성이 영원히 밀린다(실측: ai01 이 -451 적자에서 전투기만 건조, 위성 0).
    # 규칙봇의 "에너지비상 > 기존계획" 우선순위와 동일한 원칙.
    if energy is not None and energy < 0:
        sats = [a for a in ordered if a[1] == 212]
        if sats:
            ordered = sats + [a for a in ordered if a[1] != 212]
    return ordered


def _primary_safety_actions(
    candidates: list[tuple],
    levels: dict[int, int],
    energy: int | None,
    resource_state: dict | None,
) -> list[tuple]:
    """AI 정책보다 앞에 고정할 공용 성장 안전 접두부를 계산한다.

    - plan_primary 에 나타난 저장고는 실제 병목 판정으로 주입된 것이므로 전부 보호한다.
    - 공용 에너지 계산기가 선제/반응 태양광을 골랐으면 보호한다.
    - 안전 후보를 걷어낸 뒤 첫 후보가 시설이면 공용 성장 상태머신이 선택한 시설 틱이다.
      이를 보호해 정책의 광산 목록이 로봇/연구소/조선소를 영구히 뒤로 미는 일을 막는다.
    """
    protected_set = {a for a in candidates if a[1] in STORAGE_BUILDINGS}
    try:
        from bots.rule_based import energy as energy_rule

        eplan = energy_rule.plan_energy(
            levels, energy, resource_state, candidates, allow_satellite=True
        )
        if eplan.solar_action is not None:
            protected_set.add(eplan.solar_action)
    except Exception:  # noqa: BLE001 - 안전 접두부 계산 실패 시 기존 후보로 계속
        pass

    remaining = [a for a in candidates if a not in protected_set]
    if remaining and remaining[0][0] == "facility":
        protected_set.add(remaining[0])
    return [a for a in candidates if a in protected_set]


def _identity_planet_goal(personality_key: str) -> int:
    """공용 post_actions가 해당 성격에 확보하려는 최소 행성 수."""
    if personality_key == "turtle":
        return 1
    if personality_key == "raider":
        return 2
    return 3


def _identity_planets_ready(email: str, personality_key: str) -> bool:
    """정책 추가 함선이 식민 큐를 써도 되는 단계인지 보수적으로 판정한다."""
    goal = _identity_planet_goal(personality_key)
    if goal <= 1:
        return True
    try:
        return len(db_reader.list_planets_db(email)) >= goal
    except Exception:  # noqa: BLE001 - 행성 수 미상에서는 식민 큐 보호(fail closed)
        return False


def _empire_root(empire_state: Mapping) -> Mapping:
    root = empire_state.get("normalized_state", empire_state)
    return root if isinstance(root, Mapping) else {}


def _empire_planets(empire_state: Mapping) -> dict[int, Mapping]:
    root = _empire_root(empire_state)
    raw_planets = root.get("planets")
    if not isinstance(raw_planets, list):
        return {}
    planets: dict[int, Mapping] = {}
    for planet in raw_planets:
        if not isinstance(planet, Mapping):
            continue
        planet_id = planet.get("planet_id")
        if isinstance(planet_id, int) and not isinstance(planet_id, bool) and planet_id > 0:
            planets[planet_id] = planet
    return planets


def _empire_state_available(empire_state: Mapping) -> bool:
    root = _empire_root(empire_state)
    return root.get("status") != "unavailable" and isinstance(root.get("planet_count"), int)


def _current_empire_planet_id(empire_state: Mapping) -> int | None:
    for planet_id, planet in _empire_planets(empire_state).items():
        if planet.get("is_current") is True:
            return planet_id
    return None


def _structured_rejection(action: str, plan: Mapping | None, reason: str, **extra) -> dict:
    outcome = {
        "action": action,
        "ok": False,
        "status": "rejected",
        "reason": reason,
        "command_id": plan.get("command_id") if isinstance(plan, Mapping) else None,
    }
    outcome.update(extra)
    return outcome


def _reserve_colonization_plans(
    client,
    email: str,
    policy: dict,
    empire_state: Mapping,
    ledger: empire_scheduler.ReservationLedger,
    structured_outcomes: list[dict],
) -> tuple[list[dict], bool, set[int]]:
    plans = policy.get("plans")
    colonize_plans = plans.get("colonize") if isinstance(plans, dict) else None
    if not isinstance(colonize_plans, list) or not colonize_plans:
        return [], False, set()
    if not action_enabled(policy, "colonize"):
        structured_outcomes.extend(
            _structured_rejection("colonize", plan, "action_disabled")
            for plan in colonize_plans
        )
        return [], False, set()
    if not _empire_state_available(empire_state):
        structured_outcomes.extend(
            _structured_rejection("colonize", plan, "empire_state_unavailable")
            for plan in colonize_plans
        )
        return [], False, set()

    root = _empire_root(empire_state)
    planets = _empire_planets(empire_state)
    planet_count = int(root.get("planet_count") or 0)
    default_source = root.get("canonical_home_id")
    executable: list[dict] = []
    research_reserved = False
    unit_reserved_planets: set[int] = set()
    for plan in colonize_plans:
        source_planet_id = plan.get("source_planet_id", default_source)
        if source_planet_id not in planets:
            structured_outcomes.append(
                _structured_rejection("colonize", plan, "source_planet_unavailable")
            )
            continue
        try:
            source_levels = db_reader.get_levels_db(email, planet_id=source_planet_id)
            target_position = int(plan["exact_target"]["position"])
            prepared = colony.prepare_colonization(
                client,
                email,
                source_levels,
                {**plan, "source_planet_id": source_planet_id},
                max_planets=int(policy.get("limits", {}).get("max_planets", 99)),
                reserve_fleet_slots=int(
                    policy.get("limits", {}).get("reserve_fleet_slots", 0)
                ),
                enforce_strategy_cap=policy.get("autonomy") != "full",
                cooldown_sec=int(
                    policy.get("limits", {}).get("colonize_cooldown_sec", 0)
                ),
            )
            candidates = colony.colonization_prerequisite_candidates(
                source_levels,
                planet_count=planet_count,
                target_position=target_position,
            )
        except Exception as exc:  # noqa: BLE001 - structured plans fail closed
            structured_outcomes.append(_structured_rejection(
                "colonize", plan, "preparation_error", error=type(exc).__name__
            ))
            continue

        if prepared.get("status") == "rejected":
            structured_outcomes.append({"action": "colonize", **prepared})
            continue
        if candidates:
            candidate = candidates[0]
            technology_id = int(candidate[1])
            if candidate[0] == "research":
                costs = object_cost(
                    technology_id, int(source_levels.get(technology_id, 0) or 0) + 1
                )
                queue_kwargs = {"research_queue": True}
            elif candidate[0] == "ship":
                unit_cost = ship_cost(technology_id)
                amount = max(1, int(candidate[2]))
                costs = (
                    {key: int(unit_cost[key]) * amount for key in _SURPLUS_RESOURCES}
                    if unit_cost else None
                )
                queue_kwargs = {"unit_queue": True}
            else:
                costs = None
                queue_kwargs = {}
            if costs is None or not queue_kwargs:
                structured_outcomes.append(_structured_rejection(
                    "colonize", plan, "prerequisite_cost_unavailable"
                ))
                continue
            reservation = ledger.reserve(
                f"{plan['command_id']}:prerequisite:{technology_id}",
                planet_id=source_planet_id,
                resources=costs,
                **queue_kwargs,
            )
            structured_outcomes.append({
                "action": "colonize",
                "ok": reservation.accepted,
                "status": "reserved" if reservation.accepted else "rejected",
                "reason": reservation.reason,
                "command_id": plan["command_id"],
                "phase": "prerequisite",
                "candidate": candidate,
                "reservation": reservation.to_dict(),
                "prepared": prepared,
            })
            if reservation.accepted:
                executable.append({
                    "plan": plan,
                    "source_planet_id": source_planet_id,
                    "levels": source_levels,
                    "candidate": candidate,
                })
                if candidate[0] == "research":
                    research_reserved = True
                else:
                    unit_reserved_planets.add(source_planet_id)
            continue
        if prepared.get("status") == "ready":
            reservation = ledger.reserve(
                f"{plan['command_id']}:dispatch", fleet_slots=1
            )
            structured_outcomes.append({
                "action": "colonize",
                "ok": reservation.accepted,
                "status": "reserved" if reservation.accepted else "rejected",
                "reason": reservation.reason,
                "command_id": plan["command_id"],
                "phase": "dispatch",
                "reservation": reservation.to_dict(),
                "prepared": prepared,
            })
        else:
            structured_outcomes.append({"action": "colonize", **prepared})
    return executable, research_reserved, unit_reserved_planets


def _resources_after_reservations(
    resource_state: dict | None,
    ledger: empire_scheduler.ReservationLedger,
    planet_id: int | None,
) -> dict | None:
    if resource_state is None or planet_id is None:
        return resource_state
    remaining = ledger.summary().get("resources_by_planet", {}).get(str(planet_id), {}).get(
        "remaining"
    )
    if not isinstance(remaining, Mapping):
        return resource_state
    adjusted = dict(resource_state)
    for key in _SURPLUS_RESOURCES:
        if isinstance(remaining.get(key), (int, float)):
            adjusted[key] = int(remaining[key])
    return adjusted


def _execute_colonization_prerequisites(
    client,
    email: str,
    executable: list[dict],
    structured_outcomes: list[dict],
) -> tuple[list[str], bool]:
    if not executable:
        return [], True
    start_planet_id = planets_mod.current_planet_id(client)
    if start_planet_id is None:
        structured_outcomes.extend(
            _structured_rejection("colonize", item["plan"], "start_planet_unknown")
            for item in executable
        )
        return [], False
    done: list[str] = []
    restore_ok = True
    try:
        for item in executable:
            plan = item["plan"]
            planet_id = item["source_planet_id"]
            candidate = item["candidate"]
            if not planets_mod.switch_planet(client, planet_id):
                structured_outcomes.append(
                    _structured_rejection("colonize", plan, "planet_switch_failed")
                )
                continue
            if candidate[0] == "research":
                try:
                    state = db_reader.get_resource_state_db(email, planet_id=planet_id)
                except Exception:  # noqa: BLE001 - executor still verifies server result
                    state = None
                actions, message = miner_bot._queue_actions(
                    client,
                    [candidate],
                    1,
                    levels=item["levels"],
                    resource_state=state,
                    backoff_key=f"{email}:structured-colonize:{planet_id}",
                )
                action = actions[0] if actions else None
            else:
                action, message = miner_bot._try_first(client, [candidate])
            structured_outcomes.append({
                "action": "colonize",
                "ok": action is not None,
                "status": "executed" if action is not None else "rejected",
                "reason": "prerequisite_queued" if action is not None else "queue_rejected",
                "command_id": plan["command_id"],
                "phase": "prerequisite",
                "candidate": candidate,
                "message": message,
            })
            if action is not None:
                done.append(miner_bot._fmt_action(action))
    finally:
        restore_ok = planets_mod.switch_planet(client, start_planet_id)
        if not restore_ok:
            structured_outcomes.append(_structured_rejection(
                "planet_restore", None, "start_planet_restore_failed",
                planet_id=start_planet_id,
            ))
    return done, restore_ok


def _covered_production_planets(policy: dict, empire_state: Mapping) -> set[int]:
    plans = policy.get("plans")
    production = plans.get("production") if isinstance(plans, dict) else None
    if (
        not isinstance(production, list)
        or not action_enabled(policy, "production")
        or not _empire_state_available(empire_state)
    ):
        return set()
    owned = set(_empire_planets(empire_state))
    return {
        planet_id
        for plan in production
        for planet_id in plan.get("planet_ids", [])
        if planet_id in owned
    }


def _execute_structured_planet_plans(
    client,
    email: str,
    policy: dict,
    personality_key: str,
    tick_index: int,
    empire_state: Mapping,
    covered_production_planets: set[int],
    structured_outcomes: list[dict],
) -> tuple[list[str], bool]:
    plans = policy.get("plans")
    if not isinstance(plans, dict):
        return [], True
    production = plans.get("production", [])
    development = plans.get("development", [])
    production = production if action_enabled(policy, "production") else []
    development = development if action_enabled(policy, "development") else []
    if not production and not development:
        return [], True

    pending_development_receipts: list[dict[str, Any]] = []

    def record_development(
        raw_outcome: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        defer_persistence: bool = False,
        persist: bool = True,
    ) -> dict[str, Any]:
        receipt = post_actions._development_feedback_outcome(
            raw_outcome,
            email=email,
            tick_index=tick_index,
            command_id=(
                str(plan["command_id"])
                if isinstance(plan.get("command_id"), str)
                else None
            ),
            role=str(plan.get("role") or "balanced"),
            policy_activation_id=(
                str(policy["activation_id"])
                if isinstance(policy.get("activation_id"), str)
                else None
            ),
        )
        structured_outcomes.append(receipt)
        if not persist:
            return receipt
        if defer_persistence:
            pending_development_receipts.append(receipt)
        else:
            post_actions._persist_development_outcome(email, receipt)
        return receipt

    if not _empire_state_available(empire_state):
        structured_outcomes.extend(
            _structured_rejection("production", plan, "empire_state_unavailable")
            for plan in production
        )
        for plan in development:
            record_development(
                _structured_rejection(
                    "development", plan, "empire_state_unavailable"
                ),
                plan,
            )
        return [], True

    start_planet_id = planets_mod.current_planet_id(client)
    if start_planet_id is None:
        structured_outcomes.extend(
            _structured_rejection("production", plan, "start_planet_unknown")
            for plan in production
        )
        for plan in development:
            record_development(
                _structured_rejection(
                    "development", plan, "start_planet_unknown"
                ),
                plan,
            )
        return [], False
    planets = _empire_planets(empire_state)
    done: list[str] = []
    restore_ok = True
    try:
        for plan in production:
            options: list[dict] = []
            levels_by_planet: dict[int, dict[int, int]] = {}
            technology_id = plan["technology_id"]
            for planet_id in plan["planet_ids"]:
                if planet_id not in planets or not planets_mod.switch_planet(client, planet_id):
                    options.append({"planet_id": planet_id, "available": False})
                    continue
                try:
                    levels = miner_bot._collect_levels(client)
                    snapshot = production_capacity.production_queue_snapshot(email)
                    per_unit = production_capacity.unit_construction_seconds(
                        technology_id,
                        levels=levels,
                        economy_speed=snapshot.economy_speed,
                    )
                    capacity = production_capacity.cap_ship_amount_from_snapshot(
                        snapshot,
                        technology_id,
                        plan["amount"],
                        levels=levels,
                        horizon_seconds=plan["queue_horizon_seconds"],
                    )
                    levels_by_planet[planet_id] = levels
                    options.append({
                        "planet_id": planet_id,
                        "available": snapshot.available and snapshot.planet_id == planet_id,
                        "queue_remaining_seconds": snapshot.unit_queue_seconds,
                        "seconds_per_unit": per_unit,
                        "max_amount": capacity,
                    })
                except Exception:  # noqa: BLE001 - unavailable option is rejected by allocator
                    options.append({"planet_id": planet_id, "available": False})
            allocation = empire_scheduler.allocate_production_by_completion(
                plan["amount"], options
            )
            structured_outcomes.append({
                "action": "production",
                "ok": allocation.get("allocated_amount", 0) > 0,
                "status": allocation.get("status"),
                "reason": allocation.get("reason"),
                "command_id": plan["command_id"],
                "allocation": allocation,
            })
            for assigned in allocation.get("allocations", []):
                planet_id = assigned["planet_id"]
                if not planets_mod.switch_planet(client, planet_id):
                    structured_outcomes.append(
                        _structured_rejection("production", plan, "planet_switch_failed")
                    )
                    continue
                levels = levels_by_planet.get(planet_id)
                snapshot = production_capacity.production_queue_snapshot(email)
                amount = production_capacity.cap_ship_amount_from_snapshot(
                    snapshot,
                    technology_id,
                    assigned["amount"],
                    levels=levels or {},
                    horizon_seconds=plan["queue_horizon_seconds"],
                )
                action, message = miner_bot._try_first(
                    client,
                    [("ship", technology_id, amount)] if amount > 0 else [],
                )
                structured_outcomes.append({
                    "action": "production",
                    "ok": action is not None,
                    "status": "executed" if action is not None else "rejected",
                    "reason": "production_queued" if action is not None else "capacity_or_queue_rejected",
                    "command_id": plan["command_id"],
                    "planet_id": planet_id,
                    "requested_amount": assigned["amount"],
                    "accepted_amount": action[2] if action is not None else 0,
                    "message": message,
                })
                if action is not None:
                    done.append(miner_bot._fmt_action(action))

        for plan in development:
            planet_id = plan["planet_id"]
            planet = planets.get(planet_id)
            coordinates = planet.get("coordinates") if isinstance(planet, Mapping) else None
            previous, readable = post_actions._recent_command_status(
                email, plan["command_id"]
            )
            if not readable:
                record_development(
                    {
                        "ok": False,
                        "status": "stalled",
                        "reason": "development_feedback_state_unavailable",
                        "planet_id": planet_id,
                        "coordinates": coordinates,
                        "actions": [],
                    },
                    plan,
                    persist=False,
                )
                continue
            if previous in {
                "accepted",
                "queued",
                "completed",
                "executed",
                "dispatched",
                "sent",
                "confirmed",
            }:
                record_development(
                    {
                        "ok": True,
                        "status": "skipped",
                        "reason": "prior_development_command_outcome",
                        "planet_id": planet_id,
                        "coordinates": coordinates,
                        "actions": [],
                    },
                    plan,
                    persist=False,
                )
                continue
            if not isinstance(coordinates, Mapping) or not planets_mod.switch_planet(client, planet_id):
                record_development(
                    {
                        **_structured_rejection(
                            "development", plan, "planet_switch_failed"
                        ),
                        "planet_id": planet_id,
                        "coordinates": coordinates,
                        "actions": [],
                    },
                    plan,
                    defer_persistence=True,
                )
                continue
            colony_record = {
                "id": planet_id,
                "galaxy": coordinates.get("galaxy"),
                "system": coordinates.get("system"),
                "position": coordinates.get("position"),
            }
            development_policy = policy
            if planet_id in covered_production_planets:
                development_policy = dict(policy)
                selected = effective_action_plan(policy)
                development_policy["action_plan"] = [
                    name for name in (selected or ["build", "research", "defense"]) if name != "ship"
                ]
            try:
                result = colony_dev._build_on_colony(
                    client,
                    email,
                    personality_key,
                    tick_index,
                    colony_record,
                    f"{coordinates.get('galaxy')}:{coordinates.get('system')}:{coordinates.get('position')}",
                    policy=development_policy,
                    minimum_floors=plan["minimum_floors"],
                    role=plan["role"],
                    structured=True,
                )
            except Exception as exc:  # noqa: BLE001 - structured outcome, then restore
                result = _structured_rejection(
                    "development", plan, "development_error", error=type(exc).__name__
                )
            outcome = dict(result) if isinstance(result, Mapping) else {
                "ok": False, "status": "rejected", "reason": "invalid_development_outcome"
            }
            outcome.update({
                "action": "development",
                "command_id": plan["command_id"],
                "planet_id": planet_id,
                "coordinates": (
                    f"{coordinates.get('galaxy')}:{coordinates.get('system')}:"
                    f"{coordinates.get('position')}"
                ),
                "start_planet_id": start_planet_id,
            })
            record_development(
                outcome,
                plan,
                defer_persistence=True,
            )
            for action_note in outcome.get("actions", []):
                if isinstance(action_note, str):
                    done.append(action_note)
    finally:
        restore_ok = planets_mod.switch_planet(client, start_planet_id)
        for receipt in pending_development_receipts:
            receipt["restored_planet_id"] = start_planet_id if restore_ok else None
            receipt["restore_ok"] = bool(restore_ok)
            details = receipt.get("details")
            if isinstance(details, dict):
                details["restore_ok"] = bool(restore_ok)
                details["restored_planet_id"] = (
                    start_planet_id if restore_ok else None
                )
            post_actions._persist_development_outcome(email, receipt)
        if not restore_ok:
            structured_outcomes.append(_structured_rejection(
                "planet_restore", None, "start_planet_restore_failed",
                planet_id=start_planet_id,
            ))
    return done, restore_ok


def _structured_moon_row(
    empire_state: Mapping,
    moon_id: int,
    exact_coordinates: Mapping,
) -> Mapping | None:
    root = _empire_root(empire_state)
    raw_moons = root.get("moons")
    rows = raw_moons if isinstance(raw_moons, list) else root.get("worlds", [])
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("world_type", "moon") != "moon":
            continue
        observed_id = row.get("moon_id", row.get("world_id", row.get("id")))
        if observed_id != moon_id or isinstance(observed_id, bool):
            continue
        coordinates = row.get("coordinates")
        if not isinstance(coordinates, Mapping):
            coordinates = {
                "galaxy": row.get("galaxy"),
                "system": row.get("system"),
                "position": row.get("position"),
            }
        if _clean_plan_target(coordinates) == dict(exact_coordinates):
            return row
    return None


def _moon_reservation(
    ledger: empire_scheduler.ReservationLedger,
    plan: Mapping,
) -> dict | None:
    """Reserve only when the installed ledger explicitly exposes moon capacity."""
    try:
        summary = ledger.summary()
    except Exception:  # noqa: BLE001 - optional moon ledger capability
        return None
    resources_by_moon = summary.get("resources_by_moon")
    if not isinstance(resources_by_moon, Mapping) or str(plan["moon_id"]) not in resources_by_moon:
        return None
    try:
        reservation = ledger.reserve(
            plan["command_id"],
            planet_id=plan["moon_id"],
            resources=plan["cost"],
            building_queue=True,
        )
    except Exception as exc:  # noqa: BLE001 - reservation failure is fail closed
        return {
            "accepted": False,
            "reason": "moon-reservation-error",
            "error": type(exc).__name__,
        }
    if callable(getattr(reservation, "to_dict", None)):
        reservation = reservation.to_dict()
    return dict(reservation) if isinstance(reservation, Mapping) else {
        "accepted": False,
        "reason": "invalid-moon-reservation-result",
    }


def _execute_structured_moon_development(
    client,
    email: str,
    policy: dict,
    empire_state: Mapping,
    ledger: empire_scheduler.ReservationLedger,
    structured_outcomes: list[dict],
) -> tuple[list[str], bool]:
    plans = policy.get("plans")
    entries = plans.get("moon_development") if isinstance(plans, dict) else None
    if not isinstance(entries, list) or not entries:
        return [], True
    if not action_enabled(policy, "moon_development"):
        structured_outcomes.extend(
            _structured_rejection("moon_development", plan, "action_disabled")
            for plan in entries
        )
        return [], True
    if not _empire_state_available(empire_state):
        structured_outcomes.extend(
            _structured_rejection("moon_development", plan, "empire_state_unavailable")
            for plan in entries
        )
        return [], True

    # A policy tick submits one completed facility level globally. Remaining
    # valid commands stay pending for a later tick rather than cascading.
    plan = entries[0]
    for deferred in entries[1:]:
        structured_outcomes.append({
            "action": "moon_development",
            "ok": False,
            "status": "deferred",
            "reason": "one_step_per_tick",
            "command_id": deferred["command_id"],
            "moon_id": deferred["moon_id"],
        })
    moon = _structured_moon_row(
        empire_state,
        plan["moon_id"],
        plan["exact_coordinates"],
    )
    if moon is None:
        structured_outcomes.append(_structured_rejection(
            "moon_development",
            plan,
            "moon_identity_or_coordinates_unavailable",
            moon_id=plan["moon_id"],
            exact_coordinates=plan["exact_coordinates"],
        ))
        return [], True

    reservation = _moon_reservation(ledger, plan)
    if reservation is not None and reservation.get("accepted") is not True:
        structured_outcomes.append(_structured_rejection(
            "moon_development",
            plan,
            str(reservation.get("reason") or "moon_reservation_rejected"),
            moon_id=plan["moon_id"],
            reservation=reservation,
        ))
        return [], True

    root = _empire_root(empire_state)
    account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
    economy_speed = account.get("economy_speed", root.get("economy_speed", 1))
    if not isinstance(economy_speed, int) or isinstance(economy_speed, bool) or economy_speed < 1:
        economy_speed = 1
    try:
        result = moon_development.execute_moon_development_plan(
            client,
            email,
            {"commands": [plan]},
            economy_speed=economy_speed,
        )
    except Exception as exc:  # noqa: BLE001 - API outcome remains fail closed
        structured_outcomes.append(_structured_rejection(
            "moon_development",
            plan,
            "moon_development_error",
            moon_id=plan["moon_id"],
            error=type(exc).__name__,
            reservation=reservation,
        ))
        return [], False

    restore_ok = result.get("restore_ok") is True if isinstance(result, Mapping) else False
    raw_outcomes = result.get("outcomes") if isinstance(result, Mapping) else None
    outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
    notes: list[str] = []
    if not outcomes:
        structured_outcomes.append(_structured_rejection(
            "moon_development",
            plan,
            str(result.get("reason") or "missing_moon_development_outcome")
            if isinstance(result, Mapping) else "invalid_moon_development_outcome",
            moon_id=plan["moon_id"],
            restore_ok=restore_ok,
            reservation=reservation,
        ))
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        executed = outcome.get("executed") is True
        structured_outcomes.append({
            "action": "moon_development",
            "ok": executed and restore_ok,
            "status": "executed" if executed else "rejected",
            "reason": outcome.get("reason"),
            "command_id": plan["command_id"],
            "moon_id": plan["moon_id"],
            "facility": plan["facility"],
            "target_level": plan["target_level"],
            "restore_ok": restore_ok,
            "reservation": reservation,
        })
        if executed:
            notes.append(
                f"moon:{plan['moon_id']} {plan['facility']}->{plan['target_level']}"
            )
    if not restore_ok:
        structured_outcomes.append(_structured_rejection(
            "moon_restore",
            plan,
            "starting_world_restore_failed",
            moon_id=plan["moon_id"],
        ))
    return notes, restore_ok


def _optional_post_action_context(
    structured_plans: dict | None,
    empire_state: Mapping,
    reservation_ledger: empire_scheduler.ReservationLedger,
    reservation_summary: dict,
    structured_outcomes: list[dict],
) -> dict:
    candidates = {
        # Current structured API names.
        "plans": structured_plans,
        "empire_state": empire_state,
        "reservation_ledger": reservation_ledger,
        "outcomes": structured_outcomes,
        # Compatibility aliases for workers deployed on the earlier signature.
        "reservation_summary": reservation_summary,
        "structured_outcomes": structured_outcomes,
    }
    if structured_plans is not None:
        candidates["structured_plans"] = structured_plans
    try:
        signature = inspect.signature(post_actions.run_post_actions)
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    return {
        key: value
        for key, value in candidates.items()
        if accepts_kwargs or key in signature.parameters
    }


def _legacy_command_id(
    email: str,
    tick_index: int,
    action: str,
    identity: Any,
) -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    )
    raw = json.dumps(
        [email, tick_index, action, identity, stamp],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return f"legacy:{action}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _append_feedback(
    email: str,
    tick_index: int,
    action: str,
    status: str,
    *,
    command_id: str | None = None,
    **details: Any,
) -> None:
    payload = {
        "command_id": command_id
        or _legacy_command_id(email, tick_index, action, details),
        "action": action,
        "status": status,
    }
    if details:
        payload["details"] = details
    try:
        action_feedback.append_action_outcome(email, payload)
    except Exception:  # noqa: BLE001 - learning telemetry cannot stop game growth
        pass


def _record_queued_actions(
    email: str,
    tick_index: int,
    actions: list[tuple],
    *,
    planet_id: int | None,
) -> None:
    for action in actions:
        if not action:
            continue
        kind = str(action[0])
        details: dict[str, Any] = {
            "planet_id": planet_id,
            "object_id": int(action[1]) if len(action) > 1 else None,
        }
        if kind == "ship" and len(action) > 2:
            details["amount"] = int(action[2])
        _append_feedback(
            email,
            tick_index,
            kind,
            "accepted",
            **details,
        )


def _post_note_action(note: str) -> str | None:
    text = str(note)
    if "expedition SEND" in text:
        return "expedition"
    if "raid " in text and "SPY " in text:
        return "spy"
    if "raid " in text and "ATTACK" in text:
        return "raid"
    if "raid " in text and "HARVEST" in text:
        return "harvest"
    lowered = text.lower()
    if "colonize" in lowered and "idle" not in lowered:
        return "colonize"
    if "fleetsave" in lowered and "idle" not in lowered:
        return "fleetsave"
    if "defense" in lowered and "idle" not in lowered:
        return "defense"
    return None


# ---------------------------------------------------------------------------
# 집행 (한 틱)
# ---------------------------------------------------------------------------
def policy_tick(
    email: str,
    password: str = "botpass",
    tick_index: int | None = None,
    client: OGameClient | None = None,
) -> str:
    """AI 계정 1틱: 정책을 로드해 규칙봇 엔진으로 집행한다. 결과 한 줄 문자열 반환.

    로그인 실패는 예외로 올려 스케줄러가 스킵하게 한다(miner_bot.tick 과 동일).
    그 외의 조회/행동 실패는 흡수하고 폴백한다 — AI 정책이 비어도 제국은 성장한다.
    """
    policy = load_policy(email)
    try:
        empire_state = empire_scheduler.build_normalized_empire_state(email)
    except Exception as exc:  # noqa: BLE001 - structured execution must fail closed
        empire_state = {
            "schema_version": 1,
            "status": "unavailable",
            "reason": f"empire-state:{type(exc).__name__}",
            "planet_count": None,
            "canonical_home_id": None,
            "planets": [],
            "account": {},
        }
    try:
        action_reconciler.reconcile_queued_actions(email, empire_state)
    except Exception:  # noqa: BLE001 - reconciliation cannot stop the growth tick
        pass
    policy = _ensure_registered_ai_plan_contract(policy, email)
    _seed_automatic_moon_development(policy, empire_state)
    reservation_ledger = empire_scheduler.ReservationLedger(empire_state)
    structured_outcomes: list[dict] = []
    structured_plans = policy.get("plans")
    if isinstance(structured_plans, dict):
        # This marker is deliberately runtime-only and originates exclusively
        # in the AI policy engine. Rule/legacy bot contexts never receive it.
        policy[AI_STRUCTURED_CONTEXT_KEY] = True
        policy["structured_plans_enabled"] = True
        structured_outcomes.extend({
            "action": "policy_plan",
            "ok": False,
            "status": "rejected",
            **rejection,
        } for rejection in structured_plans.get("rejections", []))
    pkey = policy["personality"]
    label = PERSONALITIES[pkey]["label"]
    tag = f"AI:{label}"
    if policy["source"] != "policy":
        tag = f"AI:{label}/{policy['source']}"

    if tick_index is None:
        tick_index = miner_bot.default_tick_index()

    if client is None:
        client = OGameClient(email, password)
        client.login()

    # 자원 현황(현재값+에너지+저장 용량). DB 직독 우선, 실패 시 HTTP 폴백(에너지만).
    # resource_state 는 저장고 규칙·취소 사유 분류용 — AI 계정도 창고 정체에 걸리므로
    # 규칙봇과 동일하게 활성화한다 (miner_bot.tick 과 같은 배선).
    energy: int | None = None
    resource_state: dict | None = None
    try:
        try:
            resource_state = db_reader.get_resource_state_db(client.email)
            energy = resource_state.get("energy")
        except Exception:  # noqa: BLE001 - DB 실패 시 HTTP 폴백
            energy = client.get_status().get("energy")
    except Exception:  # noqa: BLE001 - 조회 실패는 치명적이지 않음
        energy = None

    # 이미 큐에 있는 건설 대상(저장고 중복 큐잉 방지). 실패해도 규칙은 동작.
    active_targets: set[int] = set()
    try:
        active_targets = db_reader.active_build_targets(client.email)
    except Exception:  # noqa: BLE001
        active_targets = set()

    # 레벨 조회 실패 → miner_bot 안전 모드(자원 건물만)로 폴백.
    try:
        levels = miner_bot._collect_levels(client)
    except Exception as exc:  # noqa: BLE001
        note = miner_bot._fallback_resource_tick(
            client, email, tag, pkey, tick_index, energy
        )
        return f"{note} [levels err: {type(exc).__name__}]"

    # 현재 행성 levels만으로 max_total을 판단하면 비행/식민지/건조대기 함선이 보이지 않아
    # 같은 batch를 반복 생산한다. 계정 합계를 못 읽어도 건설·연구는 계속하고, 합계가 필요한
    # 함선 curriculum/정책 항목만 아래에서 fail-closed한다.
    account_ship_totals: dict[int, int] | None = None
    try:
        account_ship_totals = db_reader.get_account_ship_totals_db(client.email)
    except Exception:  # noqa: BLE001 - 함선 건조만 보수적으로 정지
        account_ship_totals = None

    done: list[str] = []
    last_msg = ""
    current_planet_id = _current_empire_planet_id(empire_state)
    if current_planet_id is None and isinstance(resource_state, dict):
        observed_planet_id = resource_state.get("planet_id")
        if isinstance(observed_planet_id, int) and not isinstance(observed_planet_id, bool):
            current_planet_id = observed_planet_id

    colonization_prerequisites, colonization_research_reserved, colonization_unit_planets = (
        _reserve_colonization_plans(
            client,
            email,
            policy,
            empire_state,
            reservation_ledger,
            structured_outcomes,
        )
    )
    covered_production_planets = _covered_production_planets(policy, empire_state)
    resource_state = _resources_after_reservations(
        resource_state, reservation_ledger, current_planet_id
    )

    # 이번 틱 상태를 규칙봇과 **같은 빌더로 만든 같은 컨텍스트** 로 묶는다. 이렇게 해야
    # 계획 계층에 상태 파라미터가 추가돼도 AI 경로만 누락되는 회귀가 구조적으로 안 생긴다.
    # allow_satellite=True: 에너지 적자 시 plan_secondary 가 위성을 병행 건조(규칙봇과 동일).
    ctx = build_tick_context(
        pkey, levels, tick_index,
        energy=energy, resource_state=resource_state, email=email,
        active_targets=active_targets, allow_satellite=True,
        account_ship_totals=account_ship_totals,
        require_account_ship_totals=True,
        ship_strategy=policy["ship_strategy"],
    )

    # primary: 자원건물/시설 (저장고 규칙 활성 → 정책 build_priority 로 재정렬).
    # 저장고·선제에너지·시설 진행 접두부는 정책 순서보다 먼저 고정한다.
    primary = plan_primary(ctx=ctx) if action_enabled(policy, "build") else []
    protected_primary = _primary_safety_actions(
        primary, levels, energy, resource_state
    )
    primary = apply_build_priority(
        primary,
        policy["build_priority"],
        energy,
        policy["energy_target"],
        protected_actions=protected_primary,
    )
    acts, last_msg = miner_bot._queue_actions(
        client, primary, miner_bot.MAX_BUILD_PER_TICK, ctx=ctx,
        idle_fallback=True,
    ) if primary else ([], "build disabled")
    done += [miner_bot._fmt_action(a) for a in acts]
    _record_queued_actions(
        email, tick_index, acts, planet_id=current_planet_id
    )

    prerequisite_done, prerequisite_restore_ok = _execute_colonization_prerequisites(
        client,
        email,
        colonization_prerequisites,
        structured_outcomes,
    )
    done.extend(prerequisite_done)

    # secondary: 연구(연구 큐) + 함선(조선소 큐). 정책 우선순위로 재정렬/확장.
    # ctx 로 energy/resource_state 가 전달돼 위성 경로·연구 사전체크가 규칙봇과 동일하게 동작.
    secondary = plan_secondary(ctx=ctx)
    research_c = apply_research_priority(
        [a for a in secondary if a[0] == "research"], policy["research_priority"], levels
    ) if action_enabled(policy, "research") else []
    if colonization_research_reserved or miner_bot.research_blocked_by_lab(active_targets, acts):
        research_c = []
    # 정책이 공용 목표 밖 함선을 무한 추가하는 것은, 이번 틱 경제 행동을 실제로 하나
    # 성공했고 성격별 최소 식민이 끝난 뒤에만 허용한다. 공용 GROWTH 함선 후보는 이 가드와
    # 무관하게 그대로 남으므로 초기 정찰/화물/전투 준비는 규칙봇과 동일하게 진행된다.
    allow_policy_ship_extras = (
        policy["autonomy"] == "full"
        or (bool(acts) and _identity_planets_ready(email, pkey))
    )
    ship_c = apply_ship_priority(
        [a for a in secondary if a[0] == "ship"], policy["ship_priority"], levels,
        resource_state=resource_state, energy=energy, energy_target=policy["energy_target"],
        allow_policy_extras=allow_policy_ship_extras,
        account_ship_totals=account_ship_totals,
        enforce_max_total_on_shared=policy["autonomy"] != "full",
    ) if action_enabled(policy, "ship") else []
    if (
        not prerequisite_restore_ok
        or current_planet_id in colonization_unit_planets
        or current_planet_id in covered_production_planets
    ):
        ship_c = []
    res_acts, msg_r = miner_bot._queue_actions(
        client, research_c, miner_bot.MAX_RESEARCH_PER_TICK, ctx=ctx,
    )
    done += [miner_bot._fmt_action(a) for a in res_acts]
    _record_queued_actions(
        email, tick_index, res_acts, planet_id=current_planet_id
    )

    structured_done, structured_restore_ok = _execute_structured_planet_plans(
        client,
        email,
        policy,
        pkey,
        tick_index,
        empire_state,
        covered_production_planets,
        structured_outcomes,
    )
    done.extend(structured_done)
    if not structured_restore_ok:
        ship_c = []
    ship_act, msg_s = miner_bot._try_first(client, ship_c)
    if ship_act is not None:
        done.append(miner_bot._fmt_action(ship_act))
        _record_queued_actions(
            email, tick_index, [ship_act], planet_id=current_planet_id
        )
    if not last_msg:
        last_msg = msg_r or msg_s

    moon_done, _moon_restore_ok = _execute_structured_moon_development(
        client,
        email,
        policy,
        empire_state,
        reservation_ledger,
        structured_outcomes,
    )
    done.extend(moon_done)

    # 성격별 행동 + 플릿세이브 + 식민지 개발을 규칙봇과 같은 함수/순서로 실행한다.
    # 정책별 aggression/defense_ratio만 명시적 오버라이드로 전달한다.
    reservation_summary = reservation_ledger.summary()
    policy["empire_state"] = empire_state
    policy["reservation_summary"] = reservation_summary
    policy["structured_outcomes"] = structured_outcomes
    policy["structured_production_planet_ids"] = sorted(covered_production_planets)
    post_action_plans = structured_plans
    if isinstance(structured_plans, dict):
        post_action_plans = dict(structured_plans)
        if "moon_development" in post_action_plans:
            post_action_plans["moon_development"] = []
        if "development" in post_action_plans:
            post_action_plans["development"] = []
    if isinstance(post_action_plans, dict):
        policy["structured_plans"] = post_action_plans
    optional_post_context = _optional_post_action_context(
        post_action_plans if isinstance(post_action_plans, dict) else None,
        empire_state,
        reservation_ledger,
        reservation_summary,
        structured_outcomes,
    )
    post_notes = post_actions.run_post_actions(
        client,
        email,
        levels,
        pkey,
        tick_index,
        energy=energy,
        raid_options=policy["aggression"],
        defense_ratio=policy["defense_ratio"],
        policy=policy,
        **optional_post_context,
    )
    done.extend(post_notes)
    for note in post_notes:
        feedback_action = _post_note_action(note)
        if feedback_action is not None:
            _append_feedback(
                email,
                tick_index,
                feedback_action,
                "dispatched",
                note=str(note)[:500],
            )

    if action_enabled(policy, "diplomacy"):
        diplomacy_outcomes, diplomacy_notes = diplomacy.execute_policy(
            client, email, policy.get("diplomacy")
        )
        done.extend(diplomacy_notes)
        for outcome in diplomacy_outcomes:
            _append_feedback(
                email,
                tick_index,
                "diplomacy",
                str(outcome.get("status") or "unknown"),
                command_id=str(outcome.get("command_id") or "") or None,
                kind=outcome.get("kind"),
                reason=outcome.get("reason"),
                target_player_id=outcome.get("target_player_id"),
                result=outcome.get("details"),
            )

    intent = f" «{policy['note']}»" if policy["note"] else ""
    if done:
        return f"{email} [{tag}] {' + '.join(done)} E={energy}{intent}"
    return f"{email} [{tag}] nothing buildable (last: {last_msg}) E={energy}{intent}"


if __name__ == "__main__":
    import sys

    _email = sys.argv[1] if len(sys.argv) > 1 else "ai01@bots.local"
    _password = sys.argv[2] if len(sys.argv) > 2 else "botpass"
    print(policy_tick(_email, _password))
