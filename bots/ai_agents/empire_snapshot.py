"""LLM policy snapshot backed by normalized empire scheduling state."""
from __future__ import annotations

from typing import Any

from bots.shared.game_objects import (
    ASTROPHYSICS,
    DEFENSES,
    SOLAR_SATELLITE,
    name_of,
    solar_satellite_energy_each,
)
from bots.ai_agents import deathstar_goal
from bots.rule_based import empire_scheduler, strategic_growth, turtle


METAL_MINE = 1
CRYSTAL_MINE = 2
DEUTERIUM_SYNTHESIZER = 3
SOLAR_PLANT = 4
SHIPYARD = 21


def _production_factor(resource_state: dict[str, Any]) -> int | None:
    value = resource_state.get("production_factor_pct")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, min(100, int(value)))
    energy_max = resource_state.get("energy_max")
    energy_used = resource_state.get("energy_used")
    if not isinstance(energy_max, (int, float)) or not isinstance(energy_used, (int, float)):
        return None
    if energy_used <= 0:
        return 100
    return max(0, min(100, int(100 * energy_max / energy_used)))


def build_empire_snapshot(email: str) -> dict[str, Any]:
    """Return the legacy policy snapshot plus normalized empire scheduling state.

    ``is_home`` retains its historical current-selection meaning.  New callers
    should use per-planet ``is_current`` and top-level ``canonical_home_id``.
    """
    normalized = empire_scheduler.build_normalized_empire_state(email)
    secondary_goals = {"deathstar": deathstar_goal.goal_snapshot(email)}
    if normalized.get("planet_count") is None:
        return {
            "status": "unavailable",
            "reason": normalized.get("reason") or "planet-list-unavailable",
            "planet_count": None,
            "moon_count": normalized.get("moon_count"),
            "astro_level": None,
            "canonical_home_id": None,
            "secondary_goals": secondary_goals,
            "planets": [],
            "moons": [],
            "worlds": [],
            "normalized_state": normalized,
        }

    canonical_home_id = normalized.get("canonical_home_id")
    normalized_planets = normalized.get("planets", [])
    account = normalized.get("account", {})
    rows: list[dict[str, Any]] = []
    ship_totals = account.get("ship_totals")
    if isinstance(ship_totals, dict):
        ships = {
            name: int(amount)
            for name, amount in sorted(ship_totals.items())
            if int(amount) > 0
        }
        cargo_capacity = (
            int(ship_totals.get("small_cargo", 0)) * 5_000
            + int(ship_totals.get("large_cargo", 0)) * 25_000
        )
    else:
        ship_error = account.get("ship_totals_status", {}).get("reason", "unavailable")
        ships = {"status": "unavailable", "reason": ship_error}
        cargo_capacity = None

    missions = account.get("missions", {})
    if missions.get("status") == "ready" and isinstance(missions.get("by_type"), dict):
        mission_summary = missions["by_type"]
    else:
        mission_summary = {
            "status": "unavailable",
            "reason": missions.get("reason", "unavailable"),
        }
    normalized_fleet_slots = account.get("fleet_slots", {})
    if normalized_fleet_slots.get("available") is True:
        fleet_slots = {
            key: normalized_fleet_slots.get(key) for key in ("active", "maximum", "free")
        }
    else:
        fleet_slots = {"active": None, "maximum": None, "free": None}

    research_levels = account.get("research_levels")
    astro_level = (
        int(research_levels.get(name_of(ASTROPHYSICS), 0))
        if isinstance(research_levels, dict)
        else None
    )
    production_total = account.get("production_per_hour", {})
    for planet in normalized_planets:
        coordinates = planet.get("coordinates", {})
        coord = coordinates.get("label")
        identity = {
            "world_type": "planet",
            "world_id": planet.get("planet_id"),
            "moon_id": None,
            "paired_planet_id": None,
            "planet_id": planet.get("planet_id"),
            "coord": coord,
            "coordinates": coordinates,
            "name": planet.get("name", ""),
            # Backward compatibility: do not redefine this legacy field.
            "is_home": bool(planet.get("is_current")),
            "is_current": bool(planet.get("is_current")),
            "canonical_home_id": canonical_home_id,
            "is_canonical_home": bool(planet.get("is_canonical_home")),
        }
        if planet.get("core_status") != "ready":
            rows.append({
                **identity,
                "status": "unavailable",
                "reason": planet.get("reason") or "planet-state-unavailable",
            })
            continue
        levels = planet.get("relevant_levels", {})
        state = {
            **planet.get("resources", {}),
            "energy": planet.get("energy", {}).get("balance"),
            "energy_max": planet.get("energy", {}).get("maximum"),
            "energy_used": planet.get("energy", {}).get("used"),
            "production_factor_pct": planet.get("energy", {}).get("production_factor_pct"),
            "temp_max": planet.get("temperature", {}).get("maximum"),
        }
        production = planet.get("production_per_hour", {})
        production = {
            key: float(production.get(key) or 0.0)
            for key in ("metal", "crystal", "deuterium")
        }
        local_defense = planet.get("defense", {})
        defense_units = {
            name_of(tid): int(local_defense.get(name_of(tid), 0))
            for tid in DEFENSES
            if int(local_defense.get(name_of(tid), 0)) > 0
        }
        defense_value = sum(
            int(local_defense.get(name_of(tid), 0)) * sum(cost)
            for tid, cost in turtle.DEF_COST.items()
        )
        rows.append({
            **identity,
            "status": "ready",
            "mines": [
                int(levels.get(name_of(METAL_MINE), 0)),
                int(levels.get(name_of(CRYSTAL_MINE), 0)),
                int(levels.get(name_of(DEUTERIUM_SYNTHESIZER), 0)),
            ],
            "solar": int(levels.get(name_of(SOLAR_PLANT), 0)),
            "satellite": int(planet.get("ships", {}).get(name_of(SOLAR_SATELLITE), 0)),
            "shipyard": int(levels.get(name_of(SHIPYARD), 0)),
            "energy_balance": state.get("energy"),
            "temp_max": state.get("temp_max"),
            "solar_satellite_energy_per_unit": (
                solar_satellite_energy_each(int(state["temp_max"]))
                if isinstance(state.get("temp_max"), (int, float))
                and not isinstance(state.get("temp_max"), bool)
                else None
            ),
            "production_factor_pct": _production_factor(state),
            "resources": {
                key: state.get(key) for key in ("metal", "crystal", "deuterium")
            },
            "production_per_hour": {
                key: round(float(production.get(key, 0.0) or 0.0), 2)
                for key in production_total
            },
            "defense_units": defense_units,
            "defense_value": defense_value,
        })

    moon_rows: list[dict[str, Any]] = []
    for moon in normalized.get("moons", []):
        moon_rows.append({
            "world_type": "moon",
            "world_id": moon.get("moon_id"),
            "moon_id": moon.get("moon_id"),
            "planet_id": moon.get("paired_planet_id"),
            "paired_planet_id": moon.get("paired_planet_id"),
            "paired_planet_coordinates": moon.get("paired_planet_coordinates"),
            "coord": moon.get("coordinates", {}).get("label"),
            "coordinates": moon.get("coordinates", {}),
            "name": moon.get("name", ""),
            "is_home": False,
            "is_current": bool(moon.get("is_current")),
            "canonical_home_id": canonical_home_id,
            "is_canonical_home": False,
            "status": moon.get("status", "unavailable"),
            "reason": moon.get("reason"),
            "resources": moon.get("resources"),
            "fields": moon.get("fields"),
            "fields_used": moon.get("fields_used"),
            "fields_max": moon.get("fields_max"),
            "lunar_base_level": moon.get("lunar_base_level"),
            "robotics_level": moon.get("robotics_level"),
            "shipyard_level": moon.get("shipyard_level"),
            "sensor_phalanx_level": moon.get("sensor_phalanx_level"),
            "jump_gate_level": moon.get("jump_gate_level"),
            "building_queue": moon.get("queues", {}).get("building"),
            "unit_queue": moon.get("queues", {}).get("unit"),
            "queues": moon.get("queues"),
            "phalanx_range": moon.get("phalanx_range"),
            "jump_cooldown_seconds": moon.get("jump_cooldown_seconds"),
            "jump_ready_at": moon.get("jump_ready_at"),
        })

    world_rows = [*rows, *moon_rows]
    ready = sum(1 for row in world_rows if row.get("status") == "ready")
    return {
        "status": "ready" if ready == len(world_rows) else ("partial" if ready else "unavailable"),
        "reason": None if ready == len(world_rows) else "one-or-more-worlds-unavailable",
        "planet_count": normalized.get("planet_count"),
        "moon_count": normalized.get("moon_count"),
        "world_count": normalized.get("world_count"),
        "astro_level": astro_level,
        "canonical_home_id": canonical_home_id,
        "ships_account_total": ships,
        "cargo_capacity": cargo_capacity,
        "active_missions_by_type": mission_summary,
        "fleet_slots": fleet_slots,
        "raid_opportunity": strategic_growth.opportunity_snapshot(email),
        "secondary_goals": secondary_goals,
        "production_per_hour_account": {
            key: round(float(production_total.get(key) or 0.0), 2)
            for key in ("metal", "crystal", "deuterium")
        },
        "planets": rows,
        "moons": moon_rows,
        "worlds": world_rows,
        "normalized_state": normalized,
    }
