"""Conservatively reconcile queued colony-development receipts.

Only exact, machine-readable targets are eligible.  Human-readable notes are
never parsed and queue disappearance alone is never treated as completion.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bots.ai_agents import action_feedback
from bots.shared.game_objects import BUILDINGS, DEFENSES, FACILITIES, RESEARCH, SHIPS


DEFAULT_STATE_ROOT = Path(__file__).resolve().parent / "state"
RECENT_LIMIT = 200
MAX_SNAPSHOT_AGE_SECONDS = 300
STALL_AFTER_SECONDS = 12 * 60 * 60
_QUEUE_BY_TYPE = {
    "building": "building",
    "facility": "building",
    "research": "research",
    "ship": "unit",
    "defense": "unit",
}
_NAMES_BY_TYPE = {
    "building": BUILDINGS,
    "facility": FACILITIES,
    "research": RESEARCH,
    "ship": SHIPS,
    "defense": DEFENSES,
}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _root(empire_state: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = empire_state.get("normalized_state")
    return nested if isinstance(nested, Mapping) else empire_state


def _recorded_timestamp(receipt: Mapping[str, Any]) -> int | None:
    raw = receipt.get("occurred_at", receipt.get("recorded_at"))
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _queued_actions(
    receipt: Mapping[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    planet_id = _positive_int(receipt.get("planet_id"))
    details = receipt.get("details")
    if planet_id is None or not isinstance(details, Mapping):
        return None, "machine_target_missing"
    raw_actions = details.get("queued_actions")
    if (
        not isinstance(raw_actions, Sequence)
        or isinstance(raw_actions, (str, bytes))
        or not raw_actions
    ):
        return None, "machine_target_missing"
    untracked = details.get("untracked_action_count", 0)
    if not isinstance(untracked, int) or isinstance(untracked, bool) or untracked != 0:
        return None, "untracked_actions_present"

    actions: list[dict[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            return None, "machine_target_invalid"
        object_type = str(raw.get("object_type") or "").strip().lower()
        object_id = _positive_int(raw.get("object_id"))
        if (
            object_type not in _NAMES_BY_TYPE
            or object_id not in _NAMES_BY_TYPE.get(object_type, {})
        ):
            return None, "machine_target_invalid"
        target_key = (
            "target_total"
            if object_type in {"ship", "defense"}
            else "target_level"
        )
        target = _positive_int(raw.get(target_key))
        action_planet_id = _positive_int(raw.get("planet_id"))
        if target is None or action_planet_id != planet_id:
            return None, "machine_target_invalid"
        actions.append({
            "object_type": object_type,
            "object_id": object_id,
            "planet_id": planet_id,
            target_key: target,
        })
    return actions, None


def _planet(root: Mapping[str, Any], planet_id: int) -> Mapping[str, Any] | None:
    planets = root.get("planets")
    if not isinstance(planets, Sequence) or isinstance(planets, (str, bytes)):
        return None
    return next(
        (
            row
            for row in planets
            if isinstance(row, Mapping)
            and _positive_int(row.get("planet_id", row.get("id"))) == planet_id
        ),
        None,
    )


def _queue(
    root: Mapping[str, Any],
    planet: Mapping[str, Any],
    object_type: str,
) -> Mapping[str, Any] | None:
    if object_type == "research":
        account = root.get("account")
        return (
            account.get("research_queue")
            if isinstance(account, Mapping)
            and isinstance(account.get("research_queue"), Mapping)
            else None
        )
    queues = planet.get("queues")
    name = _QUEUE_BY_TYPE[object_type]
    return (
        queues.get(name)
        if isinstance(queues, Mapping) and isinstance(queues.get(name), Mapping)
        else None
    )


def _current_value(
    root: Mapping[str, Any],
    planet: Mapping[str, Any],
    object_type: str,
    object_id: int,
) -> int | None:
    name = _NAMES_BY_TYPE[object_type][object_id]
    if object_type == "research":
        account = root.get("account")
        levels = (
            account.get("research_levels")
            if isinstance(account, Mapping)
            else None
        )
    elif object_type == "ship":
        levels = planet.get("ships")
    elif object_type == "defense":
        levels = planet.get("defense")
    else:
        raw_levels = planet.get("levels")
        bucket = "buildings" if object_type == "building" else "facilities"
        levels = (
            raw_levels.get(bucket)
            if isinstance(raw_levels, Mapping)
            else None
        )
    if not isinstance(levels, Mapping):
        return None
    value = levels.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _matching_queue_item(
    queue: Mapping[str, Any],
    action: Mapping[str, Any],
) -> bool:
    items = queue.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return False
    object_id = action["object_id"]
    object_type = action["object_type"]
    for item in items:
        if (
            not isinstance(item, Mapping)
            or _positive_int(item.get("object_id")) != object_id
        ):
            continue
        if object_type in {"ship", "defense"}:
            remaining = item.get("remaining_amount")
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                return int(remaining) > 0
            return True
        queued_target = _positive_int(item.get("object_level_target"))
        if queued_target is None or queued_target >= action["target_level"]:
            return True
    return False


def _observe_receipt(
    receipt: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    now: int,
) -> tuple[str | None, str, list[dict[str, Any]]]:
    actions, schema_reason = _queued_actions(receipt)
    if actions is None:
        return None, schema_reason or "machine_target_invalid", []
    planet_id = actions[0]["planet_id"]
    planet = _planet(root, planet_id)
    if planet is None:
        return None, "planet_state_unavailable", []

    observations: list[dict[str, Any]] = []
    any_active = False
    any_unavailable = False
    all_achieved = True
    for action in actions:
        current = _current_value(
            root,
            planet,
            action["object_type"],
            action["object_id"],
        )
        target_key = (
            "target_total"
            if action["object_type"] in {"ship", "defense"}
            else "target_level"
        )
        target = action[target_key]
        achieved = current is not None and current >= target
        queue = _queue(root, planet, action["object_type"])
        queue_items = queue.get("items") if isinstance(queue, Mapping) else None
        queue_available = (
            isinstance(queue, Mapping)
            and queue.get("available") is True
            and isinstance(queue_items, Sequence)
            and not isinstance(queue_items, (str, bytes))
        )
        active = (
            not achieved
            and queue_available
            and _matching_queue_item(queue, action)
        )
        any_active = any_active or active
        any_unavailable = any_unavailable or current is None or not queue_available
        all_achieved = all_achieved and achieved
        observations.append({
            **action,
            "current": current,
            "target": target,
            "achieved": achieved,
            "queue_available": queue_available,
            "matching_queue_item": active,
        })

    if all_achieved:
        return "completed", "development_targets_observed", observations
    if any_unavailable:
        return None, "target_or_queue_state_unavailable", observations
    if any_active:
        return None, "development_queue_active", observations
    queued_at = _recorded_timestamp(receipt)
    if queued_at is None:
        return None, "queued_timestamp_unavailable", observations
    if max(0, now - queued_at) < STALL_AFTER_SECONDS:
        return None, "development_queue_absent_within_grace", observations
    return "stalled", "development_target_missing_after_queue_grace", observations


def reconcile_queued_actions(
    email: str,
    empire_state: Mapping[str, Any],
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    now: int | None = None,
) -> dict[str, Any]:
    """Append terminal transitions supported by a fresh normalized snapshot."""
    current_time = int(time.time()) if now is None else int(now)
    root = _root(empire_state)
    observed_at = _positive_int(root.get("observed_at"))
    if (
        root.get("status") == "unavailable"
        or observed_at is None
        or observed_at > current_time + 60
        or current_time - observed_at > MAX_SNAPSHOT_AGE_SECONDS
    ):
        return {
            "status": "unavailable",
            "reason": "fresh_empire_state_required",
            "examined": 0,
            "transitioned": 0,
        }

    recent = action_feedback.load_recent_action_outcomes(
        email,
        limit=RECENT_LIMIT,
        state_root=state_root,
    )
    latest_by_command: dict[str, Mapping[str, Any]] = {}
    for row in recent:
        command_id = row.get("command_id")
        if isinstance(command_id, str) and command_id not in latest_by_command:
            latest_by_command[command_id] = row

    candidates = [
        row
        for row in latest_by_command.values()
        if row.get("action") == "development"
        and action_feedback.lifecycle_status(
            row.get("lifecycle", row.get("status"))
        ) == "queued"
    ]
    transitioned = 0
    pending = 0
    reasons: Counter[str] = Counter()
    for receipt in candidates:
        status, reason, observations = _observe_receipt(
            receipt,
            root,
            now=current_time,
        )
        reasons[reason] += 1
        if status is None:
            pending += 1
            continue
        transition = {
            "command_id": receipt["command_id"],
            "action": "development",
            "status": status,
            "lifecycle": status,
            "reason": reason,
            "policy_activation_id": receipt.get("policy_activation_id"),
            "planet_id": receipt.get("planet_id"),
            "coordinates": receipt.get("coordinates"),
            "details": {
                "reconciled_from_sequence": receipt.get("sequence"),
                "observed_at": observed_at,
                "queued_actions": (
                    receipt.get("details", {}).get("queued_actions")
                    if isinstance(receipt.get("details"), Mapping)
                    else None
                ),
                "observations": observations,
            },
        }
        _stored, appended = action_feedback.append_action_transition_if_current(
            email,
            transition,
            expected_lifecycles={"queued"},
            state_root=state_root,
        )
        transitioned += int(appended)

    return {
        "status": "ready",
        "observed_at": observed_at,
        "examined": len(candidates),
        "transitioned": transitioned,
        "pending": pending,
        "reasons": dict(sorted(reasons.items())),
    }
