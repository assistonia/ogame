"""Persistent executor and observer for the optional Deathstar/Graviton goal."""
from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
from typing import Any

from bots.shared import db_reader
from bots.shared import planets as planets_mod
from bots.shared.game_objects import object_cost, ship_cost, solar_satellite_energy_each

try:
    from bots.rule_based.production_capacity import cap_ship_amount as _cap_ship_amount
except (ImportError, AttributeError):
    _cap_ship_amount = None


MANAGED_ACCOUNTS = frozenset(f"ai{i:02d}" for i in range(1, 11))
STATE_ROOT = pathlib.Path(__file__).resolve().parent / "state"

ROBOT_FACTORY = 14
NANITE_FACTORY = 15
SHIPYARD = 21
RESEARCH_LAB = 31
ENERGY_TECHNOLOGY = 113
SHIELDING_TECHNOLOGY = 110
HYPERSPACE_TECHNOLOGY = 114
HYPERSPACE_DRIVE = 118
GRAVITON_TECHNOLOGY = 199
SOLAR_SATELLITE = 212
DEATHSTAR = 214
GRAVITON_ENERGY = 300_000
DEATHSTAR_PRICE = {
    "metal": 5_000_000,
    "crystal": 4_000_000,
    "deuterium": 1_000_000,
}

FACILITY_CHAIN = (
    (ROBOT_FACTORY, 2, "robot_factory"),
    (RESEARCH_LAB, 12, "research_lab"),
    (SHIPYARD, 12, "shipyard"),
)
RESEARCH_CHAIN = (
    (ENERGY_TECHNOLOGY, 5, "energy_technology"),
    (SHIELDING_TECHNOLOGY, 5, "shielding_technology"),
    (HYPERSPACE_TECHNOLOGY, 6, "hyperspace_technology"),
    (HYPERSPACE_DRIVE, 7, "hyperspace_drive"),
)


def _account(email: str) -> str:
    return email.split("@", 1)[0]


def is_managed_account(email: str) -> bool:
    return _account(email) in MANAGED_ACCOUNTS


def _state_path(email: str) -> pathlib.Path:
    return STATE_ROOT / _account(email) / "deathstar_goal.json"


def _read_state(email: str) -> dict[str, Any]:
    try:
        value = json.loads(_state_path(email).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(email: str, value: dict[str, Any]) -> None:
    path = _state_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _bounded(value: Any, default: int, lower: int, upper: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(lower, min(upper, value))


def _controls(goal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_budget = goal.get("budget") if isinstance(goal.get("budget"), dict) else {}
    raw_risk = goal.get("risk") if isinstance(goal.get("risk"), dict) else {}
    raw_reserve = (
        raw_budget.get("reserve_resources")
        if isinstance(raw_budget.get("reserve_resources"), dict)
        else {}
    )
    budget = {
        "max_satellite_batch": _bounded(raw_budget.get("max_satellite_batch"), 64, 1, 256),
        "satellite_horizon_seconds": _bounded(
            raw_budget.get("satellite_horizon_seconds"), 21_600, 1_800, 86_400
        ),
        "max_deathstar_batch": _bounded(raw_budget.get("max_deathstar_batch"), 1, 1, 5),
        "deathstar_horizon_seconds": _bounded(
            raw_budget.get("deathstar_horizon_seconds"), 604_800, 21_600, 604_800
        ),
        "target_deathstars": _bounded(raw_budget.get("target_deathstars"), 1, 1, 25),
        "max_resource_commitment_pct": _bounded(
            raw_budget.get("max_resource_commitment_pct"), 35, 1, 100
        ),
        "reserve_resources": {
            key: _bounded(raw_reserve.get(key), default, 0, 1_000_000_000)
            for key, default in (
                ("metal", 100_000),
                ("crystal", 100_000),
                ("deuterium", 50_000),
            )
        },
    }
    risk = {
        "allow_exposed_satellites": raw_risk.get("allow_exposed_satellites") is True,
        "max_exposed_satellites": _bounded(
            raw_risk.get("max_exposed_satellites"), 12_000, 1, 25_000
        ),
        "min_nanite_level": _bounded(raw_risk.get("min_nanite_level"), 1, 0, 10),
        "satellites_without_nanite_limit": _bounded(
            raw_risk.get("satellites_without_nanite_limit"), 1_000, 0, 25_000
        ),
    }
    return budget, risk


def _queues(email: str, planet_ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    planets = {
        pid: {
            "building_busy": False,
            "building_targets": [],
            "unit_busy": False,
            "unit_targets": [],
        }
        for pid in planet_ids
    }
    research = {"busy": False, "targets": []}
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bq.planet_id,bq.object_id FROM building_queues bq "
            "JOIN planets p ON p.id=bq.planet_id JOIN users u ON u.id=p.user_id "
            "WHERE u.email=%s AND bq.processed=0 AND bq.canceled=0 ORDER BY bq.id",
            (email,),
        )
        for row in cur.fetchall():
            pid = int(row["planet_id"])
            if pid in planets:
                planets[pid]["building_busy"] = True
                planets[pid]["building_targets"].append(int(row["object_id"]))
        cur.execute(
            "SELECT rq.object_id FROM research_queues rq "
            "JOIN planets p ON p.id=rq.planet_id JOIN users u ON u.id=p.user_id "
            "WHERE u.email=%s AND rq.processed=0 AND rq.canceled=0 ORDER BY rq.id",
            (email,),
        )
        research["targets"] = [int(row["object_id"]) for row in cur.fetchall()]
        research["busy"] = bool(research["targets"])
        cur.execute(
            "SELECT uq.planet_id,uq.object_id,"
            "SUM(GREATEST(uq.object_amount-uq.object_amount_progress,0)) amount "
            "FROM unit_queues uq JOIN planets p ON p.id=uq.planet_id "
            "JOIN users u ON u.id=p.user_id WHERE u.email=%s AND uq.processed=0 "
            "GROUP BY uq.planet_id,uq.object_id ORDER BY MIN(uq.id)",
            (email,),
        )
        for row in cur.fetchall():
            pid = int(row["planet_id"])
            if pid in planets:
                planets[pid]["unit_busy"] = True
                planets[pid]["unit_targets"].append(
                    {"id": int(row["object_id"]), "amount": max(0, int(row["amount"] or 0))}
                )
    return planets, research


def _gap(current: int, required: int) -> dict[str, int | bool]:
    return {
        "current": current,
        "required": required,
        "gap": max(0, required - current),
        "met": current >= required,
    }


def _next_step(option: dict[str, Any], levels: dict[int, int]) -> dict[str, Any]:
    for object_id, target, label in FACILITY_CHAIN:
        if int(levels.get(object_id, 0)) < target:
            return {"kind": "facility", "id": object_id, "target": target, "label": label}
    for object_id, target, label in RESEARCH_CHAIN:
        if int(levels.get(object_id, 0)) < target:
            return {"kind": "research", "id": object_id, "target": target, "label": label}
    if option["energy_available"] < GRAVITON_ENERGY:
        return {"kind": "satellites", "id": SOLAR_SATELLITE, "label": "solar_satellite"}
    if int(levels.get(GRAVITON_TECHNOLOGY, 0)) < 1:
        return {
            "kind": "research",
            "id": GRAVITON_TECHNOLOGY,
            "target": 1,
            "label": "graviton_technology",
        }
    return {"kind": "deathstar", "id": DEATHSTAR, "label": "deathstar"}


def _live(email: str, goal: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    budget, risk = _controls(goal)
    planets = db_reader.list_planets_db(email)
    if not planets:
        raise RuntimeError("no-planets")
    planet_ids = [int(planet["id"]) for planet in planets]
    queue_error = None
    try:
        queue_map, research_queue = _queues(email, planet_ids)
    except Exception as exc:  # noqa: BLE001
        queue_error = type(exc).__name__
        queue_map = {
            pid: {
                "status": "unavailable",
                "building_busy": None,
                "building_targets": [],
                "unit_busy": None,
                "unit_targets": [],
            }
            for pid in planet_ids
        }
        research_queue = {"status": "unavailable", "busy": None, "targets": []}

    options: list[dict[str, Any]] = []
    level_map: dict[int, dict[int, int]] = {}
    resource_map: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for planet in planets:
        pid = int(planet["id"])
        coord = f"{planet['galaxy']}:{planet['system']}:{planet['position']}"
        try:
            levels = db_reader.get_levels_db(email, planet_id=pid)
            resources = db_reader.get_resource_state_db(email, planet_id=pid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{coord}:{type(exc).__name__}")
            continue
        level_map[pid] = levels
        resource_map[pid] = resources
        temp = resources.get("temp_max")
        output = (
            solar_satellite_energy_each(int(temp))
            if isinstance(temp, (int, float)) and not isinstance(temp, bool)
            else None
        )
        energy = int(resources.get("energy") or 0)
        energy_gap = max(0, GRAVITON_ENERGY - energy)
        estimate = math.ceil(energy_gap / output) if output is not None and output > 0 else None
        options.append(
            {
                "planet_id": pid,
                "coord": coord,
                "name": planet.get("name", ""),
                "is_current": bool(planet.get("is_current")),
                "temp_max": int(temp) if temp is not None else None,
                "solar_satellite_energy_per_unit": output,
                "energy_available": energy,
                "energy_gap": energy_gap,
                "estimated_additional_satellites": estimate,
                "current_satellites": int(levels.get(SOLAR_SATELLITE, 0)),
                "robot_factory": int(levels.get(ROBOT_FACTORY, 0)),
                "research_lab": int(levels.get(RESEARCH_LAB, 0)),
                "shipyard": int(levels.get(SHIPYARD, 0)),
                "nanite_factory": int(levels.get(NANITE_FACTORY, 0)),
                "resources": {
                    key: int(resources.get(key) or 0)
                    for key in ("metal", "crystal", "deuterium")
                },
                "queue_constraints": queue_map[pid],
            }
        )
    if not options:
        raise RuntimeError("planet-observation-unavailable")

    by_id = {option["planet_id"]: option for option in options}
    preferred = goal.get("planet_id")
    preferred = preferred if isinstance(preferred, int) and not isinstance(preferred, bool) else None
    persisted = state.get("selected_planet_id")
    persisted = persisted if isinstance(persisted, int) and not isinstance(persisted, bool) else None
    selection_rejection = (
        f"preferred-planet-{preferred}-unavailable"
        if preferred is not None and preferred not in by_id
        else None
    )
    if preferred in by_id:
        selected = by_id[preferred]
        selection_source = "policy"
    elif persisted in by_id:
        selected = by_id[persisted]
        selection_source = "persisted"
    else:
        def score(option: dict[str, Any]) -> tuple[int, int, int, int, int]:
            facility_gap = (
                max(0, 2 - option["robot_factory"])
                + max(0, 12 - option["research_lab"])
                + max(0, 12 - option["shipyard"])
            )
            estimate = option["estimated_additional_satellites"]
            output = option["solar_satellite_energy_per_unit"]
            return (
                facility_gap,
                int(estimate) if estimate is not None else 1_000_000_000,
                -option["nanite_factory"],
                -int(output) if output is not None else 1_000_000_000,
                option["planet_id"],
            )
        selected = min(options, key=score)
        selection_source = "deterministic-score"

    pid = int(selected["planet_id"])
    levels = level_map[pid]
    step = _next_step(selected, levels)
    estimate = selected["estimated_additional_satellites"]
    risk_rejections: list[str] = []
    if step["kind"] == "satellites":
        if estimate is None:
            risk_rejections.append("satellite-output-unavailable-or-zero")
        if risk["allow_exposed_satellites"] is not True:
            risk_rejections.append("exposed-satellites-not-authorized")
        if (
            estimate is not None
            and selected["current_satellites"] + estimate > risk["max_exposed_satellites"]
        ):
            risk_rejections.append("max-exposed-satellites-exceeded")
        if (
            estimate is not None
            and estimate > risk["satellites_without_nanite_limit"]
            and selected["nanite_factory"] < risk["min_nanite_level"]
        ):
            risk_rejections.append("nanite-build-time-threshold")

    try:
        deathstars = int(db_reader.get_account_ship_totals_db(email).get(DEATHSTAR, 0))
        deathstar_error = None
    except Exception as exc:  # noqa: BLE001
        deathstars = None
        deathstar_error = type(exc).__name__

    sat_price = ship_cost(SOLAR_SATELLITE) or {
        "metal": 0,
        "crystal": 2_000,
        "deuterium": 500,
    }
    return {
        "status": "ready" if queue_error is None and not errors else "partial",
        "reason": queue_error or (";".join(errors) if errors else None),
        "priority": "secondary",
        "policy": {
            "mode": goal.get("mode", "evaluate"),
            "reason": str(goal.get("reason", ""))[:300],
            "planet_id": preferred,
            "budget": budget,
            "risk": risk,
        },
        "persisted_state": state,
        "server_requirements": {
            "deathstar": {
                "shipyard": 12,
                "hyperspace_drive": 7,
                "hyperspace_technology": 6,
                "graviton_technology": 1,
            },
            "graviton": {"research_lab": 12, "available_energy": GRAVITON_ENERGY},
        },
        "requirements": {
            "shipyard": _gap(selected["shipyard"], 12),
            "hyperspace_drive": _gap(int(levels.get(HYPERSPACE_DRIVE, 0)), 7),
            "hyperspace_technology": _gap(int(levels.get(HYPERSPACE_TECHNOLOGY, 0)), 6),
            "graviton_technology": _gap(int(levels.get(GRAVITON_TECHNOLOGY, 0)), 1),
            "graviton_research_lab": _gap(selected["research_lab"], 12),
            "graviton_available_energy": _gap(selected["energy_available"], GRAVITON_ENERGY),
            "supporting_energy_technology": _gap(
                int(levels.get(ENERGY_TECHNOLOGY, 0)), 5
            ),
            "supporting_shielding_technology": _gap(
                int(levels.get(SHIELDING_TECHNOLOGY, 0)), 5
            ),
        },
        "research_queue": research_queue,
        "selected_planet": selected,
        "selection_source": selection_source,
        "selection_rejection": selection_rejection,
        "planet_options": options,
        "next_step": step,
        "risk_rejections": risk_rejections,
        "opportunity_cost": {
            "estimated_satellite_resources": (
                {key: int(cost) * int(estimate) for key, cost in sat_price.items()}
                if estimate is not None
                else None
            ),
            "satellites_are_exposed": True,
            "nanite_factory_level": selected["nanite_factory"],
            "building_and_unit_queues_are_exclusive": True,
        },
        "deathstars_account_total": deathstars,
        "deathstar_total_error": deathstar_error,
        "current_planet_id": next(
            (int(planet["id"]) for planet in planets if planet.get("is_current")), None
        ),
        "_levels": levels,
        "_resources": resource_map[pid],
        "_queue_error": queue_error,
    }


def goal_snapshot(email: str) -> dict[str, Any]:
    """Expose exact live gaps and the last executor decision to the next LLM tick."""
    try:
        from bots.ai_agents.policy_engine import load_policy

        goals = load_policy(email).get("secondary_goals")
        goal = goals.get("deathstar", {}) if isinstance(goals, dict) else {}
    except Exception as exc:  # noqa: BLE001
        goal = {
            "mode": "evaluate" if is_managed_account(email) else "pause",
            "reason": f"policy-unavailable:{type(exc).__name__}",
        }
    state = _read_state(email)
    try:
        return {
            key: value
            for key, value in _live(email, goal, state).items()
            if not key.startswith("_")
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unavailable",
            "reason": f"goal-observation:{type(exc).__name__}:{exc}",
            "priority": "secondary",
            "policy": {
                "mode": goal.get("mode", "evaluate"),
                "reason": str(goal.get("reason", ""))[:300],
            },
            "persisted_state": state,
            "server_requirements": {
                "deathstar": {
                    "shipyard": 12,
                    "hyperspace_drive": 7,
                    "hyperspace_technology": 6,
                    "graviton_technology": 1,
                },
                "graviton": {"research_lab": 12, "available_energy": GRAVITON_ENERGY},
            },
        }


def _cost_allowed(
    resources: dict[str, Any], cost: dict[str, int], budget: dict[str, Any]
) -> tuple[bool, str]:
    reserve = budget["reserve_resources"]
    for key in ("metal", "crystal", "deuterium"):
        available = int(resources.get(key) or 0)
        required = int(cost.get(key, 0)) + int(reserve.get(key, 0))
        if available < required:
            return False, f"resource-reserve:{key}:{available}<{required}"
    liquid = sum(int(resources.get(key) or 0) for key in ("metal", "crystal", "deuterium"))
    commitment = sum(int(cost.get(key, 0)) for key in ("metal", "crystal", "deuterium"))
    if commitment * 100 > liquid * int(budget["max_resource_commitment_pct"]):
        return False, "resource-commitment-budget"
    return True, ""


def _affordable_units(
    resources: dict[str, Any],
    unit_cost: dict[str, int],
    requested: int,
    budget: dict[str, Any],
) -> int:
    amount = max(0, requested)
    reserve = budget["reserve_resources"]
    for key in ("metal", "crystal", "deuterium"):
        price = int(unit_cost.get(key, 0))
        if price > 0:
            spendable = max(0, int(resources.get(key) or 0) - int(reserve.get(key, 0)))
            amount = min(amount, spendable // price)
    unit_total = sum(int(unit_cost.get(key, 0)) for key in ("metal", "crystal", "deuterium"))
    liquid = sum(int(resources.get(key) or 0) for key in ("metal", "crystal", "deuterium"))
    if unit_total > 0:
        amount = min(
            amount,
            liquid * int(budget["max_resource_commitment_pct"]) // (100 * unit_total),
        )
    return max(0, amount)


def _capacity(
    email: str,
    object_id: int,
    requested: int,
    levels: dict[int, int],
    horizon: int,
    planet_id: int | None = None,
) -> tuple[int | None, str | None]:
    if _cap_ship_amount is None:
        return None, "production-capacity-unavailable"
    try:
        amount = _cap_ship_amount(
            email,
            object_id,
            requested,
            levels=levels,
            horizon_seconds=horizon,
            planet_id=planet_id,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"production-capacity-error:{type(exc).__name__}"
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None, "production-capacity-invalid"
    return max(0, min(requested, amount)), None


def _success(result: Any) -> bool:
    return isinstance(result, dict) and (
        result.get("success") is True
        or str(result.get("status", "")).lower() in {"success", "accepted", "ok"}
    )


def _message(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("message") or result.get("error") or result.get("status") or result)
    return str(result)


def _finish(
    email: str,
    state: dict[str, Any],
    *,
    mode: str,
    planet_id: int | None,
    phase: str,
    status: str,
    reason: str,
    action: dict[str, Any] | None = None,
) -> str:
    value = dict(state)
    old_planet = value.get("selected_planet_id")
    if planet_id is not None and old_planet not in (None, planet_id):
        value["replan_count"] = int(value.get("replan_count", 0)) + 1
    value.update(
        {
            "version": 1,
            "account": _account(email),
            "mode": mode,
            "selected_planet_id": planet_id,
            "phase": phase,
            "last_status": status,
            "last_reason": reason,
            "last_action": action,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
    )
    try:
        _write_state(email, value)
        suffix = ""
    except OSError as exc:
        suffix = f"; state-write-failed:{type(exc).__name__}"
    return f"deathstar-goal {status}: {reason}{suffix}"


def _submit_on_planet(client, target: int, original: int | None, submit) -> tuple[Any, bool]:
    if original != target and not planets_mod.switch_planet(client, target):
        raise RuntimeError("planet-switch-failed")
    restore_failed = False
    try:
        result = submit()
    finally:
        if original is not None and original != target:
            restore_failed = not planets_mod.switch_planet(client, original)
    return result, restore_failed


def execute(client, email: str, policy_goal: dict[str, Any]) -> str:
    """Execute at most one safe step and return an explicit state note."""
    state = _read_state(email)
    mode = policy_goal.get("mode") if isinstance(policy_goal, dict) else None
    if not is_managed_account(email):
        return _finish(
            email,
            state,
            mode=str(mode or "pause"),
            planet_id=None,
            phase="inactive",
            status="blocked",
            reason="account-not-managed",
        )
    if mode != "pursue":
        return _finish(
            email,
            state,
            mode=str(mode or "evaluate"),
            planet_id=state.get("selected_planet_id"),
            phase=str(state.get("phase", "evaluate")),
            status="deferred",
            reason="policy-not-pursue",
        )
    try:
        live = _live(email, policy_goal, state)
    except Exception as exc:  # noqa: BLE001
        return _finish(
            email,
            state,
            mode="pursue",
            planet_id=state.get("selected_planet_id"),
            phase="observe",
            status="blocked",
            reason=f"observation-unavailable:{type(exc).__name__}:{exc}",
        )

    selected = live["selected_planet"]
    planet_id = int(selected["planet_id"])
    step = live["next_step"]
    phase = str(step["label"])
    common = {
        "email": email,
        "state": state,
        "mode": "pursue",
        "planet_id": planet_id,
        "phase": phase,
    }
    if live.get("selection_rejection"):
        return _finish(**common, status="blocked", reason=str(live["selection_rejection"]))
    if live.get("_queue_error") is not None:
        return _finish(**common, status="blocked", reason="queue-observation-unavailable")

    budget = live["policy"]["budget"]
    levels = live["_levels"]
    resources = live["_resources"]
    queues = selected["queue_constraints"]
    verify_kind = None

    if step["kind"] == "facility":
        if queues.get("building_busy") is not False:
            return _finish(**common, status="deferred", reason="building-queue-busy")
        target = int(levels.get(step["id"], 0)) + 1
        cost = object_cost(int(step["id"]), target)
        if cost is None:
            return _finish(**common, status="blocked", reason="facility-cost-unavailable")
        allowed, reason = _cost_allowed(resources, cost, budget)
        if not allowed:
            return _finish(**common, status="deferred", reason=reason)
        submit = lambda: client.build_facility(int(step["id"]))
        verify_kind = "building"
        action = {"kind": "facility", "id": int(step["id"]), "target": target}
    elif step["kind"] == "research":
        if live["research_queue"].get("busy") is not False:
            return _finish(**common, status="deferred", reason="research-queue-busy")
        if step["id"] == GRAVITON_TECHNOLOGY:
            if selected["energy_available"] < GRAVITON_ENERGY:
                return _finish(
                    **common, status="deferred", reason="graviton-energy-below-300000"
                )
        else:
            target = int(levels.get(step["id"], 0)) + 1
            cost = object_cost(int(step["id"]), target)
            if cost is None:
                return _finish(**common, status="blocked", reason="research-cost-unavailable")
            allowed, reason = _cost_allowed(resources, cost, budget)
            if not allowed:
                return _finish(**common, status="deferred", reason=reason)
        submit = lambda: client.research(int(step["id"]))
        verify_kind = "research"
        action = {"kind": "research", "id": int(step["id"])}
    elif step["kind"] == "satellites":
        if live["risk_rejections"]:
            return _finish(
                **common, status="blocked", reason=",".join(live["risk_rejections"])
            )
        if queues.get("unit_busy") is not False:
            return _finish(**common, status="deferred", reason="unit-queue-busy")
        needed = int(selected["estimated_additional_satellites"] or 0)
        requested = min(needed, int(budget["max_satellite_batch"]))
        amount, error = _capacity(
            email,
            SOLAR_SATELLITE,
            requested,
            levels,
            int(budget["satellite_horizon_seconds"]),
            planet_id=planet_id,
        )
        if error:
            return _finish(**common, status="blocked", reason=error)
        price = ship_cost(SOLAR_SATELLITE)
        if price is None:
            return _finish(**common, status="blocked", reason="satellite-cost-unavailable")
        amount = _affordable_units(resources, price, int(amount or 0), budget)
        if amount < 1:
            return _finish(
                **common, status="deferred", reason="satellite-budget-or-capacity-zero"
            )
        submit = lambda: client.build_ships(SOLAR_SATELLITE, amount)
        action = {"kind": "ship", "id": SOLAR_SATELLITE, "amount": amount}
    else:
        if queues.get("unit_busy") is not False:
            return _finish(**common, status="deferred", reason="unit-queue-busy")
        current = live.get("deathstars_account_total")
        if current is None:
            return _finish(**common, status="blocked", reason="deathstar-total-unavailable")
        remaining = int(budget["target_deathstars"]) - int(current)
        if remaining <= 0:
            return _finish(**common, status="deferred", reason="target-deathstars-reached")
        requested = min(remaining, int(budget["max_deathstar_batch"]))
        amount, error = _capacity(
            email,
            DEATHSTAR,
            requested,
            levels,
            int(budget["deathstar_horizon_seconds"]),
            planet_id=planet_id,
        )
        if error:
            return _finish(**common, status="blocked", reason=error)
        amount = _affordable_units(resources, DEATHSTAR_PRICE, int(amount or 0), budget)
        if amount < 1:
            return _finish(
                **common, status="deferred", reason="deathstar-budget-or-capacity-zero"
            )
        submit = lambda: client.build_ships(DEATHSTAR, amount)
        action = {"kind": "ship", "id": DEATHSTAR, "amount": amount}

    try:
        result, restore_failed = _submit_on_planet(
            client, planet_id, live.get("current_planet_id"), submit
        )
    except Exception as exc:  # noqa: BLE001
        return _finish(
            **common,
            status="blocked",
            reason=f"submit-error:{type(exc).__name__}:{exc}",
        )
    if not _success(result):
        return _finish(
            **common, status="deferred", reason=f"server-rejected:{_message(result)}"
        )
    if verify_kind is not None:
        try:
            canceled = db_reader.latest_queue_canceled(
                email, int(step["id"]), verify_kind, planet_id=planet_id
            )
        except Exception as exc:  # noqa: BLE001
            return _finish(
                **common,
                status="blocked",
                reason=f"queue-verification-error:{type(exc).__name__}",
                action=action,
            )
        if canceled != 0:
            return _finish(
                **common,
                status="deferred" if canceled == 1 else "blocked",
                reason="queue-canceled" if canceled == 1 else "queue-verification-missing",
                action=action,
            )
    reason = f"{phase} accepted"
    if restore_failed:
        reason += "; original-planet-restore-failed"
    return _finish(**common, status="acted", reason=reason, action=action)
