"""규칙봇과 AI 정책봇이 공유하는 성격별 후행 행동 파이프라인.

계획 계층은 TickContext로 통일됐지만 raid/원정/식민/플릿세이브/식민지 개발은 두 tick 함수에
복제돼 있었다. 새 훅을 한쪽에만 추가하는 회귀를 구조적으로 막기 위해 이 모듈 한 곳에서 순서와
인자를 관리한다.

순서:
  0) 미배정 AI 캐릭터 클래스(함대가 모두 귀환한 틱에 무료 선택)
  1) 성격 정체성 행동(raid/defense/expedition/colonize)
     - raider는 raid/spy 뒤 남는 슬롯으로 2번째 행성까지만 식민 체인을 병행한다.
  2) 남은 파킹 함대 플릿세이브
  3) 행성 전환이 필요한 식민지 개발(항상 마지막, 홈월드 복귀 보장)
  4) 계정별 12틱에 한 번 금속 sink read-only shadow 관측(집행 없음)
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from bots.rule_based import (
    colony,
    colony_dev,
    explorer,
    fleetsave,
    intercept_planner,
    metal_sink_observer,
    miner_defense,
    phalanx_intel,
    raider,
    resource_transfer,
    strategic_growth,
    turtle,
)
from bots.rule_based.personality import FLEETSAVE_MIN_VALUE, experimental_profile
from bots.shared import db_reader, moons
from bots.ai_agents.action_catalog import effective_action_plan
from bots.ai_agents import action_feedback, deathstar_goal


# 단일행성 raider는 안전한 자기 식민지 Transport 플릿세이브가 불가능하다. 두 번째 행성만
# 확보하면 목적이 달성되므로 난개발 없이 2에서 멈추고, 식민 파견 뒤에도 raid/spy 1슬롯을
# 남긴다(연구·식민선 준비 단계는 슬롯을 쓰지 않음).
RAIDER_FLEETSAVE_PLANETS = 2
RAIDER_RESERVED_FLEET_SLOTS = 1

# 실전 전투는 호출부와 raider 코어의 이중 allowlist를 모두 통과해야 한다. 첫 카나리는 이미
# 다행성이고 full-intel 표본이 있는 ai06 하나뿐이며, 나머지 계정은 battle_mode 기본값(log)을
# 그대로 사용한다. 확대할 때는 두 목록을 의도적으로 함께 리뷰한다.
RAIDER_LIVE_CANARY_EMAILS = frozenset({"ai06@bots.local"})

# 규칙봇도 성격을 행동 금지 목록으로 쓰지 않는다. 성격은 아래 최소 약탈가치와 원정
# 노출 비율의 차이만 만들며, 실제 공격은 full-intel 순EV 계산이 최종 선택한다.
RULE_MIN_LOOT = {
    "raider": 1_500,
    "hybrid": 5_000,
    "explorer": 8_000,
    "explorer_aggressive": 3_000,
    "miner": 12_000,
    "turtle": 15_000,
}
RULE_EXPEDITION_RISK = {
    "raider": 0.20,
    "hybrid": 0.30,
    # 원정 보상은 전투가치가 아니라 적재량에 막힌다. 탐험가는 현재 화물/PF를
    # 남은 서버 슬롯에 균등 배분하도록 공용 편성기가 보호하므로 인공 55% 상한을
    # 두지 않고, 실제 전체 노출은 슬롯별 분배와 서버 슬롯 한도가 결정한다.
    "explorer": 1.0,
    "explorer_aggressive": 1.0,
    "miner": 0.25,
    "turtle": 0.15,
}
RULE_RAID_CONTROLS = {
    "limits": {
        "spy_global_cooldown_sec": raider.SPY_GLOBAL_COOLDOWN_SEC,
        "spy_target_cooldown_sec": raider.SPY_COOLDOWN_SEC,
        "attack_target_cooldown_sec": raider.ATTACK_COOLDOWN_SEC,
        "bashing_max_attacks": raider.BASH_MAX_ATTACKS,
        "fleet_risk_percent": 100,
        "min_fleet_value_limit": 0,
        "max_attack_eta_sec": 0,
    },
    "targeting": {
        "allow_strong_targets": True,
        "allow_defended_targets": True,
        "excluded_systems": [],
    },
}



def raid_options_for_account(email: str) -> dict:
    if experimental_profile(email) == "raid_aggressive":
        return {"min_loot": 1500}
    return {}

# OGameX CharacterClass enum: Collector=1, General=2, Discoverer=3.
PERSONALITY_CLASS_ID = {
    "miner": 1,
    "turtle": 1,
    "raider": 2,
    "hybrid": 2,
    "explorer": 3,
}
CLASS_NAME = {1: "COLLECTOR", 2: "GENERAL", 3: "DISCOVERER"}
_CLASS_CONFIRMED: set[str] = set()
_NAMED_AI_EMAILS = {"pirate01@bots.local", "explorer01@bots.local"}


def _managed_ai_account(email: str) -> bool:
    """Return whether the account is controlled by the LLM policy layer."""
    normalized = email.strip().lower()
    local = normalized.split("@", 1)[0]
    return normalized in _NAMED_AI_EMAILS or (
        local.startswith("ai") and local[2:].isdigit()
    )


def _levels_after_dispatch(
    levels: dict[int, int], dispatch_state: dict | None,
) -> dict[int, int]:
    """방금 출발한 함선을 현재 주기장 스냅샷에서 제거한다."""
    updated = dict(levels)
    if not isinstance(dispatch_state, dict) or dispatch_state.get("sent") is not True:
        return updated
    ships = dispatch_state.get("ships")
    if not isinstance(ships, dict):
        return updated
    for tid, amount in ships.items():
        ship_id = int(tid)
        updated[ship_id] = max(0, int(updated.get(ship_id, 0) or 0) - int(amount))
    return updated


def _levels_without_reservation(
    levels: dict[int, int], reserved_ships: dict[int, int],
) -> dict[int, int]:
    """미완성 원정 슬롯용 함선을 제외한 약탈 가용 스냅샷."""
    available = dict(levels)
    for tid, amount in reserved_ships.items():
        available[int(tid)] = max(
            0, int(available.get(int(tid), 0) or 0) - max(0, int(amount))
        )
    return available


def _raid_slot_cap(
    email: str,
    levels: dict[int, int],
    requested_cap: int | None,
    reserved_slots: int,
) -> int:
    """원정 예약분을 뺀 일반 함대 슬롯까지만 raid/spy가 사용하게 한다."""
    server_cap = explorer.fleet_slots(levels, email)
    available_cap = max(0, server_cap - max(0, int(reserved_slots)))
    if requested_cap is None:
        return available_cap
    return min(max(0, int(requested_cap)), available_cap)


MIN_RAID_RESERVED_SLOTS = 1


def _leave_raid_capacity(
    reserved_slots: int,
    reserved_ships: dict[int, int],
    *,
    enabled: bool,
) -> tuple[int, dict[int, int]]:
    """Release one planned expedition slot and its proportional ships for raids."""
    slots = max(0, int(reserved_slots))
    if not enabled or slots <= 0:
        return slots, dict(reserved_ships)
    kept = max(0, slots - MIN_RAID_RESERVED_SLOTS)
    if kept <= 0:
        return 0, {}
    return kept, {
        int(ship_id): max(0, int(amount) * kept // slots)
        for ship_id, amount in reserved_ships.items()
        if int(amount) * kept // slots > 0
    }


_STRUCTURED_PLAN_KEYS = {
    "colonize": (
        "colonize",
        "colonization",
        "colonize_plan",
        "colonization_plan",
        "colonization_plans",
    ),
    "transfer": (
        "transfer",
        "transfers",
        "transfer_plan",
        "transfer_plans",
        "resource_transfer",
        "resource_transfers",
    ),
    "development": (
        "development",
        "development_plan",
        "development_plans",
        "develop_colonies",
        "colony_development",
        "colony_development_plans",
    ),
    "phalanx_scans": (
        "phalanx_scans",
        "phalanx_scan",
        "phalanx_scan_plans",
    ),
    "jump_gate": (
        "jump_gate",
        "jump_gates",
        "jump_gate_plan",
        "jump_gate_plans",
    ),
    "intercepts": (
        "intercepts",
        "intercept",
        "intercept_plan",
        "intercept_plans",
    ),
}


def _plan_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        nested = value.get("plans")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            return [item for item in nested if isinstance(item, Mapping)]
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _plan_kind(plan: Mapping[str, Any]) -> str | None:
    marker = str(
        plan.get("action") or plan.get("kind") or plan.get("type") or ""
    ).strip().lower()
    if "exact_target" in plan or marker in {"colonize", "colonization"}:
        return "colonize"
    if marker in {"transfer", "transport", "resource_transfer"} or (
        "resources" in plan
        and "source_planet_id" in plan
        and (
            "destination_planet_id" in plan
            or "destination_coordinates" in plan
        )
    ):
        return "transfer"
    if marker in {
        "development",
        "develop",
        "develop_colonies",
        "colony_development",
    } or "minimum_floors" in plan:
        return "development"
    if marker in {"phalanx", "phalanx_scan", "scan_phalanx"}:
        return "phalanx_scans"
    if marker in {"jump_gate", "jump_gate_transfer", "moon_jump"}:
        return "jump_gate"
    if marker in {"intercept", "return_intercept", "timed_intercept"}:
        return "intercepts"
    return None


def _group_structured_plans(plans: Any) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {
        kind: [] for kind in _STRUCTURED_PLAN_KEYS
    }
    if isinstance(plans, Mapping):
        matched_container = False
        for kind, keys in _STRUCTURED_PLAN_KEYS.items():
            for key in keys:
                if key in plans:
                    grouped[kind].extend(_plan_list(plans[key]))
                    matched_container = True
        if matched_container:
            return grouped
    for plan in _plan_list(plans):
        kind = _plan_kind(plan)
        if kind is not None:
            grouped[kind].append(plan)
    return grouped


def _compact_outcome(action: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        "action": action,
        "ok": bool(outcome.get("ok")),
        "status": str(outcome.get("status") or "unknown"),
        "reason": outcome.get("reason"),
    }
    for key in (
        "command_id",
        "phase",
        "exact_target",
        "planet_id",
        "coordinates",
        "start_planet_id",
        "home_planet_id",
        "retry_safe",
        "restore_ok",
        "restored_planet_id",
        "warnings",
        "observation_id",
        "source_moon_id",
        "destination_moon_id",
        "target_coordinates",
        "ambiguous",
    ):
        if outcome.get(key) is not None:
            compact[key] = outcome[key]
    actions = outcome.get("actions")
    if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
        compact["action_count"] = len(actions)
        if action == "development":
            compact["actions"] = list(actions)
    return compact


def _append_outcome(
    outcomes: list[dict[str, Any]] | None,
    action: str,
    outcome: Mapping[str, Any],
) -> None:
    if outcomes is not None:
        outcomes.append(_compact_outcome(action, outcome))


def _feedback_outcome(action: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "command_id": outcome.get("command_id"),
        "action": action,
        "status": str(outcome.get("status") or "unknown"),
        "reason": outcome.get("reason"),
    }
    details = {
        key: outcome[key]
        for key in (
            "observation_id",
            "planet_id",
            "coordinates",
            "start_planet_id",
            "home_planet_id",
            "restored_planet_id",
            "restore_ok",
            "actions",
            "source_moon_id",
            "destination_moon_id",
            "target_coordinates",
            "retry_safe",
            "ambiguous",
        )
        if outcome.get(key) is not None
    }
    if details:
        feedback["details"] = details
    if outcome.get("ambiguous") is not None:
        feedback["ambiguous"] = bool(outcome.get("ambiguous"))
    return feedback


def _persist_action_outcome(
    email: str, action: str, outcome: Mapping[str, Any],
) -> bool:
    """Best-effort compact AI feedback; callers decide whether persistence is mandatory."""
    if not isinstance(outcome.get("command_id"), str):
        return False
    append = getattr(action_feedback, "append_action_outcome", None)
    if not callable(append):
        return False
    try:
        append(email, _feedback_outcome(action, outcome))
    except Exception:  # noqa: BLE001 - feedback failure is reported by the caller
        return False
    return True


def _record_structured_outcome(
    email: str,
    outcomes: list[dict[str, Any]] | None,
    action: str,
    outcome: Mapping[str, Any],
) -> None:
    _append_outcome(outcomes, action, outcome)
    _persist_action_outcome(email, action, outcome)


def _development_command_id(
    email: str,
    tick_index: int,
    visit_index: int,
    outcome: Mapping[str, Any],
) -> str:
    identity = {
        "email": email.strip().lower(),
        "tick_index": int(tick_index),
        "visit_index": int(visit_index),
        "planet_id": outcome.get("planet_id"),
        "coordinates": outcome.get("coordinates", outcome.get("coordinate")),
        "role": outcome.get("role"),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"legacy:development:{digest}"


def _development_feedback_outcome(
    outcome: Mapping[str, Any] | str | None,
    *,
    email: str,
    tick_index: int,
    visit_index: int = 0,
    command_id: str | None = None,
    role: str | None = None,
    policy_activation_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one colony visit without calling queued work completed."""
    if isinstance(outcome, Mapping):
        source = dict(outcome)
    elif isinstance(outcome, str) and outcome:
        source = {
            "ok": True,
            "status": "executed",
            "reason": "legacy_note_only",
            "actions": [outcome],
            "note": outcome,
        }
    else:
        source = {
            "ok": True,
            "status": "idle",
            "reason": "no_development_outcome",
            "actions": [],
        }

    executor_status = str(source.get("status") or "unknown").strip().lower()
    status = {
        "executed": "queued",
        "accepted": "queued",
        "success": "queued",
        "queued": "queued",
        "completed": "completed",
        "idle": "skipped",
        "skipped": "skipped",
        "waiting": "skipped",
        "deferred": "skipped",
        "rejected": "failed",
        "error": "failed",
        "failed": "failed",
        "stalled": "stalled",
        "ambiguous": "stalled",
    }.get(executor_status, "stalled")
    coordinate = source.get("coordinates", source.get("coordinate"))
    actions = source.get("actions")
    action_details = (
        list(actions)
        if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes))
        else []
    )
    raw_queued_actions = source.get("queued_actions")
    queued_actions = (
        [dict(item) for item in raw_queued_actions if isinstance(item, Mapping)]
        if isinstance(raw_queued_actions, Sequence)
        and not isinstance(raw_queued_actions, (str, bytes))
        else []
    )
    raw_untracked = source.get(
        "untracked_action_count",
        max(0, len(action_details) - len(queued_actions)),
    )
    untracked_action_count = (
        max(0, raw_untracked)
        if isinstance(raw_untracked, int) and not isinstance(raw_untracked, bool)
        else len(action_details)
    )
    resolved_command_id = (
        command_id.strip()
        if isinstance(command_id, str) and command_id.strip()
        else _development_command_id(
            email,
            tick_index,
            visit_index,
            {
                **source,
                "coordinates": coordinate,
                "role": role or source.get("role"),
            },
        )
    )
    receipt: dict[str, Any] = {
        "command_id": resolved_command_id,
        "action": "development",
        "status": status,
        "lifecycle": status,
        "reason": source.get("reason") or "development_outcome_unknown",
        "ok": status in {"queued", "completed"},
        "planet_id": source.get("planet_id"),
        "coordinates": coordinate,
        "start_planet_id": source.get("start_planet_id"),
        "home_planet_id": source.get("home_planet_id"),
        "restored_planet_id": source.get("restored_planet_id"),
        "restore_ok": source.get("restore_ok"),
        "role": role or source.get("role") or "balanced",
        "actions": action_details,
        "details": {
            "executor_status": executor_status,
            "actions": action_details,
            "queued_actions": queued_actions,
            "untracked_action_count": untracked_action_count,
            "note": source.get("note"),
            "energy": source.get("energy"),
            "error": source.get("error"),
        },
    }
    if isinstance(policy_activation_id, str) and policy_activation_id.strip():
        receipt["policy_activation_id"] = policy_activation_id.strip()
    return receipt


def _persist_development_outcome(
    email: str,
    outcome: Mapping[str, Any],
) -> bool:
    append = getattr(action_feedback, "append_action_outcome", None)
    if not callable(append):
        return False
    try:
        append(email, outcome)
    except Exception:  # noqa: BLE001 - feedback must not stop colony restoration
        return False
    return True


def _record_development_outcome(
    email: str,
    outcomes: list[dict[str, Any]] | None,
    raw_outcome: Mapping[str, Any] | str | None,
    *,
    tick_index: int,
    visit_index: int = 0,
    command_id: str | None = None,
    role: str | None = None,
    policy_activation_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    receipt = _development_feedback_outcome(
        raw_outcome,
        email=email,
        tick_index=tick_index,
        visit_index=visit_index,
        command_id=command_id,
        role=role,
        policy_activation_id=policy_activation_id,
    )
    if outcomes is not None:
        outcomes.append(dict(receipt))
    if persist:
        _persist_development_outcome(email, receipt)
    return receipt


def _run_legacy_development_visit(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    visit_index: int,
    *,
    policy: dict | None,
    outcomes: list[dict[str, Any]] | None,
) -> str | None:
    try:
        raw = colony_dev.colony_dev_tick(
            client,
            email,
            personality_key,
            tick_index,
            policy=policy,
            structured=True,
        )
    except Exception as exc:  # defensive for replaced/custom executors
        raw = {
            "ok": False,
            "status": "error",
            "reason": "colony_development_error",
            "error": f"{type(exc).__name__}: {exc}",
            "actions": [],
        }
    receipt = _record_development_outcome(
        email,
        outcomes,
        raw,
        tick_index=tick_index,
        visit_index=visit_index,
        role="balanced",
        policy_activation_id=(
            str(policy["activation_id"])
            if isinstance(policy, Mapping)
            and isinstance(policy.get("activation_id"), str)
            else None
        ),
        # The policy engine always supplies its shared outcome collector.
        # Standalone/tests/rule paths without that context must not write into
        # the live AI learning store.
        persist=_managed_ai_account(email) and outcomes is not None,
    )
    if isinstance(raw, Mapping):
        note = raw.get("note")
        if isinstance(note, str) and note:
            return note
        if receipt["status"] == "failed" and raw.get("error"):
            return f"colonydev err {raw['error']}"
        return None
    return raw if isinstance(raw, str) and raw else None


def _ledger_command(
    reservation_ledger: Any, command_id: Any,
) -> Mapping[str, Any] | None:
    if not isinstance(command_id, str) or not command_id:
        return None
    try:
        summary = (
            reservation_ledger.summary()
            if callable(getattr(reservation_ledger, "summary", None))
            else reservation_ledger
        )
    except Exception:  # noqa: BLE001 - an optional arbiter must fail closed
        return {"accepted": False, "reason": "reservation-ledger-unavailable"}
    if not isinstance(summary, Mapping):
        return None
    commands = summary.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        return None
    return next(
        (
            item for item in commands
            if isinstance(item, Mapping) and item.get("command_id") == command_id
        ),
        None,
    )


def _reserve_colonization_slot(
    reservation_ledger: Any, command_id: Any,
) -> Mapping[str, Any] | None:
    if reservation_ledger is None:
        return None
    existing = _ledger_command(reservation_ledger, command_id)
    if existing is not None:
        return existing
    reserve = getattr(reservation_ledger, "reserve", None)
    if not callable(reserve):
        return {"accepted": False, "reason": "reservation-ledger-unavailable"}
    try:
        result = reserve(str(command_id), fleet_slots=1)
    except Exception as exc:  # noqa: BLE001 - do not dispatch around arbiter failure
        return {
            "accepted": False,
            "reason": "reservation-ledger-error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if callable(getattr(result, "to_dict", None)):
        result = result.to_dict()
    return result if isinstance(result, Mapping) else {
        "accepted": False,
        "reason": "invalid-reservation-result",
    }


def _structured_enabled(policy: dict | None, plans: Any) -> bool:
    if plans is not None:
        return True
    if not isinstance(policy, Mapping):
        return False
    return any(
        policy.get(key) is True
        for key in (
            "structured",
            "structured_post_actions",
            "structured_plans_enabled",
        )
    )


def _colony_development_visits(email: str) -> int:
    """Run several independent colony queues per tick without an HTTP stampede."""
    try:
        from bots.shared import db_reader

        colony_count = max(0, len(db_reader.list_planets_db(email)) - 1)
    except Exception:  # noqa: BLE001 - keep the legacy single-visit fallback
        return 1
    return min(3, colony_count)


def _policy_structured_plans(policy: dict | None, plans: Any) -> Any:
    if plans is not None or not isinstance(policy, Mapping):
        return plans
    configured = policy.get("structured_post_actions")
    if isinstance(configured, Mapping) and "plans" in configured:
        return configured.get("plans")
    return policy.get("structured_plans")


def _colonization_options(policy: dict | None) -> dict[str, Any]:
    active_policy = policy if isinstance(policy, Mapping) else {}
    limits = (
        active_policy.get("limits")
        if isinstance(active_policy.get("limits"), Mapping)
        else {}
    )
    return {
        "max_planets": limits.get("max_planets", colony.MAX_PLANETS),
        "reserve_fleet_slots": limits.get("reserve_fleet_slots", 0),
        "enforce_strategy_cap": active_policy.get("autonomy") != "full",
        "cooldown_sec": limits.get(
            "colonize_cooldown_sec", colony.COLONIZE_COOLDOWN_SEC
        ),
    }


def _run_structured_colonization(
    client,
    email: str,
    levels: dict[int, int],
    plan: Mapping[str, Any] | None,
    *,
    policy: dict | None,
    reservation_ledger: Any,
    outcomes: list[dict[str, Any]] | None,
) -> int:
    """Run one exact colony command and return its pending slot reservation."""
    if plan is None:
        return 0
    comparison = plan.get("comparison")
    if plan.get("phase") == "deferred" or (
        isinstance(comparison, Mapping)
        and comparison.get("recommendation") == "invest_existing"
    ):
        _append_outcome(outcomes, "colonize", {
            "ok": False,
            "status": "rejected",
            "reason": "policy_deferred",
            "command_id": plan.get("command_id"),
            "phase": plan.get("phase"),
            "exact_target": plan.get("exact_target"),
        })
        return 0
    options = _colonization_options(policy)
    try:
        prepared = colony.prepare_colonization(
            client, email, levels, plan, **options
        )
    except Exception as exc:  # noqa: BLE001 - structured rejection, no fallback target
        prepared = {
            "ok": False,
            "status": "rejected",
            "reason": "colonization_prepare_error",
            "command_id": plan.get("command_id"),
            "exact_target": plan.get("exact_target"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    if prepared.get("status") not in {"ready", "waiting"}:
        _append_outcome(outcomes, "colonize", prepared)
        return 0

    reservation = _reserve_colonization_slot(
        reservation_ledger, plan.get("command_id")
    )
    if isinstance(reservation, Mapping) and reservation.get("accepted") is False:
        rejected = dict(prepared)
        rejected.update({
            "ok": False,
            "status": "rejected",
            "reason": "fleet_slot_reservation_rejected",
            "reservation_reason": reservation.get("reason"),
        })
        _append_outcome(outcomes, "colonize", rejected)
        return 1
    if prepared.get("status") == "waiting":
        _append_outcome(outcomes, "colonize", prepared)
        return 1

    try:
        result = colony.execute_colonization_plan(
            client, email, levels, plan, **options
        )
    except Exception as exc:  # noqa: BLE001 - never substitute or retry an exact target
        result = {
            "ok": False,
            "status": "rejected",
            "reason": "colonization_execute_error",
            "command_id": plan.get("command_id"),
            "exact_target": plan.get("exact_target"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    _append_outcome(outcomes, "colonize", result)
    return 0 if result.get("status") == "dispatched" else 1


def _transfer_limit(policy: dict | None) -> int:
    if not isinstance(policy, Mapping):
        return 1
    config = policy.get("resource_transfer")
    configured = (
        config.get("max_per_tick") if isinstance(config, Mapping) else None
    )
    if configured is None:
        limits = policy.get("limits")
        configured = (
            limits.get("max_transfers_per_tick")
            if isinstance(limits, Mapping)
            else None
        )
    try:
        return max(0, min(10, int(1 if configured is None else configured)))
    except (TypeError, ValueError, OverflowError):
        return 1


def _transfer_options(policy: dict | None, reservation_ledger: Any) -> dict[str, Any]:
    config = (
        policy.get("resource_transfer")
        if isinstance(policy, Mapping)
        and isinstance(policy.get("resource_transfer"), Mapping)
        else {}
    )
    options: dict[str, Any] = {}
    for key in (
        "minimum_cargo_reserve",
        "maximum_cargo_ships",
        "emergency_slot_reserve",
        "fuel_safety_factor",
    ):
        if key in config:
            options[key] = config[key]
    ledger_key = config.get("ledger_key")
    if ledger_key is None and isinstance(reservation_ledger, str):
        ledger_key = reservation_ledger
    if isinstance(ledger_key, str) and ledger_key:
        options["ledger_key"] = ledger_key
    return options


def _run_structured_transfers(
    client,
    email: str,
    levels: dict[int, int],
    plans: Sequence[Mapping[str, Any]],
    *,
    policy: dict | None,
    reservation_ledger: Any,
    colonization_slot_reservation: int,
    outcomes: list[dict[str, Any]] | None,
) -> dict[int, int]:
    available = dict(levels)
    options = _transfer_options(policy, reservation_ledger)
    for plan in list(plans)[:_transfer_limit(policy)]:
        reserved = _ledger_command(reservation_ledger, plan.get("command_id"))
        if isinstance(reserved, Mapping) and reserved.get("accepted") is False:
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "reservation_rejected",
                "command_id": plan.get("command_id"),
                "retry_safe": False,
            }
        else:
            adapted_plan = {
                "command_id": plan.get("command_id"),
                "source_planet_id": plan.get("source_planet_id"),
                "resources": dict(plan.get("resources") or {}),
                "reserve_floors": dict(plan.get("reserve_floors") or {
                    "metal": 0, "crystal": 0, "deuterium": 0,
                }),
                "purpose": str(plan.get("purpose") or "empire_rebalance"),
                "speed": plan.get("speed", 10),
            }
            if plan.get("target_planet_id") is not None:
                adapted_plan["destination_planet_id"] = plan.get("target_planet_id")
            else:
                adapted_plan["destination_coordinates"] = plan.get("exact_target")
            if plan.get("deadline") is not None:
                adapted_plan["deadline"] = plan.get("deadline")
            result = resource_transfer.execute_transfer_plan(
                client,
                email,
                adapted_plan,
                colonization_slot_reservation=colonization_slot_reservation,
                **options,
            )
        _append_outcome(outcomes, "transfer", result)
        if result.get("status") in {"sent", "confirmed"}:
            dispatch = result.get("dispatch")
            if isinstance(dispatch, Mapping):
                available = _levels_after_dispatch(available, {
                    "sent": True,
                    "ships": dispatch.get("ships", {}),
                })
        if result.get("status") == "ambiguous":
            break
    return available


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and not isinstance(value, bool) else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and not isinstance(value, bool) else None


def _moon_id(plan: Mapping[str, Any], prefix: str) -> int | None:
    value = plan.get(f"{prefix}_moon_id")
    nested = plan.get(f"{prefix}_moon", plan.get(prefix))
    if value is None and isinstance(nested, Mapping):
        value = nested.get("moon_id", nested.get("id"))
    return _positive_int(value)


def _coordinates(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, Mapping) and isinstance(value.get("coordinates"), Mapping):
        value = value["coordinates"]
    try:
        return phalanx_intel.normalize_coordinate(value)
    except Exception:  # noqa: BLE001 - malformed AI plans fail closed
        return None


def _moon_context(empire_state: Any, moon_id: int) -> Mapping[str, Any] | None:
    """Find one moon row without treating a same-id planet as moon state."""
    seen: set[int] = set()

    def visit(value: Any) -> Mapping[str, Any] | None:
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        if isinstance(value, Mapping):
            candidate_id = value.get("moon_id", value.get("id"))
            moon_marked = (
                value.get("object_type") == "moon"
                or value.get("type") == "moon"
                or "sensor_phalanx_level" in value
                or "jump_gate_level" in value
                or "lunar_base_level" in value
            )
            if moon_marked and _positive_int(candidate_id) == moon_id:
                return value
            for key in ("normalized_state", "moons", "moon", "planets"):
                found = visit(value.get(key)) if key in value else None
                if found is not None:
                    return found
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                found = visit(item)
                if found is not None:
                    return found
        return None

    return visit(empire_state)


def _moon_deuterium(
    plan: Mapping[str, Any], moon_state: Mapping[str, Any] | None,
) -> int | None:
    source = plan.get("source_moon")
    candidates = [plan, source, moon_state]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for container in (candidate, candidate.get("resources"), candidate.get("resource_state")):
            if not isinstance(container, Mapping):
                continue
            value = container.get("deuterium_available", container.get("deuterium"))
            parsed = _nonnegative_int(value)
            if parsed is not None:
                return parsed
    return None


def _run_structured_phalanx_scan(
    client,
    email: str,
    plans: Sequence[Mapping[str, Any]],
    *,
    policy: dict | None,
    empire_state: Any,
    outcomes: list[dict[str, Any]] | None,
) -> list[str]:
    """Attempt at most one explicit scan and cache only validated positive evidence."""
    if not plans:
        return []
    plan = plans[0]
    command_id = plan.get("command_id")
    source_id = _moon_id(plan, "source")
    target = _coordinates(
        plan.get("target_coordinates", plan.get("target", plan.get("coordinates")))
    )
    moon_state = _moon_context(empire_state, source_id) if source_id is not None else None
    source = _coordinates(
        plan.get("source_coordinates", plan.get("source_moon", moon_state))
    )
    config = (
        policy.get("phalanx_scans")
        if isinstance(policy, Mapping)
        and isinstance(policy.get("phalanx_scans"), Mapping)
        else {}
    )
    reserve = _nonnegative_int(
        plan.get("deuterium_reserve", config.get("deuterium_reserve", 0))
    )
    cost = _positive_int(plan.get("scan_cost", config.get("scan_cost", 5_000)))
    available = _moon_deuterium(plan, moon_state)
    if not isinstance(command_id, str) or not command_id:
        result = {"ok": False, "status": "rejected", "reason": "command_id_required"}
    elif source_id is None or source is None or target is None:
        result = {
            "ok": False,
            "status": "rejected",
            "reason": "explicit_moon_or_coordinates_unavailable",
            "command_id": command_id,
        }
    elif reserve is None or cost is None or available is None:
        result = {
            "ok": False,
            "status": "rejected",
            "reason": "deuterium_reserve_state_unavailable",
            "command_id": command_id,
            "source_moon_id": source_id,
        }
    elif available - cost < reserve:
        result = {
            "ok": False,
            "status": "rejected",
            "reason": "deuterium_reserve",
            "command_id": command_id,
            "source_moon_id": source_id,
        }
    else:
        try:
            response = moons.phalanx_scan(
                client, source_id, target[0], target[1], target[2]
            )
        except Exception as exc:  # noqa: BLE001 - no blind fallback scan
            response = {
                "ok": False,
                "ambiguous": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if isinstance(response, Mapping) and response.get("ok") is True:
            try:
                ttl = _positive_int(plan.get("ttl_seconds", 120)) or 120
                cache = phalanx_intel.PhalanxObservationCache(email)
                observation = cache.record_scan_response(
                    source_moon=source,
                    target=target,
                    response=response,
                    scan_time=time.time(),
                    ttl_seconds=min(900, ttl),
                )
                result = {
                    "ok": True,
                    "status": "observed",
                    "reason": "phalanx_observation_recorded",
                    "command_id": command_id,
                    "source_moon_id": source_id,
                    "target_coordinates": target,
                    "observation_id": observation.observation_id,
                }
            except Exception as exc:  # scan happened, but unusable intel must fail closed
                result = {
                    "ok": False,
                    "status": "rejected",
                    "reason": "phalanx_cache_recording_failed",
                    "command_id": command_id,
                    "source_moon_id": source_id,
                    "target_coordinates": target,
                    "retry_safe": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        else:
            ambiguous = bool(
                isinstance(response, Mapping) and response.get("ambiguous") is True
            )
            result = {
                "ok": False,
                "status": "ambiguous" if ambiguous else "rejected",
                "reason": (
                    response.get("error")
                    if isinstance(response, Mapping)
                    else "invalid_phalanx_response"
                ),
                "command_id": command_id,
                "source_moon_id": source_id,
                "target_coordinates": target,
                "ambiguous": ambiguous,
                "retry_safe": not ambiguous,
            }
    _record_structured_outcome(email, outcomes, "phalanx_scan", result)
    return ["moon phalanx observed"] if result.get("status") == "observed" else []


def _ship_pairs(value: Any) -> tuple[tuple[int, int], ...]:
    if isinstance(value, Mapping):
        pairs = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        pairs = value
    else:
        raise ValueError("ships must be a mapping")
    normalized = tuple(sorted((int(ship_id), int(amount)) for ship_id, amount in pairs))
    if not normalized or any(ship_id <= 0 or amount <= 0 for ship_id, amount in normalized):
        raise ValueError("ships must contain positive amounts")
    return normalized


def _coerce_intercept_plan(value: Any) -> intercept_planner.ReturnInterceptPlan:
    if isinstance(value, intercept_planner.ReturnInterceptPlan):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("intercept plan must be a mapping")
    payload = value.get("plan", value)
    if isinstance(payload, intercept_planner.ReturnInterceptPlan):
        return payload
    if not isinstance(payload, Mapping):
        raise ValueError("intercept payload must be a mapping")
    stage_value = payload.get("jump_gate_staging")
    stage = None
    if isinstance(stage_value, Mapping):
        stage = intercept_planner.JumpGateStagingPlan(
            command_id=str(stage_value["command_id"]),
            source_moon=phalanx_intel.normalize_coordinate(stage_value["source_moon"]),
            destination_moon=phalanx_intel.normalize_coordinate(
                stage_value["destination_moon"]
            ),
            ships=_ship_pairs(stage_value["ships"]),
            execute_at=stage_value["execute_at"],
            latest_at=stage_value["latest_at"],
        )
    plan = intercept_planner.ReturnInterceptPlan(
        version=int(payload["version"]),
        command_id=str(payload["command_id"]),
        agent_id=str(payload["agent_id"]),
        created_at=payload["created_at"],
        observation_id=str(payload["observation_id"]),
        observed_server_time=payload["observed_server_time"],
        observation_expires_at=payload["observation_expires_at"],
        movement_id=str(payload["movement_id"]),
        movement_fingerprint=str(payload["movement_fingerprint"]),
        hostile_owner_id=str(payload["hostile_owner_id"]),
        source_coordinates=phalanx_intel.normalize_coordinate(
            payload["source_coordinates"]
        ),
        target_coordinates=phalanx_intel.normalize_coordinate(
            payload["target_coordinates"]
        ),
        target_type=int(payload["target_type"]),
        ships=_ship_pairs(payload["ships"]),
        speed=payload["speed"],
        own_flight_seconds=int(payload["own_flight_seconds"]),
        hostile_return_eta=payload["hostile_return_eta"],
        desired_impact_at=payload["desired_impact_at"],
        dispatch_at=payload["dispatch_at"],
        dispatch_margin_seconds=int(payload["dispatch_margin_seconds"]),
        send_window_seconds=int(payload["send_window_seconds"]),
        max_phalanx_age_seconds=int(payload["max_phalanx_age_seconds"]),
        jump_gate_staging=stage,
    )
    plan.to_dict()
    return plan


def _coerce_safety_callbacks(value: Any) -> intercept_planner.InterceptSafetyCallbacks | None:
    if isinstance(value, intercept_planner.InterceptSafetyCallbacks):
        return value
    if not isinstance(value, Mapping):
        return None
    names = (
        "espionage_full_intel",
        "admin_vacation_banned",
        "bashing",
        "combat_profitability",
        "fleet_slot",
        "ship_resource",
        "target_ownership",
        "phalanx_revalidation",
        "timing_revalidation",
    )
    if not all(callable(value.get(name)) for name in names):
        return None
    return intercept_planner.InterceptSafetyCallbacks(
        **{name: value[name] for name in names},
        jump_gate_ready=(
            value.get("jump_gate_ready")
            if callable(value.get("jump_gate_ready"))
            else None
        ),
    )


def _intercept_callbacks(
    client,
    email: str,
    levels: dict[int, int],
    entry: Mapping[str, Any],
    plan: intercept_planner.ReturnInterceptPlan,
    *,
    policy: dict | None,
    empire_state: Any,
) -> intercept_planner.InterceptSafetyCallbacks | Mapping[str, Any] | None:
    candidate = entry.get("safety_callbacks", entry.get("callbacks"))
    if candidate is None and isinstance(policy, Mapping):
        configured = policy.get("intercept_safety_callbacks")
        if isinstance(configured, Mapping) and plan.command_id in configured:
            candidate = configured[plan.command_id]
        else:
            candidate = configured
    callbacks = _coerce_safety_callbacks(candidate)
    if callbacks is not None:
        return callbacks

    try:
        normalized_plan = intercept_planner.normalize_intercept_timing(plan)
        context: dict[str, Any] = {"levels": dict(levels)}
        limits = (
            policy.get("limits")
            if isinstance(policy, Mapping) and isinstance(policy.get("limits"), Mapping)
            else {}
        )
        targeting = (
            policy.get("targeting")
            if isinstance(policy, Mapping)
            and isinstance(policy.get("targeting"), Mapping)
            else {}
        )
        option_sources = {
            "min_loot": (entry.get("min_loot"), limits.get("min_loot")),
            "max_active_missions": (
                entry.get("max_active_missions"),
                limits.get("max_active_missions"),
            ),
            "bashing_max_attacks": (
                entry.get("bashing_max_attacks"),
                limits.get("bashing_max_attacks"),
            ),
            "attack_cooldown_sec": (
                entry.get("attack_cooldown_sec"),
                limits.get("attack_target_cooldown_sec"),
            ),
            "fleet_risk_percent": (
                entry.get("fleet_risk_percent"),
                limits.get("fleet_risk_percent"),
            ),
            "min_fleet_value_limit": (
                entry.get("min_fleet_value_limit"),
                limits.get("min_fleet_value_limit"),
            ),
            "allow_defended_targets": (
                entry.get("allow_defended_targets"),
                targeting.get("allow_defended_targets"),
            ),
            "allow_strong_targets": (
                entry.get("allow_strong_targets"),
                targeting.get("allow_strong_targets"),
            ),
            "excluded_systems": (
                entry.get("excluded_systems"),
                targeting.get("excluded_systems"),
            ),
        }
        for name, values in option_sources.items():
            value = next((item for item in values if item is not None), None)
            if value is not None:
                context[name] = value

        if isinstance(candidate, Mapping):
            for name in (
                "phalanx_revalidation",
                "timing_revalidation",
                "jump_gate_ready",
                "immediate_target_revalidation",
            ):
                if callable(candidate.get(name)):
                    context[name] = candidate[name]

        state = raider._load_state(email)
        if isinstance(state, dict):
            request = raider._preflight_request(
                state, normalized_plan.target_coordinates
            )
            if isinstance(request, dict):
                context["preflight_request"] = request

        built = intercept_planner.build_intercept_safety_callbacks(
            client,
            email,
            normalized_plan,
            raider_module=raider,
            context=context,
        )
        callbacks = _coerce_safety_callbacks(built)
        if callbacks is None:
            raise ValueError("factory did not return complete safety callbacks")
        return callbacks
    except Exception as exc:  # noqa: BLE001 - explicit intercept fails closed
        return {
            "ok": False,
            "status": "rejected",
            "reason": {
                "code": "intercept_safety_factory_failed",
                "message": "mandatory intercept safety adapters are unavailable",
                "details": {"error": f"{type(exc).__name__}: {exc}"},
            },
            "command_id": getattr(plan, "command_id", None),
            "retry_safe": False,
        }


def _prepare_intercepts(
    email: str,
    plans: Sequence[Mapping[str, Any]],
    *,
    reservation_ledger: Any,
    outcomes: list[dict[str, Any]] | None,
) -> tuple[list[tuple[Mapping[str, Any], intercept_planner.ReturnInterceptPlan]], int]:
    prepared: list[tuple[Mapping[str, Any], intercept_planner.ReturnInterceptPlan]] = []
    reserved_slots = 0
    for entry in plans:
        try:
            plan = intercept_planner.normalize_intercept_timing(
                _coerce_intercept_plan(entry)
            )
        except Exception as exc:  # noqa: BLE001 - malformed explicit commands never dispatch
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "invalid_intercept_plan",
                "command_id": entry.get("command_id"),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _record_structured_outcome(email, outcomes, "intercept", result)
            continue
        reservation = _reserve_colonization_slot(
            reservation_ledger, plan.command_id
        )
        if not isinstance(reservation, Mapping) or reservation.get("accepted") is not True:
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "intercept_slot_reservation_unavailable",
                "command_id": plan.command_id,
                "retry_safe": False,
            }
            _record_structured_outcome(email, outcomes, "intercept", result)
            continue
        prepared.append((entry, plan))
        reserved_slots += 1
    return prepared, reserved_slots


def _jump_dependency(plan: Mapping[str, Any], intercept_ids: set[str]) -> str | None:
    value = plan.get(
        "dependent_intercept_command_id", plan.get("intercept_command_id")
    )
    if value is None:
        depends_on = plan.get("depends_on")
        value = (
            depends_on.get("command_id")
            if isinstance(depends_on, Mapping)
            else depends_on
        )
    if value is None and len(intercept_ids) == 1:
        value = next(iter(intercept_ids))
    return str(value) if isinstance(value, str) and value in intercept_ids else None


def _recent_command_status(email: str, command_id: str) -> tuple[str | None, bool]:
    load = getattr(action_feedback, "load_recent_action_outcomes", None)
    if not callable(load):
        return None, False
    try:
        recent = load(email, 100)
    except Exception:  # noqa: BLE001 - unknown durable state forbids a jump retry
        return None, False
    for item in recent:
        if isinstance(item, Mapping) and item.get("command_id") == command_id:
            return str(item.get("status") or "unknown"), True
    return None, True


def _run_structured_jump_gates(
    client,
    email: str,
    plans: Sequence[Mapping[str, Any]],
    *,
    intercept_ids: set[str],
    outcomes: list[dict[str, Any]] | None,
) -> tuple[dict[str, str], list[str]]:
    dependency_status: dict[str, str] = {}
    notes: list[str] = []
    now = time.time()
    for plan in plans:
        command_id = plan.get("command_id")
        dependency = _jump_dependency(plan, intercept_ids)
        source_id = _moon_id(plan, "source")
        destination_id = _moon_id(plan, "destination")
        ships = plan.get("ships")
        execute_at = plan.get("execute_at")
        latest_at = plan.get("latest_at")
        if not isinstance(command_id, str) or not command_id:
            result = {"ok": False, "status": "rejected", "reason": "command_id_required"}
        elif dependency is None:
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "dependent_intercept_unavailable",
                "command_id": command_id,
            }
        elif source_id is None or destination_id is None or not isinstance(ships, Mapping):
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "explicit_owned_moon_plan_required",
                "command_id": command_id,
            }
        elif execute_at is not None and now < float(execute_at):
            result = {
                "ok": False,
                "status": "waiting",
                "reason": "jump_gate_not_due",
                "command_id": command_id,
            }
        elif latest_at is not None and now > float(latest_at):
            result = {
                "ok": False,
                "status": "rejected",
                "reason": "jump_gate_window_missed",
                "command_id": command_id,
                "retry_safe": False,
            }
        else:
            previous, readable = _recent_command_status(email, command_id)
            if not readable:
                result = {
                    "ok": False,
                    "status": "rejected",
                    "reason": "jump_gate_feedback_state_unavailable",
                    "command_id": command_id,
                    "retry_safe": False,
                }
            elif previous in {"sending", "ambiguous", "sent", "confirmed", "rejected"}:
                acknowledged = previous in {"sent", "confirmed"}
                result = {
                    "ok": acknowledged,
                    "status": "sent" if acknowledged else str(previous),
                    "reason": "prior_jump_gate_outcome",
                    "command_id": command_id,
                    "retry_safe": False,
                }
            else:
                boundary = {
                    "command_id": command_id,
                    "status": "sending",
                    "reason": "durable_jump_gate_send_boundary",
                }
                if not _persist_action_outcome(email, "jump_gate", boundary):
                    result = {
                        "ok": False,
                        "status": "rejected",
                        "reason": "jump_gate_send_boundary_unavailable",
                        "command_id": command_id,
                        "retry_safe": False,
                    }
                else:
                    try:
                        response = moons.execute_jump_gate_transfer(
                            client, source_id, destination_id, ships
                        )
                    except Exception as exc:  # outcome is uncertain after durable boundary
                        response = {
                            "ok": False,
                            "ambiguous": True,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ambiguous = bool(
                        isinstance(response, Mapping)
                        and response.get("ambiguous") is True
                    )
                    sent = bool(
                        isinstance(response, Mapping) and response.get("ok") is True
                    )
                    result = {
                        "ok": sent,
                        "status": "sent" if sent else ("ambiguous" if ambiguous else "rejected"),
                        "reason": (
                            "jump_gate_transferred"
                            if sent
                            else response.get("error", "jump_gate_rejected")
                        ),
                        "command_id": command_id,
                        "source_moon_id": source_id,
                        "destination_moon_id": destination_id,
                        "ambiguous": ambiguous,
                        "retry_safe": False,
                    }
        if dependency is not None:
            dependency_status[dependency] = str(result.get("status") or "rejected")
        _record_structured_outcome(email, outcomes, "jump_gate", result)
        if result.get("status") == "sent":
            notes.append("moon jump gate sent")
    return dependency_status, notes


def _run_due_intercepts(
    client,
    email: str,
    levels: dict[int, int],
    prepared: Sequence[tuple[Mapping[str, Any], intercept_planner.ReturnInterceptPlan]],
    reserved_slots: int,
    *,
    policy: dict | None,
    empire_state: Any,
    jump_dependencies: Mapping[str, str],
    outcomes: list[dict[str, Any]] | None,
) -> tuple[dict[int, int], int, list[str]]:
    available = dict(levels)
    remaining = reserved_slots
    notes: list[str] = []
    now = time.time()
    for entry, plan in prepared:
        dependency_state = jump_dependencies.get(plan.command_id)
        if dependency_state is not None and dependency_state != "sent":
            status = "waiting" if dependency_state == "waiting" else "rejected"
            result = {
                "ok": False,
                "status": status,
                "reason": "dependent_jump_gate_not_acknowledged",
                "command_id": plan.command_id,
                "retry_safe": status == "waiting",
            }
        else:
            stage_due = bool(
                plan.jump_gate_staging is not None
                and now >= float(plan.jump_gate_staging.execute_at)
            )
            attack_due = now >= float(plan.dispatch_at)
            if not stage_due and not attack_due:
                result = intercept_planner.record_intercept_plan(plan)
            else:
                callback_result = _intercept_callbacks(
                    client,
                    email,
                    available,
                    entry,
                    plan,
                    policy=policy,
                    empire_state=empire_state,
                )
                if isinstance(callback_result, Mapping):
                    result = dict(callback_result)
                elif callback_result is None:
                    result = {
                        "status": "rejected",
                        "reason": "mandatory_raider_safety_adapters_unavailable",
                        "command_id": plan.command_id,
                        "retry_safe": False,
                    }
                elif plan.jump_gate_staging is not None and dependency_state == "sent":
                    result = {
                        "status": "rejected",
                        "reason": "duplicate_jump_gate_staging_contract",
                        "command_id": plan.command_id,
                        "retry_safe": False,
                    }
                else:
                    result = intercept_planner.execute_due_intercept(
                        client, plan, callback_result
                    )
        normalized = {"ok": result.get("status") == "sent", **dict(result)}
        _record_structured_outcome(email, outcomes, "intercept", normalized)
        if result.get("status") == "sent":
            available = _levels_without_reservation(
                available, {ship_id: amount for ship_id, amount in plan.ships}
            )
            notes.append("moon intercept sent")
        elif result.get("status") == "rejected":
            remaining = max(0, remaining - 1)
    return available, remaining, notes


def _empire_planet_ids(empire_state: Any) -> tuple[int | None, int | None]:
    if not isinstance(empire_state, Mapping):
        return None, None
    root = empire_state.get("normalized_state", empire_state)
    if not isinstance(root, Mapping):
        return None, None
    home = root.get("canonical_home_id")
    planets = root.get("planets")
    current = None
    if isinstance(planets, Sequence) and not isinstance(planets, (str, bytes)):
        row = next(
            (
                planet for planet in planets
                if isinstance(planet, Mapping)
                and (planet.get("is_current") is True or planet.get("current") is True)
            ),
            None,
        )
        if isinstance(row, Mapping):
            current = row.get("planet_id", row.get("id"))
    try:
        current_id = int(current) if current is not None else None
    except (TypeError, ValueError, OverflowError):
        current_id = None
    try:
        home_id = int(home) if home is not None else None
    except (TypeError, ValueError, OverflowError):
        home_id = None
    return current_id, home_id


def _run_structured_development(
    client,
    email: str,
    personality_key: str,
    tick_index: int,
    plan: Mapping[str, Any],
    *,
    policy: dict | None,
    empire_state: Any,
    outcomes: list[dict[str, Any]] | None,
) -> str | None:
    state_start, state_home = _empire_planet_ids(empire_state)
    start = plan.get("start_planet_id", plan.get("start_planet", state_start))
    home = plan.get("home_planet_id", plan.get("home_planet", state_home))
    floors = plan.get("minimum_floors")
    command_id = plan.get("command_id")
    previous = None
    if isinstance(command_id, str) and command_id:
        previous, _readable = _recent_command_status(email, command_id)
    if previous in {
        "accepted",
        "queued",
        "completed",
        "executed",
        "dispatched",
        "sent",
        "confirmed",
    }:
        result: Mapping[str, Any] = {
            "ok": True,
            "status": "skipped",
            "reason": "prior_development_command_outcome",
            "planet_id": plan.get("planet_id"),
            "coordinate": plan.get("coordinates"),
            "start_planet_id": start,
            "home_planet_id": home,
            "actions": [],
        }
    else:
        try:
            result = colony_dev.colony_dev_outcome(
                client,
                email,
                personality_key,
                tick_index,
                policy=policy,
                start_planet=start,
                home_planet=home,
                minimum_floors=floors if isinstance(floors, Mapping) else None,
                role=str(plan.get("role") or "balanced"),
            )
        except Exception as exc:  # defensive for custom/replaced executors
            result = {
                "ok": False,
                "status": "error",
                "reason": "colony_development_error",
                "error": f"{type(exc).__name__}: {exc}",
                "planet_id": plan.get("planet_id"),
                "actions": [],
            }
    receipt = _record_development_outcome(
        email,
        outcomes,
        result,
        tick_index=tick_index,
        command_id=command_id if isinstance(command_id, str) else None,
        role=str(plan.get("role") or "balanced"),
        policy_activation_id=(
            str(policy["activation_id"])
            if isinstance(policy, Mapping)
            and isinstance(policy.get("activation_id"), str)
            else None
        ),
        # A duplicate-suppression observation is not a lifecycle transition.
        # Keeping the prior queued/completed receipt latest lets the reconciler
        # settle it on a later fresh snapshot.
        persist=previous not in {
            "accepted",
            "queued",
            "completed",
            "executed",
            "dispatched",
            "sent",
            "confirmed",
        },
    )
    note = result.get("note")
    return (
        note
        if receipt["status"] == "queued" and isinstance(note, str) and note
        else None
    )


def _expedition_can_preserve_slot(
    email: str, levels: dict[int, int], reserved_slots: int,
) -> bool:
    if reserved_slots <= 0:
        return True
    try:
        used = int(fleetsave.active_slots_in_use(email))
        maximum = int(explorer.fleet_slots(levels, email))
    except Exception:  # noqa: BLE001 - pending structured action fails closed
        return False
    return used >= 0 and used + 1 + reserved_slots <= maximum


def ensure_ai_character_class(client, email: str, personality_key: str) -> str | None:
    """미배정 AI의 무료 클래스를, 활성 함대가 없는 틱에 한 번만 선택한다.

    서버는 함대 비행 중 클래스 변경을 거부한다. raid/원정/플릿세이브보다 먼저 호출해
    귀환 직후의 짧은 빈 구간을 사용하고, DB 장애·불명 성격은 아무 행동 없이 닫는다.
    규칙봇은 이미 일괄 배정됐으므로 추가 조회 부하를 피하려고 등록 AI 계정에만 적용한다.
    """
    normalized = email.lower()
    if (
        not normalized.startswith("ai")
        and normalized not in _NAMED_AI_EMAILS
    ) or normalized in _CLASS_CONFIRMED:
        return None
    class_id = PERSONALITY_CLASS_ID.get(personality_key)
    if class_id is None:
        return None
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.character_class, "
                "(SELECT COUNT(*) FROM fleet_missions fm "
                " WHERE fm.user_id=u.id AND fm.processed=0 AND fm.mission_type<>10) active "
                "FROM users u WHERE u.email=%s",
                (email,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - 상태 불명이면 클래스 선택 금지
        return None
    if not row:
        return None
    if row.get("character_class") is not None:
        _CLASS_CONFIRMED.add(normalized)
        return None
    if int(row.get("active") or 0) > 0:
        return None
    try:
        result = client.select_class(class_id)
    except Exception:  # noqa: BLE001 - 클래스 실패가 성장 틱을 막지 않음
        return None
    if isinstance(result, dict) and (
        result.get("status") == "success" or result.get("success") is True
    ):
        _CLASS_CONFIRMED.add(normalized)
        return f"class {CLASS_NAME[class_id]}"
    return None


def run_post_actions(
    client,
    email: str,
    levels: dict[int, int],
    personality_key: str,
    tick_index: int,
    *,
    energy: int | None,
    raid_options: dict | None = None,
    defense_ratio: float | None = None,
    policy: dict | None = None,
    plans: Any = None,
    empire_state: Mapping[str, Any] | None = None,
    reservation_ledger: Any = None,
    outcomes: list[dict[str, Any]] | None = None,
) -> list[str]:
    """한 틱의 공용 후행 행동을 실행하고 비어 있지 않은 로그 조각을 순서대로 반환."""
    notes: list[str] = []
    pkey = personality_key

    class_note = ensure_ai_character_class(client, email, pkey)
    if class_note:
        notes.append(class_note)

    def append_deathstar_goal() -> None:
        goals = policy.get("secondary_goals") if isinstance(policy, dict) else None
        goal = goals.get("deathstar") if isinstance(goals, dict) else None
        if (
            deathstar_goal.is_managed_account(email)
            and isinstance(goal, dict)
            and goal.get("mode") == "pursue"
        ):
            notes.append(deathstar_goal.execute(client, email, goal))

    configured_structured_plans = _policy_structured_plans(policy, plans)
    structured = _structured_enabled(policy, plans)
    plan_groups = _group_structured_plans(configured_structured_plans)
    prepared_intercepts, intercept_reservation = _prepare_intercepts(
        email,
        plan_groups["intercepts"],
        reservation_ledger=reservation_ledger,
        outcomes=outcomes,
    ) if structured else ([], 0)
    if structured:
        notes.extend(_run_structured_phalanx_scan(
            client,
            email,
            plan_groups["phalanx_scans"],
            policy=policy,
            empire_state=empire_state,
            outcomes=outcomes,
        ))
        jump_dependencies, jump_notes = _run_structured_jump_gates(
            client,
            email,
            plan_groups["jump_gate"],
            intercept_ids={plan.command_id for _, plan in prepared_intercepts},
            outcomes=outcomes,
        )
        notes.extend(jump_notes)
        available_levels, intercept_reservation, intercept_notes = _run_due_intercepts(
            client,
            email,
            levels,
            prepared_intercepts,
            intercept_reservation,
            policy=policy,
            empire_state=empire_state,
            jump_dependencies=jump_dependencies,
            outcomes=outcomes,
        )
        notes.extend(intercept_notes)
    else:
        available_levels = dict(levels)
    colonization_reservation = _run_structured_colonization(
        client,
        email,
        available_levels,
        plan_groups["colonize"][0] if plan_groups["colonize"] else None,
        policy=policy,
        reservation_ledger=reservation_ledger,
        outcomes=outcomes,
    ) if structured else 0
    available_levels = _run_structured_transfers(
        client,
        email,
        available_levels,
        plan_groups["transfer"],
        policy=policy,
        reservation_ledger=reservation_ledger,
        colonization_slot_reservation=(
            colonization_reservation + intercept_reservation
        ),
        outcomes=outcomes,
    ) if structured else available_levels
    fleet_slot_reservation = colonization_reservation + intercept_reservation

    configured_plan = effective_action_plan(policy)
    if configured_plan is not None:
        notes.extend(_run_policy_actions(
            client,
            email,
            available_levels,
            pkey,
            tick_index,
            energy=energy,
            raid_options=raid_options or {},
            defense_ratio=defense_ratio,
            policy=policy or {},
            action_plan=configured_plan,
            colonization_slot_reservation=fleet_slot_reservation,
            skip_legacy_colonization=bool(plan_groups["colonize"]),
        ))
        append_deathstar_goal()
        if structured and plan_groups["development"]:
            note = _run_structured_development(
                client,
                email,
                pkey,
                tick_index,
                plan_groups["development"][0],
                policy=policy,
                empire_state=empire_state,
                outcomes=outcomes,
            )
            if note:
                notes.append(note)
        elif "develop_colonies" in configured_plan:
            # An empty structured development section means "no explicit
            # per-planet command", not "disable colony upkeep".  Keep the
            # proven round-robin fallback unless a real structured development
            # command took authority above.  This matters for AI policies whose
            # schema always contains an empty ``plans.development`` container.
            for visit_index in range(_colony_development_visits(email)):
                cnote = _run_legacy_development_visit(
                    client,
                    email,
                    pkey,
                    tick_index,
                    visit_index,
                    policy=policy,
                    outcomes=outcomes,
                )
                if cnote:
                    notes.append(cnote)
        return notes

    # 1) 원정 슬롯을 먼저 채운다. 첫 원정이 화물을 독식하지 않도록 explorer가 남은
    #    슬롯에 균등 분배하고, 방금 출발한 함선은 아래 raid 가용량에서 즉시 제외한다.
    opts = raid_options if raid_options is not None else raid_options_for_account(email)
    doctrine = strategic_growth.individual_doctrine(email, pkey)
    risk = RULE_EXPEDITION_RISK.get(pkey, 0.25)
    if doctrine == "expeditioneer":
        risk = min(1.0, risk * 1.75)
    aggressive_expedition = pkey == "explorer_aggressive"
    dispatch_state: dict = {}
    enote = None
    legacy_raid_floor = (
        MIN_RAID_RESERVED_SLOTS if _managed_ai_account(email) else 0
    )
    if _expedition_can_preserve_slot(
        email,
        available_levels,
        fleet_slot_reservation + legacy_raid_floor,
    ):
        expedition_levels = dict(available_levels)
        if pkey == "raider":
            for cargo_id in (202, 203):
                expedition_levels[cargo_id] = int(
                    max(0, expedition_levels.get(cargo_id, 0)) * 0.60
                )
        enote = explorer.expedition_tick(
            client,
            email,
            expedition_levels,
            aggressive=aggressive_expedition,
            use_server_cap=True,
            risk_fraction=risk,
            total_exposure_fraction=risk,
            dispatch_state=dispatch_state,
        )
    if enote:
        notes.append(enote)
    action_levels = _levels_after_dispatch(available_levels, dispatch_state)
    reserved_slots, reserved_ships = explorer.expedition_raid_reservation(
        email,
        action_levels,
        aggressive=aggressive_expedition,
        use_server_cap=True,
        risk_fraction=risk,
        total_exposure_fraction=risk,
    )
    reserved_slots, reserved_ships = _leave_raid_capacity(
        reserved_slots,
        reserved_ships,
        enabled=bool(legacy_raid_floor),
    )

    # 2) 원정 예약 함선·슬롯을 제외한 나머지로만 full-intel 순EV 약탈/정찰/수확을 한다.
    #    원정을 꾸릴 수 없으면 예약값이 0이라 유휴 슬롯은 그대로 raid 엔진에 열린다.
    default_min_loot = RULE_MIN_LOOT.get(pkey, 8_000)
    if doctrine == "debris_hunter":
        default_min_loot = 0
    elif doctrine == "cargo_raider":
        default_min_loot = max(0, default_min_loot // 2)
    requested_raid_cap = opts.get("max_active_missions")
    note = raider.raid_tick(
        client,
        email,
        _levels_without_reservation(action_levels, reserved_ships),
        min_loot=opts.get("min_loot", default_min_loot),
        max_active_missions=_raid_slot_cap(
            email,
            action_levels,
            requested_raid_cap,
            reserved_slots + fleet_slot_reservation,
        ),
            controls=RULE_RAID_CONTROLS,
    )
    if note:
        notes.append(note)

    # 3) 성격과 무관하게 천체물리/식민선/서버 슬롯이 허용하는 만큼 확장한다.
    if not structured:
        cnote = colony.colony_tick(
            client,
            email,
            action_levels,
            max_planets=99,
            reserve_fleet_slots=0,
            enforce_strategy_cap=False,
        )
        if cnote:
            notes.append(cnote)

    # 4) 모든 계정이 약탈가치에 비례한 방어를 갖는다. 터틀만 더 높은 잉여 투자 비율과
    #    반사 재건을 사용하고, 나머지는 공용 동적 방어선을 사용한다.
    if pkey == "turtle":
        kwargs = {"energy": energy}
        if defense_ratio is not None:
            kwargs["defense_ratio"] = defense_ratio
        dnote = turtle.defense_tick(client, email, levels, **kwargs)
    else:
        dnote = miner_defense.base_defense_tick(
            client, email, levels, pkey, energy=energy
        ) if miner_defense.MINER_BASE_DEFENSE_ENABLED else None
    if dnote:
        notes.append(dnote)

    # 정체성 미션이 먼저 함선/슬롯을 가져간 뒤 남은 파킹 함대만 세이브한다.
    fnote = fleetsave.fleetsave_tick(
        client, email, action_levels, pkey, FLEETSAVE_MIN_VALUE.get(pkey, 40000),
        mode="adaptive",
    )
    if fnote:
        notes.append(fnote)

    append_deathstar_goal()
    # planet_current를 바꾸는 개발 루틴은 반드시 마지막. 구조화 API와 레거시 API 모두
    # 내부 finally에서 실제 시작 행성으로 복귀한다.
    if structured:
        if plan_groups["development"]:
            cnote = _run_structured_development(
                client,
                email,
                pkey,
                tick_index,
                plan_groups["development"][0],
                policy=policy,
                empire_state=empire_state,
                outcomes=outcomes,
            )
            if cnote:
                notes.append(cnote)
    else:
        for visit_index in range(_colony_development_visits(email)):
            cnote = _run_legacy_development_visit(
                client,
                email,
                pkey,
                tick_index,
                visit_index,
                policy=policy,
                outcomes=outcomes,
            )
            if cnote:
                notes.append(cnote)

    # 금속 overflow는 아직 집행하지 않는다. 모든 행동·행성 전환이 끝난 뒤 저빈도
    # read-only 스냅샷으로 PLAN/HOLD만 관측한다(HTTP 0, 계정별 12틱에 1회 분산).
    snote = metal_sink_observer.observe_metal_sink_shadow(
        email, levels, pkey, tick_index
    )
    if snote:
        notes.append(snote)
    return notes


def _run_policy_actions(
    client,
    email: str,
    levels: dict[int, int],
    personality_key: str,
    tick_index: int,
    *,
    energy: int | None,
    raid_options: dict,
    defense_ratio: float | None,
    policy: dict,
    action_plan: list[str],
    colonization_slot_reservation: int = 0,
    skip_legacy_colonization: bool = False,
) -> list[str]:
    """LLM action_plan을 성격과 무관하게 순서대로 집행한다.

    raid/spy/harvest는 같은 정찰·타겟 캐시를 쓰는 단일 엔진이므로 세 이름 중 가장 먼저
    나타난 위치에서 한 번만 호출하고, 각각의 허용 여부를 인자로 전달한다.
    """
    notes: list[str] = []
    limits = policy.get("limits") if isinstance(policy.get("limits"), dict) else {}
    targeting = (
        policy.get("targeting") if isinstance(policy.get("targeting"), dict) else {}
    )
    raid_called = False
    expedition_called = False
    defense_called = False
    colony_called = False
    action_levels = dict(levels)

    expedition_enabled = "expedition" in action_plan
    expedition_aggressive = policy.get("autonomy") == "full"
    expedition_use_server_cap = bool(
        limits.get("use_server_expedition_slots", False)
    )
    expedition_risk = float(limits.get("expedition_risk_percent", 35)) / 100.0
    expedition_total = (
        float(limits.get("expedition_total_exposure_percent", 70)) / 100.0
    )
    raid_actions_enabled = bool({"spy", "raid", "harvest"}.intersection(action_plan))
    raid_slot_floor = MIN_RAID_RESERVED_SLOTS if raid_actions_enabled else 0

    def _raid_reservation() -> tuple[int, dict[int, int]]:
        if not expedition_enabled:
            return 0, {}
        reserved_slots, reserved_ships = explorer.expedition_raid_reservation(
            email,
            action_levels,
            aggressive=expedition_aggressive,
            use_server_cap=expedition_use_server_cap,
            risk_fraction=expedition_risk,
            total_exposure_fraction=expedition_total,
        )
        return _leave_raid_capacity(
            reserved_slots,
            reserved_ships,
            enabled=raid_actions_enabled and expedition_called,
        )

    for action in action_plan:
        if action in {"spy", "raid", "harvest"}:
            if raid_called:
                continue
            raid_called = True
            opts = dict(raid_options)
            # full autonomy에서 별도 상한이 없으면 실제 연구 기반 함대 슬롯까지 사용한다.
            if policy.get("autonomy") == "full" and opts.get("max_active_missions") is None:
                opts["max_active_missions"] = explorer.fleet_slots(
                    action_levels, email
                )
            reserved_slots, reserved_ships = _raid_reservation()
            raid_kwargs = {
                "min_loot": opts.get("min_loot"),
                "max_active_missions": _raid_slot_cap(
                    email,
                    action_levels,
                    opts.get("max_active_missions"),
                    reserved_slots + colonization_slot_reservation,
                ),
                "allow_spy": "spy" in action_plan,
                "allow_attack": "raid" in action_plan,
                "allow_harvest": "harvest" in action_plan,
                "controls": {"limits": limits, "targeting": targeting},
            }
            note = raider.raid_tick(
                client,
                email,
                _levels_without_reservation(action_levels, reserved_ships),
                **raid_kwargs,
            )
            if note:
                notes.append(note)
        elif action == "expedition":
            expedition_called = True
            if not _expedition_can_preserve_slot(
                email,
                action_levels,
                colonization_slot_reservation
                + (raid_slot_floor if not raid_called else 0),
            ):
                continue
            dispatch_state: dict = {}
            expedition_levels = dict(action_levels)
            if personality_key == "raider":
                for cargo_id in (202, 203):
                    expedition_levels[cargo_id] = int(
                        max(0, expedition_levels.get(cargo_id, 0)) * 0.60
                    )
            note = explorer.expedition_tick(
                client,
                email,
                expedition_levels,
                aggressive=expedition_aggressive,
                use_server_cap=expedition_use_server_cap,
                risk_fraction=expedition_risk,
                total_exposure_fraction=expedition_total,
                dispatch_state=dispatch_state,
            )
            action_levels = _levels_after_dispatch(action_levels, dispatch_state)
            if note:
                notes.append(note)
        elif (
            action == "colonize"
            and not colony_called
            and not skip_legacy_colonization
        ):
            colony_called = True
            note = colony.colony_tick(
                client,
                email,
                action_levels,
                max_planets=int(limits.get("max_planets", colony.MAX_PLANETS)),
                reserve_fleet_slots=int(limits.get("reserve_fleet_slots", 0)),
                enforce_strategy_cap=policy.get("autonomy") != "full",
                scan_span=int(limits.get("colony_scan_span", colony.SCAN_SPAN)),
                cooldown_sec=int(limits.get(
                    "colonize_cooldown_sec", colony.COLONIZE_COOLDOWN_SEC
                )),
            )
            if note:
                notes.append(note)
        elif action == "defense" and not defense_called:
            defense_called = True
            kwargs = {
                "energy": energy,
                "strategy": policy.get("defense_strategy"),
            }
            if defense_ratio is not None:
                kwargs["defense_ratio"] = defense_ratio
            note = turtle.defense_tick(client, email, levels, **kwargs)
            if note:
                notes.append(note)
        elif action == "fleetsave":
            fs_config = (
                policy.get("fleetsave")
                if isinstance(policy.get("fleetsave"), dict)
                else {}
            )
            full_autonomy = policy.get("autonomy") == "full"
            max_active_fleetsaves = limits.get("max_active_fleetsaves")
            if max_active_fleetsaves is None:
                # full LLM에는 함대 슬롯 외의 인공 동시 세이브 상한을 기본 적용하지 않는다.
                # guided 정책은 기존 1개 기본값을 유지한다.
                max_active_fleetsaves = (
                    0 if full_autonomy else fleetsave.MAX_ACTIVE_FLEETSAVES
                )
            note = fleetsave.fleetsave_tick(
                client,
                email,
                action_levels,
                personality_key,
                int(fs_config.get(
                    "min_value", FLEETSAVE_MIN_VALUE.get(personality_key, 40000)
                )),
                mode=str(fs_config.get("mode", "adaptive")),
                max_active_fleetsaves=int(max_active_fleetsaves),
                fleet_slot_cap=(
                    explorer.fleet_slots(action_levels, email)
                    if full_autonomy
                    else None
                ),
            )
            if note:
                notes.append(note)
        elif action == "develop_colonies":
            # Deferred by run_post_actions so every planet-switching development
            # path is unconditionally the final post-action.
            continue

    snote = metal_sink_observer.observe_metal_sink_shadow(
        email, levels, personality_key, tick_index
    )
    if snote:
        notes.append(snote)
    return notes
