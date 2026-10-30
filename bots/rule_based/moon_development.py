"""Field-safe moon development planning and guarded facility execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from bots.rule_based import empire_scheduler
from bots.shared import moons, planets as planets_mod


LUNAR_BASE = 41
ROBOTICS = 14
SHIPYARD = 21
SENSOR_PHALANX = 42
JUMP_GATE = 43
RESOURCE_KEYS = ("metal", "crystal", "deuterium")
FACILITY_NAMES = {
    LUNAR_BASE: "lunar_base",
    ROBOTICS: "robot_factory",
    SHIPYARD: "shipyard",
    SENSOR_PHALANX: "sensor_phalanx",
    JUMP_GATE: "jump_gate",
}
FACILITY_IDS = {name: object_id for object_id, name in FACILITY_NAMES.items()}
FACILITY_ORDER = (LUNAR_BASE, ROBOTICS, SHIPYARD, SENSOR_PHALANX, JUMP_GATE)
ROLES = ("intel_hub", "fleet_hub", "industrial_moon", "balanced")

ROLE_TARGETS = {
    "intel_hub": {
        "lunar_base": 8,
        "robot_factory": 6,
        "shipyard": 2,
        "sensor_phalanx": 6,
        "jump_gate": 1,
    },
    "fleet_hub": {
        "lunar_base": 9,
        "robot_factory": 6,
        "shipyard": 6,
        "sensor_phalanx": 3,
        "jump_gate": 1,
    },
    "industrial_moon": {
        "lunar_base": 10,
        "robot_factory": 8,
        "shipyard": 8,
        "sensor_phalanx": 2,
        "jump_gate": 1,
    },
    "balanced": {
        "lunar_base": 9,
        "robot_factory": 6,
        "shipyard": 6,
        "sensor_phalanx": 4,
        "jump_gate": 1,
    },
}

ROLE_BENEFIT = {
    "intel_hub": {
        "lunar_base": 3.0,
        "robot_factory": 2.0,
        "shipyard": 1.0,
        "sensor_phalanx": 10.0,
        "jump_gate": 7.0,
    },
    "fleet_hub": {
        "lunar_base": 4.0,
        "robot_factory": 5.0,
        "shipyard": 7.0,
        "sensor_phalanx": 4.0,
        "jump_gate": 12.0,
    },
    "industrial_moon": {
        "lunar_base": 5.0,
        "robot_factory": 10.0,
        "shipyard": 10.0,
        "sensor_phalanx": 2.0,
        "jump_gate": 5.0,
    },
    "balanced": {
        "lunar_base": 5.0,
        "robot_factory": 6.0,
        "shipyard": 6.0,
        "sensor_phalanx": 6.0,
        "jump_gate": 9.0,
    },
}

BASE_COSTS = {
    LUNAR_BASE: {"metal": 20_000, "crystal": 40_000, "deuterium": 20_000},
    ROBOTICS: {"metal": 400, "crystal": 120, "deuterium": 200},
    SHIPYARD: {"metal": 400, "crystal": 200, "deuterium": 100},
    SENSOR_PHALANX: {"metal": 20_000, "crystal": 40_000, "deuterium": 20_000},
    JUMP_GATE: {"metal": 2_000_000, "crystal": 4_000_000, "deuterium": 2_000_000},
}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _root(state: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = state.get("normalized_state")
    return nested if isinstance(nested, Mapping) else state


def _moon_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    root = _root(state)
    rows = root.get("moons")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        worlds = root.get("worlds")
        rows = worlds if isinstance(worlds, Sequence) and not isinstance(worlds, (str, bytes)) else []
    return [
        row for row in rows
        if isinstance(row, Mapping) and row.get("world_type", "moon") == "moon"
    ]


def _levels(moon: Mapping[str, Any]) -> dict[str, int] | None:
    source = moon.get("relevant_levels")
    if not isinstance(source, Mapping):
        nested = moon.get("levels")
        source = nested.get("facilities") if isinstance(nested, Mapping) else None
    if not isinstance(source, Mapping):
        return None
    result: dict[str, int] = {}
    for name in FACILITY_IDS:
        value = _int(source.get(name))
        if value is None:
            return None
        result[name] = max(0, value)
    return result


def _facility_cost(facility_id: int, target_level: int) -> dict[str, int]:
    multiplier = 2 ** max(0, target_level - 1)
    return {key: amount * multiplier for key, amount in BASE_COSTS[facility_id].items()}


def _build_time_seconds(
    cost: Mapping[str, int], robotics_level: int, economy_speed: int
) -> int:
    speed = max(1, economy_speed)
    hours = (int(cost["metal"]) + int(cost["crystal"])) / (
        2_500 * (1 + max(0, robotics_level)) * speed
    )
    return max(1, math.ceil(hours * 3_600))


def _prerequisite_issue(
    facility_id: int,
    levels: Mapping[str, int],
    research: Mapping[str, Any],
) -> str | None:
    if facility_id == SHIPYARD and levels["robot_factory"] < 2:
        return "requires-robot-factory-2"
    if facility_id == SENSOR_PHALANX and levels["lunar_base"] < 3:
        return "requires-lunar-base-3"
    if facility_id == JUMP_GATE:
        hyperspace = _int(research.get("hyperspace_technology"))
        if levels["lunar_base"] < 1:
            return "requires-lunar-base-1"
        if hyperspace is None:
            return "hyperspace-technology-unavailable"
        if hyperspace < 7:
            return "requires-hyperspace-technology-7"
    return None


def _payoff(
    facility_id: int,
    target_level: int,
    levels: Mapping[str, int],
    build_seconds: int,
) -> dict[str, Any]:
    name = FACILITY_NAMES[facility_id]
    result: dict[str, Any] = {
        "facility": name,
        "target_level": target_level,
        "benefit_available_after_completion": True,
    }
    if facility_id == LUNAR_BASE:
        result.update({
            "fields_added_after_completion": 3,
            "future_fields_counted_before_completion": 0,
        })
    elif facility_id == ROBOTICS:
        before = 1 + levels["robot_factory"]
        after = 1 + target_level
        result["subsequent_build_time_reduction_pct"] = round(100 * (1 - before / after), 2)
    elif facility_id == SHIPYARD:
        result["moon_shipyard_throughput_factor"] = target_level + 1
    elif facility_id == SENSOR_PHALANX:
        result["phalanx_range_systems"] = max(0, target_level * target_level - 1)
    elif facility_id == JUMP_GATE:
        result["intermoon_instant_transfer_enabled"] = target_level >= 1
    result["build_time_seconds"] = build_seconds
    return result


def _candidate(
    moon: Mapping[str, Any],
    facility_id: int,
    role: str,
    levels: Mapping[str, int],
    research: Mapping[str, Any],
    resources: Mapping[str, Any],
    free_fields: int,
    economy_speed: int,
) -> dict[str, Any]:
    name = FACILITY_NAMES[facility_id]
    target = levels[name] + 1
    cost = _facility_cost(facility_id, target)
    build_seconds = _build_time_seconds(cost, levels["robot_factory"], economy_speed)
    prerequisite = _prerequisite_issue(facility_id, levels, research)
    field_issue = None
    if facility_id == LUNAR_BASE and free_fields < 1:
        field_issue = "no-field-for-next-lunar-base"
    elif facility_id != LUNAR_BASE and free_fields < 2:
        field_issue = "reserved-field-required-for-next-lunar-base"
    missing_resource = next(
        (
            key for key in RESOURCE_KEYS
            if not isinstance(resources.get(key), (int, float))
            or isinstance(resources.get(key), bool)
        ),
        None,
    )
    affordable = missing_resource is None and all(
        float(resources[key]) >= cost[key] for key in RESOURCE_KEYS
    )
    weighted_cost = cost["metal"] + cost["crystal"] + 1.5 * cost["deuterium"]
    benefit = ROLE_BENEFIT[role][name]
    score = benefit * 1_000_000_000 / (weighted_cost * (1 + build_seconds / 3_600))
    issue = prerequisite or field_issue
    if issue is None and missing_resource is not None:
        issue = f"resource-{missing_resource}-unavailable"
    if issue is None and not affordable:
        issue = "insufficient-resources"
    return {
        "facility_id": facility_id,
        "facility": name,
        "current_level": levels[name],
        "target_level": target,
        "cost": cost,
        "weighted_cost": int(weighted_cost),
        "build_time_seconds": build_seconds,
        "strategic_benefit": benefit,
        "benefit_cost_time_score": round(score, 6),
        "affordable": affordable,
        "eligible": issue is None,
        "issue": issue,
        "payoff": _payoff(facility_id, target, levels, build_seconds),
    }


def _required_phase(levels: Mapping[str, int], targets: Mapping[str, int]) -> int | None:
    milestones = (
        (LUNAR_BASE, 1),
        (ROBOTICS, min(2, targets["robot_factory"])),
        (SHIPYARD, min(2, targets["shipyard"])),
        (SENSOR_PHALANX, min(1, targets["sensor_phalanx"])),
        (JUMP_GATE, min(1, targets["jump_gate"])),
    )
    for facility_id, target in milestones:
        if levels[FACILITY_NAMES[facility_id]] < target:
            return facility_id
    return None


def _dependency_facility(issue: str | None) -> int | None:
    if issue in {"requires-lunar-base-1", "requires-lunar-base-3", "reserved-field-required-for-next-lunar-base"}:
        return LUNAR_BASE
    if issue == "requires-robot-factory-2":
        return ROBOTICS
    return None


def _moon_plan(
    moon: Mapping[str, Any],
    role: str,
    research: Mapping[str, Any],
    economy_speed: int,
) -> dict[str, Any]:
    moon_id = _int(moon.get("moon_id"))
    base = {"moon_id": moon_id, "role": role, "command": None}
    if moon_id is None:
        return {**base, "status": "unavailable", "reason": "moon-id-unavailable", "comparisons": []}
    levels = _levels(moon)
    if levels is None:
        return {**base, "status": "unavailable", "reason": "facility-levels-unavailable", "comparisons": []}
    fields = moon.get("fields") if isinstance(moon.get("fields"), Mapping) else {}
    used = _int(fields.get("used", moon.get("fields_used")))
    maximum = _int(fields.get("maximum", moon.get("fields_max")))
    if used is None or maximum is None or used < 0 or maximum < used:
        return {**base, "status": "unavailable", "reason": "field-state-unavailable", "comparisons": []}
    queues = moon.get("queues") if isinstance(moon.get("queues"), Mapping) else {}
    for queue_name in ("building", "unit"):
        queue = queues.get(queue_name) if isinstance(queues.get(queue_name), Mapping) else {}
        if queue.get("available") is not True:
            return {**base, "status": "unavailable", "reason": f"{queue_name}-queue-unavailable", "comparisons": []}
        if queue.get("occupied") is not False:
            return {**base, "status": "hold", "reason": f"{queue_name}-queue-occupied", "comparisons": []}
    resources = moon.get("resources") if isinstance(moon.get("resources"), Mapping) else {}
    targets = ROLE_TARGETS[role]
    comparisons = [
        _candidate(
            moon,
            facility_id,
            role,
            levels,
            research,
            resources,
            maximum - used,
            economy_speed,
        )
        for facility_id in FACILITY_ORDER
        if levels[FACILITY_NAMES[facility_id]] < targets[FACILITY_NAMES[facility_id]]
    ]
    phase = _required_phase(levels, targets)
    selected = next((row for row in comparisons if row["facility_id"] == phase), None)
    while selected is not None and not selected["eligible"]:
        dependency = _dependency_facility(selected.get("issue"))
        replacement = next(
            (row for row in comparisons if row["facility_id"] == dependency), None
        )
        if replacement is None or replacement is selected:
            break
        selected = replacement
    if phase is None:
        eligible = [row for row in comparisons if row["eligible"]]
        selected = min(
            eligible,
            key=lambda row: (-row["benefit_cost_time_score"], FACILITY_ORDER.index(row["facility_id"])),
            default=None,
        )
    if selected is None:
        reason = "role-targets-complete" if not comparisons else "no-eligible-facility"
        return {**base, "status": "complete" if not comparisons else "hold", "reason": reason, "comparisons": comparisons}
    if not selected["eligible"]:
        return {**base, "status": "hold", "reason": selected["issue"], "comparisons": comparisons}

    facility = selected["facility"]
    target = selected["target_level"]
    command_id = f"moon-development:{moon_id}:{selected['facility_id']}:{target}"
    reason = (
        f"{role} sequence selects {facility} {target}; compared facility cost, "
        "build time, field safety, prerequisites, and strategic benefit"
    )
    command = {
        "command_id": command_id,
        "action": "build_moon_facility",
        "moon_id": moon_id,
        "paired_planet_id": _int(moon.get("paired_planet_id", moon.get("planet_id"))),
        "role": role,
        **selected,
        "reason": reason,
    }
    return {**base, "status": "planned", "reason": reason, "command": command, "comparisons": comparisons}


def plan_moon_development(
    empire_state: Mapping[str, Any] | str,
    *,
    roles: Mapping[int | str, str] | None = None,
    default_role: str = "balanced",
    economy_speed: int = 1,
) -> dict[str, Any]:
    """Plan at most one completed-level facility upgrade for each owned moon."""
    if default_role not in ROLES:
        return {"status": "unavailable", "reason": "invalid-default-role", "commands": [], "moon_plans": []}
    if not isinstance(economy_speed, int) or isinstance(economy_speed, bool) or economy_speed <= 0:
        return {"status": "unavailable", "reason": "invalid-economy-speed", "commands": [], "moon_plans": []}
    state = (
        empire_scheduler.build_normalized_empire_state(empire_state)
        if isinstance(empire_state, str)
        else empire_state
    )
    if not isinstance(state, Mapping):
        return {"status": "unavailable", "reason": "invalid-empire-state", "commands": [], "moon_plans": []}
    root = _root(state)
    moon_rows = _moon_rows(state)
    if not moon_rows and root.get("moon_count") is None:
        return {
            "status": "unavailable",
            "reason": "moon-state-unavailable",
            "commands": [],
            "moon_plans": [],
            "plans": [],
        }
    account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
    research = account.get("research_levels") if isinstance(account.get("research_levels"), Mapping) else {}
    role_map = roles if isinstance(roles, Mapping) else {}
    plans: list[dict[str, Any]] = []
    for moon in sorted(moon_rows, key=lambda row: (_int(row.get("moon_id")) is None, _int(row.get("moon_id")) or 0)):
        moon_id = _int(moon.get("moon_id"))
        role = role_map.get(moon_id, role_map.get(str(moon_id), default_role))
        if role not in ROLES:
            plans.append({"moon_id": moon_id, "role": role, "status": "unavailable", "reason": "invalid-role", "command": None, "comparisons": []})
            continue
        plans.append(_moon_plan(moon, role, research, economy_speed))
    commands = [row["command"] for row in plans if isinstance(row.get("command"), Mapping)]
    unavailable = [row for row in plans if row.get("status") == "unavailable"]
    return {
        "status": "ready" if not unavailable else ("partial" if commands else "unavailable"),
        "reason": None if not unavailable else "one-or-more-moons-unavailable",
        "planner": "field-safe-moon-development-v1",
        "one_step_per_moon": True,
        "field_policy": "reserve-one-completed-field-for-next-lunar-base",
        "roles": list(ROLES),
        "commands": commands,
        "moon_plans": plans,
        "plans": plans,
    }


def _command_list(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    commands = plan.get("commands")
    if isinstance(commands, Sequence) and not isinstance(commands, (str, bytes)):
        return [row for row in commands if isinstance(row, Mapping)]
    command = plan.get("command")
    return [command] if isinstance(command, Mapping) else []


def _switch_to_moon(client: Any, moon_id: int) -> bool:
    switch = getattr(moons, "switch_moon", None)
    if not callable(switch):
        switch = getattr(moons, "switch_to_moon", None)
    if not callable(switch):
        switch = getattr(moons, "safe_switch_moon", None)
    if not callable(switch):
        return False
    result = switch(client, moon_id)
    if result is True:
        return True
    return isinstance(result, Mapping) and (
        result.get("status") == "success" or result.get("success") is True
    )


def _current_world(state: Mapping[str, Any]) -> dict[str, Any] | None:
    root = _root(state)
    worlds = root.get("worlds")
    if not isinstance(worlds, Sequence) or isinstance(worlds, (str, bytes)):
        worlds = [*root.get("planets", []), *root.get("moons", [])]
    current = [row for row in worlds if isinstance(row, Mapping) and row.get("is_current") is True]
    if len(current) != 1:
        return None
    row = current[0]
    world_type = row.get("world_type", "planet")
    world_id = _int(row.get("moon_id") if world_type == "moon" else row.get("planet_id"))
    return {"world_type": world_type, "world_id": world_id} if world_id is not None else None


def _restore_world(client: Any, original: Mapping[str, Any]) -> bool:
    world_id = _int(original.get("world_id"))
    if world_id is None:
        return False
    if original.get("world_type") == "moon":
        return _switch_to_moon(client, world_id)
    return planets_mod.switch_planet(client, world_id) is True


def _fresh_command_check(
    state: Mapping[str, Any], command: Mapping[str, Any], economy_speed: int
) -> tuple[bool, str, Mapping[str, Any] | None]:
    moon_id = _int(command.get("moon_id"))
    facility_id = _int(command.get("facility_id"))
    target = _int(command.get("target_level"))
    if moon_id is None or facility_id not in FACILITY_NAMES or target is None:
        return False, "invalid-command", None
    moon = next((row for row in _moon_rows(state) if _int(row.get("moon_id")) == moon_id), None)
    if moon is None or moon.get("is_current") is not True:
        return False, "moon-switch-not-verified", moon
    role = command.get("role")
    if role not in ROLES:
        return False, "invalid-role", moon
    levels = _levels(moon)
    if levels is None or target != levels[FACILITY_NAMES[facility_id]] + 1:
        return False, "stale-target-level", moon
    fields = moon.get("fields") if isinstance(moon.get("fields"), Mapping) else {}
    used = _int(fields.get("used", moon.get("fields_used")))
    maximum = _int(fields.get("maximum", moon.get("fields_max")))
    if used is None or maximum is None or maximum < used:
        return False, "field-state-unavailable", moon
    queues = moon.get("queues") if isinstance(moon.get("queues"), Mapping) else {}
    for queue_name in ("building", "unit"):
        queue = queues.get(queue_name) if isinstance(queues.get(queue_name), Mapping) else {}
        if queue.get("available") is not True:
            return False, f"{queue_name}-queue-unavailable", moon
        if queue.get("occupied") is not False:
            return False, f"{queue_name}-queue-occupied", moon
    root = _root(state)
    account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
    research = account.get("research_levels") if isinstance(account.get("research_levels"), Mapping) else {}
    resources = moon.get("resources") if isinstance(moon.get("resources"), Mapping) else {}
    fresh = _candidate(moon, facility_id, role, levels, research, resources, maximum - used, economy_speed)
    if fresh.get("target_level") != target:
        return False, "stale-target-level", moon
    if fresh.get("eligible") is not True:
        return False, str(fresh.get("issue") or "facility-not-eligible"), moon
    if command.get("cost") != fresh.get("cost"):
        return False, "planned-cost-mismatch", moon
    expected_id = f"moon-development:{moon_id}:{facility_id}:{target}"
    if command.get("command_id") != expected_id:
        return False, "invalid-command-id", moon
    return True, "verified", moon


def execute_moon_development_plan(
    client: Any,
    email: str,
    plan: Mapping[str, Any],
    *,
    economy_speed: int = 1,
) -> dict[str, Any]:
    """Execute no more than one verified facility level per moon, then restore."""
    if not isinstance(plan, Mapping):
        return {"status": "unavailable", "reason": "invalid-plan", "outcomes": [], "restore_ok": False}
    if not isinstance(economy_speed, int) or isinstance(economy_speed, bool) or economy_speed <= 0:
        return {"status": "unavailable", "reason": "invalid-economy-speed", "outcomes": [], "restore_ok": False}
    commands = _command_list(plan)
    seen: set[int] = set()
    for command in commands:
        moon_id = _int(command.get("moon_id"))
        if moon_id is None or moon_id in seen:
            return {"status": "unavailable", "reason": "duplicate-or-invalid-moon-command", "outcomes": [], "restore_ok": False}
        seen.add(moon_id)
    initial = empire_scheduler.build_normalized_empire_state(email)
    original = _current_world(initial)
    if original is None:
        return {"status": "unavailable", "reason": "starting-world-unavailable", "outcomes": [], "restore_ok": False}

    outcomes: list[dict[str, Any]] = []
    restore_ok = False
    try:
        for command in sorted(commands, key=lambda row: _int(row.get("moon_id")) or 0):
            moon_id = int(command["moon_id"])
            outcome = {
                "command_id": command.get("command_id"),
                "moon_id": moon_id,
                "target_level": command.get("target_level"),
                "facility": command.get("facility"),
                "executed": False,
                "reason": None,
                "payoff": command.get("payoff"),
            }
            try:
                if not _switch_to_moon(client, moon_id):
                    outcome["reason"] = "moon-switch-failed"
                    outcomes.append(outcome)
                    continue
                fresh_state = empire_scheduler.build_normalized_empire_state(email)
                valid, reason, _moon = _fresh_command_check(fresh_state, command, economy_speed)
                if not valid:
                    outcome["reason"] = reason
                    outcomes.append(outcome)
                    continue
                result = client.build_facility(int(command["facility_id"]))
                success = isinstance(result, Mapping) and (
                    result.get("status") == "success" or result.get("success") is True
                )
                outcome["executed"] = success
                outcome["reason"] = "submitted" if success else "facility-build-rejected"
                outcomes.append(outcome)
            except Exception as exc:  # noqa: BLE001 - fail closed per moon
                outcome["reason"] = f"execution-error:{type(exc).__name__}"
                outcomes.append(outcome)
    finally:
        try:
            restore_ok = _restore_world(client, original)
        except Exception:  # noqa: BLE001 - restoration result remains explicit
            restore_ok = False

    executed = sum(1 for row in outcomes if row.get("executed") is True)
    failed = len(outcomes) - executed
    status = "ready" if failed == 0 and restore_ok else "partial" if executed else "unavailable"
    reason = None
    if not restore_ok:
        reason = "starting-world-restore-failed"
    elif failed:
        reason = "one-or-more-commands-not-executed"
    return {
        "status": status,
        "reason": reason,
        "executed_count": executed,
        "attempted_count": len(outcomes),
        "one_step_per_moon": True,
        "restore_ok": restore_ok,
        "outcomes": outcomes,
    }
