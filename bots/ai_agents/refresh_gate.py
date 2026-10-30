#!/usr/bin/env python3
"""Auditable, fail-open gate for expensive policy-model refreshes."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
from typing import Any, Mapping


SCHEMA_VERSION = 1
AUDIT_MAX = 96
DEFAULT_MAX_MODEL_SILENCE_HOURS = 6.0
REJECTION_STATUSES = {"rejected", "failed", "ambiguous", "unknown", "timeout", "stalled"}
_DROP_KEYS = {
    "observed_at", "generated_at", "recorded_at", "settled_at", "updated_at",
    "refreshed_at", "time_start", "time_end", "remaining_seconds",
    "expires_at", "fresh_until", "last_seen_at", "is_current",
    "object_amount_progress", "remaining_amount",
}
_QUANTITY_PARENTS = {
    "resources", "capacities", "production_per_hour", "production_per_hour_account",
    "ships", "ship_totals", "defense",
    "my_general_score", "rank10_cutoff_score", "rank1_score", "gap_to_top10",
    "cargo_shortfall", "resources_total", "potential_loot", "defense_value",
}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_dict(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _quantity_bucket(value: int | float) -> Any:
    number = float(value)
    if not math.isfinite(number):
        return "non-finite"
    if number == 0:
        return 0
    sign = -1 if number < 0 else 1
    absolute = abs(number)
    if absolute < 1_000:
        return sign * int(round(absolute / 25.0))
    # Roughly 5% bands: routine production drift stays stable, while a build,
    # transfer, loss, or meaningful stock movement changes the context.
    return [sign, int(math.floor(math.log(absolute, 1.05)))]


def _canonicalize(value: Any, *, parent: str = "") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if key in _DROP_KEYS:
                continue
            child_parent = parent if parent in _QUANTITY_PARENTS else key
            result[key] = _canonicalize(item, parent=child_parent)
        return result
    if isinstance(value, list):
        return [_canonicalize(item, parent=parent) for item in value[:200]]
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and parent in _QUANTITY_PARENTS
    ):
        return _quantity_bucket(value)
    return value


def _event_token(item: Any) -> Any:
    if not isinstance(item, Mapping):
        return None
    return {
        key: item.get(key)
        for key in (
            "command_id", "sequence", "message_id", "id", "contract_id", "kind",
            "status", "sender_id", "recipient_id", "expires_at",
        )
        if item.get(key) is not None
    }


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def rejection_fingerprint(context: Mapping[str, Any]) -> str:
    rows = context.get("recent_action_rejections")
    if not isinstance(rows, list):
        rows = []
    tokens = [_event_token(row) for row in rows if isinstance(row, Mapping)]
    return _fingerprint(tokens)


def diplomacy_fingerprint(context: Mapping[str, Any]) -> str:
    diplomacy = context.get("diplomacy")
    if not isinstance(diplomacy, Mapping):
        return _fingerprint({"invalid": True})
    return _fingerprint({
        "messages": [
            _event_token(item)
            for item in diplomacy.get("recent_messages", [])
            if isinstance(item, Mapping)
        ],
        "contracts": [
            _event_token(item)
            for item in diplomacy.get("contracts", [])
            if isinstance(item, Mapping)
        ],
    })


def material_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded policy-relevant state used by the stable-context gate."""
    empire = context.get("empire")
    if not isinstance(empire, Mapping):
        empire = {}
    diplomacy = context.get("diplomacy")
    if not isinstance(diplomacy, Mapping):
        diplomacy = {}
    return _canonicalize({
        "normalized_state": context.get("normalized_state"),
        "resources": context.get("resources"),
        "recent_spy": context.get("recent_spy"),
        "highscore": context.get("highscore"),
        "raid_opportunity": empire.get("raid_opportunity"),
        "secondary_goals": empire.get("secondary_goals"),
        "diplomacy_contracts": diplomacy.get("contracts", []),
        "diplomacy_messages": [
            _event_token(item)
            for item in diplomacy.get("recent_messages", [])
            if isinstance(item, Mapping)
        ],
        "recent_action_rejections": [
            _event_token(item)
            for item in context.get("recent_action_rejections", [])
            if isinstance(item, Mapping)
        ],
    })


def context_fingerprint(context: Mapping[str, Any]) -> str:
    return _fingerprint(material_context(context))


def effective_policy_fingerprint(policy: Mapping[str, Any]) -> str:
    # Share the activation ledger's exact definition of executable controls.
    from bots.ai_agents.experience_scorecard import _policy_fingerprint, policy_view

    return _policy_fingerprint(policy_view(policy))


_KNOWN_UNAVAILABLE_MOON_ISSUES = frozenset({
    "resources-unavailable",
    "fields-unavailable",
    "facility-levels-unavailable",
    "building-queue:moon-queue-unavailable",
    "unit-queue:moon-queue-unavailable",
})
_MOON_PLAN_SECTIONS = (
    "moon_development",
    "phalanx_scans",
    "jump_gate",
    "intercepts",
)


def _known_non_actionable_moon_partial(
    normalized: Mapping[str, Any],
    policy: Mapping[str, Any] | None,
) -> bool:
    """Tolerate a known moon telemetry gap only when no moon command is active."""
    if (
        normalized.get("status") != "partial"
        or normalized.get("reason") != "one-or-more-moons-partial"
    ):
        return False
    if isinstance(policy, Mapping):
        plans = policy.get("plans")
        if isinstance(plans, Mapping) and any(
            isinstance(plans.get(section), list) and plans.get(section)
            for section in _MOON_PLAN_SECTIONS
        ):
            return False
    moons = normalized.get("moons")
    if not isinstance(moons, list) or not moons:
        return False
    for moon in moons:
        if not isinstance(moon, Mapping) or moon.get("status") != "partial":
            return False
        issues = moon.get("issues")
        if (
            not isinstance(issues, list)
            or not issues
            or not set(map(str, issues)).issubset(_KNOWN_UNAVAILABLE_MOON_ISSUES)
        ):
            return False
    return True


def _status_anomalies(
    context: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
) -> list[str]:
    anomalies: list[str] = []
    normalized = context.get("normalized_state")
    if not isinstance(normalized, Mapping):
        return ["normalized-state-missing"]
    if (
        normalized.get("status") != "ready"
        and not _known_non_actionable_moon_partial(normalized, policy)
    ):
        anomalies.append(f"normalized-state-{normalized.get('status', 'invalid')}")
    planets = normalized.get("planets")
    if not isinstance(planets, list) or not planets:
        anomalies.append("normalized-planets-missing")
        planets = []
    for key in (
        "action_feedback_status",
        "action_reconciliation",
        "game_outcome_sync",
        "diplomacy",
    ):
        value = context.get(key)
        if not isinstance(value, Mapping) or value.get("status") not in {"ready", "ok"}:
            status = value.get("status", "missing") if isinstance(value, Mapping) else "missing"
            anomalies.append(f"{key}-{status}")
    if not isinstance(context.get("recent_action_outcomes"), list):
        anomalies.append("recent-action-outcomes-missing")
    if not isinstance(context.get("recent_action_rejections"), list):
        anomalies.append("recent-action-rejections-missing")
    highscore = context.get("highscore")
    if isinstance(highscore, Mapping) and highscore.get("error"):
        anomalies.append("highscore-error")
    for planet in planets:
        if not isinstance(planet, Mapping):
            anomalies.append("planet-entry-invalid")
            continue
        planet_id = planet.get("planet_id", "unknown")
        if planet.get("status") != "ready" or planet.get("core_status") != "ready":
            anomalies.append(f"planet-{planet_id}-not-ready")
        if planet.get("issues"):
            anomalies.append(f"planet-{planet_id}-issues")
        for section in ("production_status",):
            status = planet.get(section)
            if isinstance(status, Mapping) and status.get("status") != "ready":
                anomalies.append(f"planet-{planet_id}-{section}-not-ready")
    return sorted(set(anomalies))


def _critical_colonies(context: Mapping[str, Any]) -> list[str]:
    normalized = context.get("normalized_state")
    planets = normalized.get("planets", []) if isinstance(normalized, Mapping) else []
    critical: list[str] = []
    for planet in planets if isinstance(planets, list) else []:
        if not isinstance(planet, Mapping):
            continue
        planet_id = planet.get("planet_id", "unknown")
        energy = planet.get("energy")
        if isinstance(energy, Mapping):
            balance = energy.get("balance")
            factor = energy.get("production_factor_pct")
            if isinstance(balance, (int, float)) and balance < 0:
                critical.append(f"planet-{planet_id}-negative-energy")
            if isinstance(factor, (int, float)) and factor < 100:
                critical.append(f"planet-{planet_id}-production-under-100")
        levels = planet.get("levels")
        buildings = levels.get("buildings", {}) if isinstance(levels, Mapping) else {}
        if isinstance(buildings, Mapping) and all(
            int(buildings.get(name, 0) or 0) == 0
            for name in ("metal_mine", "crystal_mine", "deuterium_synthesizer")
        ):
            critical.append(f"planet-{planet_id}-unbootstrapped")
    return sorted(set(critical))


def _policy_freshness(
    policy: Mapping[str, Any],
    now: dt.datetime,
) -> tuple[bool, str]:
    updated = _parse_time(policy.get("updated_at"))
    ttl = policy.get("ttl_hours")
    if updated is None:
        return False, "policy-updated-at-invalid"
    if (
        not isinstance(ttl, (int, float))
        or isinstance(ttl, bool)
        or not math.isfinite(float(ttl))
        or ttl <= 0
    ):
        return False, "policy-ttl-invalid"
    if updated > now + dt.timedelta(minutes=5):
        return False, "policy-updated-at-in-future"
    if now >= updated + dt.timedelta(hours=float(ttl)):
        return False, "policy-stale"
    if not isinstance(policy.get("activation_id"), str) or not policy.get("activation_id"):
        return False, "policy-activation-id-missing"
    return True, "fresh"


def evaluate_refresh_gate(
    policy: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    *,
    now: dt.datetime | None = None,
    max_model_silence_hours: float = DEFAULT_MAX_MODEL_SILENCE_HOURS,
) -> dict[str, Any]:
    """Pure decision function. Any uncertainty returns ``call_model``."""
    now = (now or _utc_now()).astimezone(dt.timezone.utc)
    base = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": _iso(now),
        "decision": "call_model",
        "call_model": True,
    }
    if not isinstance(context, Mapping):
        return {**base, "reason": "context-missing-or-invalid"}
    context_fp = context_fingerprint(context)
    rejection_fp = rejection_fingerprint(context)
    diplomacy_fp = diplomacy_fingerprint(context)
    fingerprints = {
        "context": context_fp,
        "rejections": rejection_fp,
        "diplomacy": diplomacy_fp,
    }
    base["fingerprints"] = fingerprints
    if not isinstance(policy, Mapping):
        return {**base, "reason": "policy-missing-or-invalid"}
    base["policy_activation_id"] = policy.get("activation_id")
    fingerprints["policy"] = effective_policy_fingerprint(policy)

    fresh, fresh_reason = _policy_freshness(policy, now)
    if not fresh:
        return {**base, "reason": fresh_reason}
    anomalies = _status_anomalies(context, policy)
    if anomalies:
        return {**base, "reason": "observation-anomaly", "signals": anomalies}
    critical = _critical_colonies(context)
    if critical:
        return {**base, "reason": "critical-colony-health", "signals": critical}
    if not isinstance(state, Mapping):
        return {**base, "reason": "refresh-baseline-missing"}
    previous_fp = state.get("last_model_context_fingerprint")
    if not isinstance(previous_fp, str) or not previous_fp:
        return {**base, "reason": "refresh-baseline-missing"}
    last_model_at = _parse_time(state.get("last_model_at"))
    if last_model_at is None:
        return {**base, "reason": "last-model-time-invalid"}
    if (
        not isinstance(max_model_silence_hours, (int, float))
        or max_model_silence_hours <= 0
        or now - last_model_at
        >= dt.timedelta(hours=float(max_model_silence_hours))
    ):
        return {**base, "reason": "maximum-model-silence-reached"}
    if state.get("last_model_activation_id") != policy.get("activation_id"):
        return {**base, "reason": "policy-activation-changed-since-model"}
    if state.get("last_model_policy_fingerprint") != fingerprints["policy"]:
        return {**base, "reason": "effective-policy-changed-since-model"}
    if state.get("last_model_rejection_fingerprint") != rejection_fp:
        return {**base, "reason": "new-action-rejection-event"}
    if state.get("last_model_diplomacy_fingerprint") != diplomacy_fp:
        return {**base, "reason": "new-diplomacy-event"}
    if previous_fp != context_fp:
        return {**base, "reason": "material-context-changed"}
    return {
        **base,
        "decision": "skip_model",
        "call_model": False,
        "reason": "stable-context-and-fresh-policy",
    }


def evaluate_and_record(
    policy_path: pathlib.Path,
    context_path: pathlib.Path,
    state_path: pathlib.Path,
    decision_path: pathlib.Path,
    *,
    now: dt.datetime | None = None,
    max_model_silence_hours: float = DEFAULT_MAX_MODEL_SILENCE_HOURS,
) -> dict[str, Any]:
    now = (now or _utc_now()).astimezone(dt.timezone.utc)
    policy = _load_dict(policy_path)
    context = _load_dict(context_path)
    state = _load_dict(state_path) or {"schema_version": SCHEMA_VERSION, "audit": []}
    try:
        decision = evaluate_refresh_gate(
            policy,
            context,
            state,
            now=now,
            max_model_silence_hours=max_model_silence_hours,
        )
    except Exception as exc:  # noqa: BLE001 - this gate must fail open
        decision = {
            "schema_version": SCHEMA_VERSION,
            "checked_at": _iso(now),
            "decision": "call_model",
            "call_model": True,
            "reason": f"gate-exception:{type(exc).__name__}",
        }
    audit = state.get("audit")
    if not isinstance(audit, list):
        audit = []
    audit.append({
        key: decision.get(key)
        for key in (
            "checked_at", "decision", "reason", "policy_activation_id",
            "fingerprints", "signals",
        )
        if decision.get(key) is not None
    })
    state["schema_version"] = SCHEMA_VERSION
    state["last_decision"] = audit[-1]
    state["audit"] = audit[-AUDIT_MAX:]
    _atomic_json(state_path, state)
    _atomic_json(decision_path, decision)
    return decision


def refresh_policy_liveness(
    policy_path: pathlib.Path,
    decision_path: pathlib.Path,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Refresh TTL after (and only after) a current successful skip decision."""
    now = (now or _utc_now()).astimezone(dt.timezone.utc)
    policy = _load_dict(policy_path)
    decision = _load_dict(decision_path)
    if policy is None or decision is None:
        return {"status": "rejected", "reason": "policy-or-decision-invalid"}
    if decision.get("decision") != "skip_model" or decision.get("call_model") is not False:
        return {"status": "rejected", "reason": "decision-does-not-authorize-skip"}
    checked_at = _parse_time(decision.get("checked_at"))
    if checked_at is None or abs((now - checked_at).total_seconds()) > 300:
        return {"status": "rejected", "reason": "skip-decision-stale"}
    if policy.get("activation_id") != decision.get("policy_activation_id"):
        return {"status": "rejected", "reason": "policy-activation-changed"}
    fresh, reason = _policy_freshness(policy, now)
    if not fresh:
        return {"status": "rejected", "reason": reason}
    stamped = dict(policy)
    stamped["updated_at"] = _iso(now)
    stamped["refreshed_at"] = _iso(now)
    stamped["refresh_reason"] = str(decision.get("reason"))
    _atomic_json(policy_path, stamped)
    return {
        "status": "refreshed",
        "activation_id": stamped.get("activation_id"),
        "refreshed_at": stamped["refreshed_at"],
        "reason": stamped["refresh_reason"],
    }


def commit_model_context(
    state_path: pathlib.Path,
    decision_path: pathlib.Path,
    *,
    activation_id: str | None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit a context baseline only after a model output validates."""
    state = _load_dict(state_path)
    decision = _load_dict(decision_path)
    if state is None or decision is None:
        return {"status": "rejected", "reason": "gate-state-or-decision-invalid"}
    fingerprints = decision.get("fingerprints")
    if not isinstance(fingerprints, Mapping) or not isinstance(
        fingerprints.get("context"), str
    ):
        return {"status": "rejected", "reason": "decision-fingerprints-missing"}
    state["last_model_at"] = decision.get("checked_at")
    state["last_model_activation_id"] = activation_id
    state["last_model_context_fingerprint"] = fingerprints["context"]
    state["last_model_policy_fingerprint"] = (
        effective_policy_fingerprint(policy)
        if isinstance(policy, Mapping)
        else fingerprints.get("policy")
    )
    state["last_model_rejection_fingerprint"] = fingerprints.get("rejections")
    state["last_model_diplomacy_fingerprint"] = fingerprints.get("diplomacy")
    _atomic_json(state_path, state)
    return {
        "status": "committed",
        "activation_id": activation_id,
        "context_fingerprint": fingerprints["context"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--policy", type=pathlib.Path, required=True)
    evaluate.add_argument("--context", type=pathlib.Path, required=True)
    evaluate.add_argument("--state", type=pathlib.Path, required=True)
    evaluate.add_argument("--decision", type=pathlib.Path, required=True)
    evaluate.add_argument(
        "--max-model-silence-hours",
        type=float,
        default=DEFAULT_MAX_MODEL_SILENCE_HOURS,
    )
    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--policy", type=pathlib.Path, required=True)
    refresh.add_argument("--decision", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.command == "evaluate":
        result = evaluate_and_record(
            args.policy,
            args.context,
            args.state,
            args.decision,
            max_model_silence_hours=args.max_model_silence_hours,
        )
    else:
        result = refresh_policy_liveness(args.policy, args.decision)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    # A "call_model" decision is not a process failure. Any genuine exception is
    # caught by evaluate_and_record and represented as a fail-open reason.
    return 0 if result.get("status") != "rejected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
