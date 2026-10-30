"""Read-only empire state and pure scheduling primitives.

This module may read the database, but it never selects planets, submits game
commands, or mutates live state.  Missing observations are represented as
unavailable values so callers can fail closed.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import time
from typing import Any

from bots.shared import db_reader, moons
from bots.shared.game_objects import (
    ASTROPHYSICS,
    BUILDINGS,
    DEFENSES,
    FACILITIES,
    RESEARCH,
    SHIPS,
    name_of,
)


RESOURCE_KEYS = ("metal", "crystal", "deuterium")
MOON_FACILITIES = {
    41: "lunar_base",
    14: "robot_factory",
    21: "shipyard",
    42: "sensor_phalanx",
    43: "jump_gate",
}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, value)


def _coordinate(row: Mapping[str, Any]) -> dict[str, int | str | None]:
    galaxy = _int_or_none(row.get("galaxy"))
    system = _int_or_none(row.get("system"))
    position = _int_or_none(row.get("position"))
    label = (
        f"{galaxy}:{system}:{position}"
        if galaxy is not None and system is not None and position is not None
        else None
    )
    return {
        "galaxy": galaxy,
        "system": system,
        "position": position,
        "label": label,
    }


def _select_canonical_home(
    planets: Iterable[Mapping[str, Any]],
) -> tuple[int | None, str]:
    rows = list(planets)
    valid = [(row, _int_or_none(row.get("id", row.get("planet_id")))) for row in rows]
    valid = [(row, pid) for row, pid in valid if pid is not None]
    if not valid:
        return None, "unavailable"

    explicit_ids = {
        _int_or_none(row.get("canonical_home_id"))
        for row, _pid in valid
        if _int_or_none(row.get("canonical_home_id")) is not None
    }
    for row, pid in valid:
        if pid in explicit_ids or row.get("is_canonical_home") is True or row.get("is_homeworld") is True:
            return pid, "explicit"

    # The current selection and the legacy ``is_home`` snapshot field are
    # deliberately ignored.  Both are session state, not durable identity.
    for key in ("created_at", "creation_time", "time_created", "created"):
        dated = [(row.get(key), pid) for row, pid in valid if row.get(key) is not None]
        if dated:
            def order(item: tuple[Any, int]) -> tuple[int, Any, int]:
                value, pid = item
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return (0, value, pid)
                return (1, str(value), pid)

            return min(dated, key=order)[1], f"oldest-{key}"

    return min(pid for _row, pid in valid), "lowest-planet-id"


def choose_canonical_home_id(planets: Iterable[Mapping[str, Any]]) -> int | None:
    """Return a durable home planet id, never inferring it from current selection."""
    return _select_canonical_home(planets)[0]


def _unavailable_queue(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "available": False,
        "occupied": None,
        "depth": None,
        "remaining_seconds": None,
        "items": [],
        "reason": reason,
    }


def _queue_from_rows(rows: list[Mapping[str, Any]], observed_at: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    ends: list[int] = []
    timing_complete = True
    for row in rows:
        end = _int_or_none(row.get("time_end"))
        if end is None:
            timing_complete = False
        else:
            ends.append(end)
        item = {
            "object_id": _int_or_none(row.get("object_id")),
            "object_name": name_of(_int_or_none(row.get("object_id")) or 0),
            "time_start": _int_or_none(row.get("time_start")),
            "time_end": end,
            "remaining_seconds": max(0, end - observed_at) if end is not None else None,
        }
        for key in ("object_level_target", "object_amount", "object_amount_progress"):
            if key in row:
                item[key] = _int_or_none(row.get(key))
        if "object_amount" in item:
            amount = item.get("object_amount")
            progress = item.get("object_amount_progress")
            item["remaining_amount"] = (
                max(0, amount - (progress or 0)) if amount is not None else None
            )
        items.append(item)
    return {
        "status": "ready",
        "available": True,
        "occupied": bool(rows),
        "depth": len(rows),
        "remaining_seconds": (
            max(0, max(ends) - observed_at)
            if rows and timing_complete and ends
            else (0 if not rows else None)
        ),
        "items": items,
        "reason": None,
    }


def _first_present(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source.get(key) is not None:
            return source.get(key)
    return None


def _moon_queue(
    source: Mapping[str, Any], queue_name: str, observed_at: int
) -> dict[str, Any]:
    queues = source.get("queues") if isinstance(source.get("queues"), Mapping) else {}
    raw = queues.get(queue_name)
    if raw is None:
        raw = _first_present(
            source,
            f"{queue_name}_queue",
            f"{queue_name}_queues",
        )
    if isinstance(raw, Mapping) and "available" in raw:
        result = dict(raw)
        result.setdefault("status", "ready" if raw.get("available") is True else "unavailable")
        result.setdefault("occupied", None)
        result.setdefault("depth", None)
        result.setdefault("remaining_seconds", None)
        result.setdefault("items", [])
        result.setdefault("reason", None)
        return result
    if isinstance(raw, Mapping):
        raw = [raw]
    if isinstance(raw, (list, tuple)) and all(isinstance(row, Mapping) for row in raw):
        return _queue_from_rows(list(raw), observed_at)
    return _unavailable_queue("moon-queue-unavailable")


def _moon_facility_levels(source: Mapping[str, Any]) -> dict[str, int | None]:
    levels = source.get("levels") if isinstance(source.get("levels"), Mapping) else {}
    facilities = (
        levels.get("facilities")
        if isinstance(levels.get("facilities"), Mapping)
        else source.get("facilities")
        if isinstance(source.get("facilities"), Mapping)
        else levels
    )
    aliases = {
        "lunar_base": ("lunar_base", "lunar_base_level"),
        "robot_factory": ("robot_factory", "robotics", "robotics_level"),
        "shipyard": ("shipyard", "shipyard_level"),
        "sensor_phalanx": ("sensor_phalanx", "sensor_phalanx_level"),
        "jump_gate": ("jump_gate", "jump_gate_level"),
    }
    normalized: dict[str, int | None] = {}
    for object_id, name in MOON_FACILITIES.items():
        value = _first_present(facilities, name, str(object_id))
        if value is None:
            value = _first_present(source, *aliases[name])
        parsed = _int_or_none(value)
        normalized[name] = max(0, parsed) if parsed is not None else None
    return normalized


def _normalize_moon_world(
    source: Mapping[str, Any],
    planets_by_id: Mapping[int, Mapping[str, Any]],
    observed_at: int,
    canonical_home_id: int | None,
) -> dict[str, Any]:
    moon_id = _int_or_none(_first_present(source, "moon_id", "id"))
    paired_planet_id = _int_or_none(
        _first_present(source, "paired_planet_id", "parent_planet_id", "planet_id")
    )
    paired_planet = planets_by_id.get(paired_planet_id or -1, {})
    coordinate_source = source.get("coordinates")
    if not isinstance(coordinate_source, Mapping):
        coordinate_source = source
    coordinates = _coordinate(coordinate_source)
    if coordinates["label"] is None and isinstance(paired_planet, Mapping):
        coordinates = _coordinate(paired_planet)

    resource_source = (
        source.get("resources")
        if isinstance(source.get("resources"), Mapping)
        else source.get("resource_state")
        if isinstance(source.get("resource_state"), Mapping)
        else source
    )
    field_source = source.get("fields") if isinstance(source.get("fields"), Mapping) else {}
    fields_used = _int_or_none(
        _first_present(field_source, "used", "current")
    )
    if fields_used is None:
        fields_used = _int_or_none(
            _first_present(source, "fields_used", "field_current", "fields")
        )
    fields_max = _int_or_none(
        _first_present(field_source, "maximum", "max")
    )
    if fields_max is None:
        fields_max = _int_or_none(
            _first_present(source, "fields_max", "field_max", "max_fields")
        )

    facilities = _moon_facility_levels(source)
    queues = {
        "building": _moon_queue(source, "building", observed_at),
        "unit": _moon_queue(source, "unit", observed_at),
    }
    resources = {
        key: _number_or_none(resource_source.get(key)) for key in RESOURCE_KEYS
    }
    phalanx_level = facilities.get("sensor_phalanx")
    phalanx_range = _int_or_none(
        _first_present(source, "phalanx_range", "sensor_phalanx_range")
    )
    if phalanx_range is None and phalanx_level is not None:
        phalanx_range = max(0, phalanx_level * phalanx_level - 1)

    jump_cooldown = _int_or_none(
        _first_present(
            source,
            "jump_cooldown_seconds",
            "jump_gate_cooldown_seconds",
            "jump_cooldown_remaining",
            "jump_gate_cooldown_remaining",
        )
    )
    jump_ready_at = _int_or_none(
        _first_present(source, "jump_ready_at", "jump_gate_ready_at", "jump_cooldown_until")
    )
    if jump_cooldown is None and jump_ready_at is not None:
        jump_cooldown = max(0, jump_ready_at - observed_at)

    issues: list[str] = []
    for condition, reason in (
        (moon_id is None, "moon-id-unavailable"),
        (paired_planet_id is None, "paired-planet-id-unavailable"),
        (any(value is None for value in resources.values()), "resources-unavailable"),
        (fields_used is None or fields_max is None, "fields-unavailable"),
        (any(value is None for value in facilities.values()), "facility-levels-unavailable"),
    ):
        if condition:
            issues.append(reason)
    for queue_name, queue in queues.items():
        if queue.get("available") is not True:
            issues.append(f"{queue_name}-queue:{queue.get('reason') or 'unavailable'}")

    return {
        "world_type": "moon",
        "world_id": moon_id,
        "moon_id": moon_id,
        "planet_id": paired_planet_id,
        "paired_planet_id": paired_planet_id,
        "paired_planet_coordinates": coordinates,
        "name": str(source.get("name") or source.get("moon_name") or ""),
        "coordinates": coordinates,
        "is_current": source.get("is_current") in (True, 1) or source.get("current") in (True, 1),
        "is_canonical_home": False,
        "canonical_home_id": canonical_home_id,
        "status": "ready" if not issues else "partial",
        "core_status": "ready" if not issues[:5] else "unavailable",
        "reason": None if not issues else "one-or-more-observations-unavailable",
        "issues": issues,
        "resources": resources,
        "fields": {
            "used": fields_used,
            "maximum": fields_max,
            "free": (
                max(0, fields_max - fields_used)
                if fields_used is not None and fields_max is not None
                else None
            ),
        },
        "fields_used": fields_used,
        "fields_max": fields_max,
        "levels": {"facilities": facilities},
        "relevant_levels": facilities,
        "lunar_base_level": facilities.get("lunar_base"),
        "robotics_level": facilities.get("robot_factory"),
        "shipyard_level": facilities.get("shipyard"),
        "sensor_phalanx_level": facilities.get("sensor_phalanx"),
        "jump_gate_level": facilities.get("jump_gate"),
        "queues": queues,
        "phalanx_range": phalanx_range,
        "jump_cooldown_seconds": jump_cooldown,
        "jump_ready_at": jump_ready_at,
    }


def _read_queue_states(
    email: str,
    planet_ids: Iterable[int],
    observed_at: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    per_planet = {
        pid: {
            "building": _unavailable_queue("not-read"),
            "unit": _unavailable_queue("not-read"),
        }
        for pid in planet_ids
    }

    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bq.planet_id,bq.object_id,bq.object_level_target,"
                "bq.time_start,bq.time_end FROM building_queues bq "
                "JOIN planets p ON p.id=bq.planet_id JOIN users u ON u.id=p.user_id "
                "WHERE u.email=%s AND bq.processed=0 AND bq.canceled=0 ORDER BY bq.id",
                (email,),
            )
            rows = cur.fetchall()
        grouped: dict[int, list[Mapping[str, Any]]] = {pid: [] for pid in per_planet}
        for row in rows:
            pid = _int_or_none(row.get("planet_id"))
            if pid in grouped:
                grouped[pid].append(row)
        for pid, queue_rows in grouped.items():
            per_planet[pid]["building"] = _queue_from_rows(queue_rows, observed_at)
    except Exception as exc:  # noqa: BLE001 - queue uncertainty must remain visible
        reason = type(exc).__name__
        for queues in per_planet.values():
            queues["building"] = _unavailable_queue(reason)

    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uq.planet_id,uq.object_id,uq.object_amount,"
                "uq.object_amount_progress,uq.time_start,uq.time_end FROM unit_queues uq "
                "JOIN planets p ON p.id=uq.planet_id JOIN users u ON u.id=p.user_id "
                "WHERE u.email=%s AND uq.processed=0 ORDER BY uq.id",
                (email,),
            )
            rows = cur.fetchall()
        grouped = {pid: [] for pid in per_planet}
        for row in rows:
            pid = _int_or_none(row.get("planet_id"))
            if pid in grouped:
                grouped[pid].append(row)
        for pid, queue_rows in grouped.items():
            per_planet[pid]["unit"] = _queue_from_rows(queue_rows, observed_at)
    except Exception as exc:  # noqa: BLE001
        reason = type(exc).__name__
        for queues in per_planet.values():
            queues["unit"] = _unavailable_queue(reason)

    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rq.planet_id,rq.object_id,rq.object_level_target,"
                "rq.time_start,rq.time_end FROM research_queues rq "
                "JOIN planets p ON p.id=rq.planet_id JOIN users u ON u.id=p.user_id "
                "WHERE u.email=%s AND rq.processed=0 AND rq.canceled=0 ORDER BY rq.id",
                (email,),
            )
            research_rows = cur.fetchall()
        research = _queue_from_rows(research_rows, observed_at)
    except Exception as exc:  # noqa: BLE001
        research = _unavailable_queue(type(exc).__name__)
    return per_planet, research


def _read_fleet_state(email: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fm.mission_type,COUNT(*) n FROM fleet_missions fm "
                "JOIN users u ON u.id=fm.user_id WHERE u.email=%s "
                "AND fm.processed=0 AND fm.canceled=0 GROUP BY fm.mission_type",
                (email,),
            )
            rows = cur.fetchall()
        missions = {
            "status": "ready",
            "by_type": {str(int(row["mission_type"])): int(row["n"]) for row in rows},
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        missions = {"status": "unavailable", "by_type": None, "reason": type(exc).__name__}

    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ut.computer_technology FROM users u "
                "LEFT JOIN users_tech ut ON ut.user_id=u.id WHERE u.email=%s",
                (email,),
            )
            user = cur.fetchone()
        if not user:
            raise RuntimeError("account-not-found")
        maximum = 1 + max(0, int(user.get("computer_technology") or 0))
        by_type = missions.get("by_type")
        if not isinstance(by_type, Mapping):
            raise RuntimeError("mission-state-unavailable")
        active = sum(int(value) for value in by_type.values())
        fleet_slots = {
            "status": "ready",
            "available": True,
            "active": active,
            "maximum": maximum,
            "free": max(0, maximum - active),
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        fleet_slots = {
            "status": "unavailable",
            "available": False,
            "active": None,
            "maximum": None,
            "free": None,
            "reason": type(exc).__name__,
        }
    return missions, fleet_slots


def build_normalized_empire_state(email: str) -> dict[str, Any]:
    """Return serializable empire state with explicit unavailable observations.

    The result contains ``canonical_home_id``, normalized ``planets`` and an
    ``account`` section for research, missions, fleet slots, and ship totals.
    All reads are SELECT-only and no account name is assumed.
    """
    observed_at = int(time.time())
    try:
        source_planets = db_reader.list_planets_db(email)
    except Exception as exc:  # noqa: BLE001
        reason = f"planet-list:{type(exc).__name__}"
        return {
            "schema_version": 2,
            "status": "unavailable",
            "reason": reason,
            "observed_at": observed_at,
            "planet_count": None,
            "moon_count": None,
            "world_count": None,
            "canonical_home_id": None,
            "canonical_home_source": "unavailable",
            "planets": [],
            "moons": [],
            "worlds": [],
            "account": {
                "research_levels": None,
                "research_levels_status": {"status": "unavailable", "reason": reason},
                "research_queue": _unavailable_queue(reason),
                "missions": {"status": "unavailable", "by_type": None, "reason": reason},
                "fleet_slots": {
                    "status": "unavailable",
                    "available": False,
                    "active": None,
                    "maximum": None,
                    "free": None,
                    "reason": reason,
                },
                "ship_totals": None,
                "ship_totals_status": {"status": "unavailable", "reason": reason},
                "production_per_hour": {key: None for key in RESOURCE_KEYS},
            },
        }

    canonical_home_id, home_source = _select_canonical_home(source_planets)
    moon_list_reason = None
    try:
        raw_moons = moons.list_owned_moons(email)
        if isinstance(raw_moons, Mapping):
            raw_moons = raw_moons.get("moons", raw_moons.get("worlds", []))
        source_moons = [row for row in raw_moons if isinstance(row, Mapping)]
    except Exception as exc:  # noqa: BLE001 - planet state remains useful
        source_moons = []
        moon_list_reason = type(exc).__name__
    planet_ids = [
        pid for pid in (_int_or_none(row.get("id")) for row in source_planets) if pid is not None
    ]
    queue_states, research_queue = _read_queue_states(email, planet_ids, observed_at)
    missions, fleet_slots = _read_fleet_state(email)

    try:
        raw_ship_totals = db_reader.get_account_ship_totals_db(email)
        ship_totals = {
            name: max(0, int(raw_ship_totals.get(object_id, 0) or 0))
            for object_id, name in SHIPS.items()
        }
        ship_totals_status = {"status": "ready", "reason": None}
    except Exception as exc:  # noqa: BLE001
        ship_totals = None
        ship_totals_status = {"status": "unavailable", "reason": type(exc).__name__}

    normalized_planets: list[dict[str, Any]] = []
    research_levels: dict[str, int] | None = None
    production_total = {key: 0.0 for key in RESOURCE_KEYS}
    production_seen = {key: False for key in RESOURCE_KEYS}
    for source in source_planets:
        pid = _int_or_none(source.get("id"))
        identity = {
            "world_type": "planet",
            "world_id": pid,
            "moon_id": None,
            "paired_planet_id": None,
            "planet_id": pid,
            "name": str(source.get("name") or ""),
            "coordinates": _coordinate(source),
            "is_current": source.get("is_current") is True,
            "is_canonical_home": pid is not None and pid == canonical_home_id,
            "canonical_home_id": canonical_home_id,
        }
        queues = queue_states.get(
            pid,
            {
                "building": _unavailable_queue("planet-id-unavailable"),
                "unit": _unavailable_queue("planet-id-unavailable"),
            },
        )
        try:
            if pid is None:
                raise RuntimeError("planet-id-unavailable")
            levels = db_reader.get_levels_db(email, planet_id=pid)
            resource_state = db_reader.get_resource_state_db(email, planet_id=pid)
        except Exception as exc:  # noqa: BLE001 - preserve other planets
            normalized_planets.append({
                **identity,
                "status": "unavailable",
                "core_status": "unavailable",
                "reason": type(exc).__name__,
                "issues": [type(exc).__name__],
                "resources": {key: None for key in RESOURCE_KEYS},
                "capacities": {key: None for key in RESOURCE_KEYS},
                "energy": {
                    "balance": None,
                    "maximum": None,
                    "used": None,
                    "production_factor_pct": None,
                },
                "temperature": {"maximum": None},
                "levels": {"buildings": None, "facilities": None},
                "relevant_levels": None,
                "ships": None,
                "defense": None,
                "production_per_hour": {key: None for key in RESOURCE_KEYS},
                "production_status": {"status": "unavailable", "reason": type(exc).__name__},
                "queues": queues,
            })
            continue

        buildings = {
            name: max(0, int(levels.get(object_id, 0) or 0))
            for object_id, name in BUILDINGS.items()
        }
        facilities = {
            name: max(0, int(levels.get(object_id, 0) or 0))
            for object_id, name in FACILITIES.items()
        }
        local_ships = {
            name: max(0, int(levels.get(object_id, 0) or 0))
            for object_id, name in SHIPS.items()
        }
        local_defense = {
            name: max(0, int(levels.get(object_id, 0) or 0))
            for object_id, name in DEFENSES.items()
        }
        if research_levels is None:
            research_levels = {
                name: max(0, int(levels.get(object_id, 0) or 0))
                for object_id, name in RESEARCH.items()
            }

        try:
            raw_production = db_reader.get_production_per_hour_db(email, planet_id=pid)
            production = {
                key: float(raw_production.get(key, 0.0) or 0.0) for key in RESOURCE_KEYS
            }
            for key, value in production.items():
                production_total[key] += value
                production_seen[key] = True
            production_status = {"status": "ready", "reason": None}
        except Exception as exc:  # noqa: BLE001
            production = {key: None for key in RESOURCE_KEYS}
            production_status = {"status": "unavailable", "reason": type(exc).__name__}

        issues: list[str] = []
        if production_status["status"] != "ready":
            issues.append(f"production:{production_status['reason']}")
        for queue_name, queue in queues.items():
            if queue.get("available") is not True:
                issues.append(f"{queue_name}-queue:{queue.get('reason') or 'unavailable'}")
        normalized_planets.append({
            **identity,
            "status": "ready" if not issues else "partial",
            "core_status": "ready",
            "reason": None if not issues else "one-or-more-observations-unavailable",
            "issues": issues,
            "resources": {key: _number_or_none(resource_state.get(key)) for key in RESOURCE_KEYS},
            "capacities": {
                key: _number_or_none(resource_state.get(f"{key}_max")) for key in RESOURCE_KEYS
            },
            "energy": {
                "balance": _int_or_none(resource_state.get("energy")),
                "maximum": _int_or_none(resource_state.get("energy_max")),
                "used": _int_or_none(resource_state.get("energy_used")),
                "production_factor_pct": _int_or_none(resource_state.get("production_factor_pct")),
            },
            "temperature": {"maximum": _int_or_none(resource_state.get("temp_max"))},
            "levels": {"buildings": buildings, "facilities": facilities},
            "relevant_levels": {**buildings, **facilities},
            "ships": local_ships,
            "defense": local_defense,
            "production_per_hour": production,
            "production_status": production_status,
            "queues": queues,
        })

    planets_by_id = {
        pid: row
        for row in source_planets
        if (pid := _int_or_none(row.get("id"))) is not None
    }
    normalized_moons = [
        _normalize_moon_world(source, planets_by_id, observed_at, canonical_home_id)
        for source in source_moons
    ]
    normalized_moons.sort(key=lambda row: (row.get("moon_id") is None, row.get("moon_id") or 0))
    worlds = [*normalized_planets, *normalized_moons]

    issues = []
    if research_queue.get("available") is not True:
        issues.append("research-queue-unavailable")
    if fleet_slots.get("available") is not True:
        issues.append("fleet-slots-unavailable")
    if ship_totals is None:
        issues.append("ship-totals-unavailable")
    if research_levels is None:
        issues.append("research-levels-unavailable")
    if any(planet.get("status") != "ready" for planet in normalized_planets):
        issues.append("one-or-more-planets-partial")
    if moon_list_reason is not None:
        issues.append(f"moon-list:{moon_list_reason}")
    if any(moon.get("status") != "ready" for moon in normalized_moons):
        issues.append("one-or-more-moons-partial")
    return {
        "schema_version": 2,
        "status": "ready" if not issues else ("partial" if normalized_planets else "unavailable"),
        "reason": None if not issues else ";".join(issues),
        "observed_at": observed_at,
        "planet_count": len(source_planets),
        "moon_count": len(normalized_moons) if moon_list_reason is None else None,
        "world_count": len(worlds) if moon_list_reason is None else None,
        "canonical_home_id": canonical_home_id,
        "canonical_home_source": home_source,
        "planets": normalized_planets,
        "moons": normalized_moons,
        "worlds": worlds,
        "account": {
            "research_levels": research_levels,
            "research_levels_status": {
                "status": "ready" if research_levels is not None else "unavailable",
                "reason": None if research_levels is not None else "no-readable-planet",
            },
            "research_queue": research_queue,
            "missions": missions,
            "fleet_slots": fleet_slots,
            "ship_totals": ship_totals,
            "ship_totals_status": ship_totals_status,
            "production_per_hour": {
                key: round(production_total[key], 2) if production_seen[key] else None
                for key in RESOURCE_KEYS
            },
        },
    }


@dataclass(frozen=True)
class ReservationResult:
    """Serializable result of one atomic ledger reservation attempt."""

    accepted: bool
    command_id: str
    reason: str
    requested: dict[str, Any]
    deduplicated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the result as JSON-compatible primitives."""
        return {
            "accepted": self.accepted,
            "command_id": self.command_id,
            "reason": self.reason,
            "requested": self.requested,
            "deduplicated": self.deduplicated,
        }


class ReservationLedger:
    """Atomically reserve observed resources, slots, and queues in memory.

    Unknown planet state, resource balances, fleet slots, or queue occupancy
    rejects the whole command.  Reusing a command id with an identical request
    is idempotent; reusing it with different data is rejected.
    """

    def __init__(self, empire_state: Mapping[str, Any]):
        root = empire_state.get("normalized_state", empire_state)
        if not isinstance(root, Mapping):
            root = {}
        raw_planets = root.get("planets")
        planets = raw_planets if isinstance(raw_planets, list) else []
        self._resources: dict[int, dict[str, int | float | None]] = {}
        self._queues: dict[int, dict[str, dict[str, Any]]] = {}
        for planet in planets:
            if not isinstance(planet, Mapping):
                continue
            pid = _int_or_none(planet.get("planet_id", planet.get("id")))
            if pid is None or pid in self._resources:
                continue
            raw_resources = planet.get("resources")
            resources = raw_resources if isinstance(raw_resources, Mapping) else {}
            self._resources[pid] = {
                key: _number_or_none(resources.get(key)) for key in RESOURCE_KEYS
            }
            raw_queues = planet.get("queues")
            queues = raw_queues if isinstance(raw_queues, Mapping) else {}
            self._queues[pid] = {
                name: dict(queues.get(name)) if isinstance(queues.get(name), Mapping) else {}
                for name in ("building", "unit")
            }

        account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
        fleet = account.get("fleet_slots") if isinstance(account.get("fleet_slots"), Mapping) else {}
        free = _int_or_none(fleet.get("free"))
        self._fleet_available = fleet.get("available") is True and free is not None and free >= 0
        self._fleet_free = free if self._fleet_available else None
        research = account.get("research_queue")
        self._research_queue = dict(research) if isinstance(research, Mapping) else {}

        self._resource_reserved = {
            pid: {key: 0 for key in RESOURCE_KEYS} for pid in self._resources
        }
        self._fleet_reserved = 0
        self._research_reserved_by: str | None = None
        self._building_reserved_by: dict[int, str] = {}
        self._unit_reserved_by: dict[int, str] = {}
        self._commands: dict[str, tuple[tuple[Any, ...], ReservationResult]] = {}
        self._rejections: list[ReservationResult] = []

    @staticmethod
    def _request_signature(request: Mapping[str, Any]) -> tuple[Any, ...]:
        resources = request["resources"]
        return (
            request["planet_id"],
            tuple((key, resources[key]) for key in RESOURCE_KEYS),
            request["fleet_slots"],
            request["research_queue"],
            request["building_queue"],
            request["unit_queue"],
        )

    def _reject(
        self,
        command_id: str,
        reason: str,
        request: dict[str, Any],
        signature: tuple[Any, ...] | None = None,
    ) -> ReservationResult:
        result = ReservationResult(False, command_id, reason, request)
        self._rejections.append(result)
        if command_id and signature is not None and command_id not in self._commands:
            self._commands[command_id] = (signature, result)
        return result

    def reserve(
        self,
        command_id: str,
        *,
        planet_id: int | None = None,
        resources: Mapping[str, int | float] | None = None,
        fleet_slots: int = 0,
        research_queue: bool = False,
        building_queue: bool = False,
        unit_queue: bool = False,
    ) -> ReservationResult:
        """Atomically reserve one command and return a documented result.

        ``resources`` is a metal/crystal/deuterium cost on ``planet_id``.
        Queue booleans reserve the corresponding exclusive scheduling token.
        """
        if not isinstance(command_id, str) or not command_id.strip():
            return self._reject(str(command_id or ""), "invalid-command-id", {})
        command_id = command_id.strip()
        pid = _int_or_none(planet_id)
        if planet_id is not None and pid is None:
            return self._reject(command_id, "invalid-planet-id", {})
        if isinstance(fleet_slots, bool) or _int_or_none(fleet_slots) is None or int(fleet_slots) < 0:
            return self._reject(command_id, "invalid-fleet-slot-count", {})
        if not all(isinstance(flag, bool) for flag in (research_queue, building_queue, unit_queue)):
            return self._reject(command_id, "invalid-queue-reservation", {})

        costs: dict[str, int | float] = {key: 0 for key in RESOURCE_KEYS}
        if resources is not None:
            if not isinstance(resources, Mapping) or any(key not in RESOURCE_KEYS for key in resources):
                return self._reject(command_id, "invalid-resource-cost", {})
            for key in RESOURCE_KEYS:
                value = resources.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    return self._reject(command_id, "invalid-resource-cost", {})
                costs[key] = value
        request = {
            "planet_id": pid,
            "resources": costs,
            "fleet_slots": int(fleet_slots),
            "research_queue": research_queue,
            "building_queue": building_queue,
            "unit_queue": unit_queue,
        }
        signature = self._request_signature(request)
        previous = self._commands.get(command_id)
        if previous is not None:
            old_signature, old_result = previous
            if old_signature != signature:
                return self._reject(command_id, "command-id-conflict", request)
            return ReservationResult(
                old_result.accepted,
                old_result.command_id,
                old_result.reason,
                old_result.requested,
                deduplicated=True,
            )

        needs_planet = any(costs.values()) or building_queue or unit_queue
        if needs_planet and pid is None:
            return self._reject(command_id, "planet-id-required", request, signature)
        if needs_planet and pid not in self._resources:
            return self._reject(command_id, "planet-state-unavailable", request, signature)
        if not needs_planet and not fleet_slots and not research_queue:
            return self._reject(command_id, "empty-reservation", request, signature)

        if any(costs.values()):
            assert pid is not None
            for key, cost in costs.items():
                available = self._resources[pid][key]
                if available is None:
                    return self._reject(command_id, f"resource-unavailable:{key}", request, signature)
                remaining = available - self._resource_reserved[pid][key]
                if cost > remaining:
                    return self._reject(command_id, f"insufficient-resource:{key}", request, signature)

        if fleet_slots:
            if not self._fleet_available or self._fleet_free is None:
                return self._reject(command_id, "fleet-slots-unavailable", request, signature)
            if self._fleet_reserved + fleet_slots > self._fleet_free:
                return self._reject(command_id, "insufficient-fleet-slots", request, signature)

        if research_queue:
            if self._research_queue.get("available") is not True:
                return self._reject(command_id, "research-queue-unavailable", request, signature)
            if self._research_queue.get("occupied") is not False:
                return self._reject(command_id, "research-queue-busy", request, signature)
            if self._research_reserved_by is not None:
                return self._reject(command_id, "research-queue-reserved", request, signature)

        for queue_name, requested, reservations in (
            ("building", building_queue, self._building_reserved_by),
            ("unit", unit_queue, self._unit_reserved_by),
        ):
            if not requested:
                continue
            assert pid is not None
            queue = self._queues.get(pid, {}).get(queue_name, {})
            if queue.get("available") is not True:
                return self._reject(command_id, f"{queue_name}-queue-unavailable", request, signature)
            if queue.get("occupied") is not False:
                return self._reject(command_id, f"{queue_name}-queue-busy", request, signature)
            if pid in reservations:
                return self._reject(command_id, f"{queue_name}-queue-reserved", request, signature)

        # Commit only after every requested dimension has passed validation.
        if any(costs.values()):
            assert pid is not None
            for key, cost in costs.items():
                self._resource_reserved[pid][key] += cost
        self._fleet_reserved += int(fleet_slots)
        if research_queue:
            self._research_reserved_by = command_id
        if building_queue:
            assert pid is not None
            self._building_reserved_by[pid] = command_id
        if unit_queue:
            assert pid is not None
            self._unit_reserved_by[pid] = command_id
        result = ReservationResult(True, command_id, "reserved", request)
        self._commands[command_id] = (signature, result)
        return result

    def rejection_reasons(self) -> list[dict[str, Any]]:
        """Return all rejected attempts as JSON-compatible dictionaries."""
        return [result.to_dict() for result in self._rejections]

    def summary(self) -> dict[str, Any]:
        """Return reservations, remaining capacity, commands, and reason counts."""
        resources: dict[str, Any] = {}
        for pid, observed in sorted(self._resources.items()):
            reserved = self._resource_reserved[pid]
            resources[str(pid)] = {
                "observed": observed,
                "reserved": dict(reserved),
                "remaining": {
                    key: observed[key] - reserved[key] if observed[key] is not None else None
                    for key in RESOURCE_KEYS
                },
            }
        commands = [result.to_dict() for _signature, result in self._commands.values()]
        reason_counts = Counter(result.reason for result in self._rejections)
        return {
            "status": "ready" if not self._rejections else "ready-with-rejections",
            "resources_by_planet": resources,
            "fleet_slots": {
                "available": self._fleet_available,
                "observed_free": self._fleet_free,
                "reserved": self._fleet_reserved,
                "remaining": (
                    self._fleet_free - self._fleet_reserved if self._fleet_free is not None else None
                ),
            },
            "research_queue_reserved_by": self._research_reserved_by,
            "building_queue_reserved_by": {
                str(pid): command for pid, command in sorted(self._building_reserved_by.items())
            },
            "unit_queue_reserved_by": {
                str(pid): command for pid, command in sorted(self._unit_reserved_by.items())
            },
            "commands": commands,
            "rejections": self.rejection_reasons(),
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
        }


def _planet_level(planet: Mapping[str, Any], name: str) -> int | None:
    relevant = planet.get("relevant_levels")
    if isinstance(relevant, Mapping):
        return _int_or_none(relevant.get(name))
    levels = planet.get("levels")
    if isinstance(levels, Mapping):
        for section in ("buildings", "facilities"):
            values = levels.get(section)
            if isinstance(values, Mapping) and name in values:
                return _int_or_none(values.get(name))
    return None


def score_planet_development(
    planets: Iterable[Mapping[str, Any]],
    minimum_floors: Mapping[str, int],
    *,
    roles: Mapping[int, str] | None = None,
    role_targets: Mapping[str, Mapping[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Rank level gaps, always placing balanced floors before specialization.

    Roles and their targets are caller-defined; the output is a deterministic,
    descending list of serializable score records and performs no live action.
    """
    role_map = roles or {}
    targets_by_role = role_targets or {}
    scored: list[dict[str, Any]] = []
    for planet in planets:
        pid = _int_or_none(planet.get("planet_id", planet.get("id")))
        if pid is None:
            continue
        role = role_map.get(pid, str(planet.get("role"))) if planet.get("role") is not None else role_map.get(pid)
        specialization = targets_by_role.get(role, {}) if role is not None else {}
        names = sorted(set(minimum_floors) | set(specialization))
        for name in names:
            floor = max(0, _int_or_none(minimum_floors.get(name, 0)) or 0)
            role_target = max(floor, _int_or_none(specialization.get(name, floor)) or floor)
            current = _planet_level(planet, name)
            if current is None:
                scored.append({
                    "planet_id": pid,
                    "object_name": name,
                    "role": role,
                    "current_level": None,
                    "minimum_floor": floor,
                    "role_target": role_target,
                    "floor_gap": None,
                    "specialization_gap": None,
                    "priority_tier": "unavailable",
                    "score": None,
                    "reason": "level-unavailable",
                })
                continue
            floor_gap = max(0, floor - current)
            role_gap = max(0, role_target - max(current, floor))
            if floor_gap <= 0 and role_gap <= 0:
                continue
            tier = "balanced-floor" if floor_gap > 0 else "role-specialization"
            score = (
                2.0 + floor_gap / max(1, floor)
                if floor_gap > 0
                else 1.0 + role_gap / max(1, role_target)
            )
            queue = planet.get("queues") if isinstance(planet.get("queues"), Mapping) else {}
            building = queue.get("building") if isinstance(queue.get("building"), Mapping) else {}
            scored.append({
                "planet_id": pid,
                "object_name": name,
                "role": role,
                "current_level": current,
                "minimum_floor": floor,
                "role_target": role_target,
                "floor_gap": floor_gap,
                "specialization_gap": role_gap,
                "priority_tier": tier,
                "score": round(score, 6),
                "queue_remaining_seconds": _int_or_none(building.get("remaining_seconds")),
                "reason": None,
            })
    return sorted(
        scored,
        key=lambda row: (
            row["score"] is None,
            -(row["score"] or 0),
            row.get("queue_remaining_seconds") is None,
            row.get("queue_remaining_seconds") or 0,
            row["planet_id"],
            row["object_name"],
        ),
    )


def allocate_production_by_completion(
    amount: int,
    planet_options: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Allocate units to minimize projected final completion time.

    Each option requires ``planet_id``, nonnegative ``queue_remaining_seconds``,
    positive ``seconds_per_unit``, and optionally nonnegative ``max_amount``.
    Invalid or unavailable options are reported and never treated as idle.
    """
    requested = _int_or_none(amount)
    if requested is None or requested < 0 or isinstance(amount, bool):
        return {"status": "unavailable", "reason": "invalid-amount", "requested_amount": amount, "allocated_amount": 0, "allocations": [], "rejected_options": []}
    if requested == 0:
        return {"status": "ready", "reason": None, "requested_amount": 0, "allocated_amount": 0, "projected_completion_seconds": 0, "allocations": [], "rejected_options": []}

    options: list[dict[str, int | None]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in planet_options:
        pid = _int_or_none(raw.get("planet_id"))
        queue = _int_or_none(raw.get("queue_remaining_seconds"))
        per_unit = _int_or_none(raw.get("seconds_per_unit"))
        cap_raw = raw.get("max_amount")
        cap = None if cap_raw is None else _int_or_none(cap_raw)
        reason = None
        if raw.get("available") is False:
            reason = "option-unavailable"
        elif pid is None:
            reason = "planet-id-unavailable"
        elif pid in seen:
            reason = "duplicate-planet-option"
        elif queue is None or queue < 0:
            reason = "queue-time-unavailable"
        elif per_unit is None or per_unit <= 0:
            reason = "production-time-unavailable"
        elif cap_raw is not None and (cap is None or cap < 0):
            reason = "invalid-max-amount"
        if reason is not None:
            rejected.append({"planet_id": pid, "reason": reason})
            continue
        assert pid is not None and queue is not None and per_unit is not None
        seen.add(pid)
        if cap == 0:
            rejected.append({"planet_id": pid, "reason": "zero-capacity"})
            continue
        options.append({
            "planet_id": pid,
            "queue_remaining_seconds": queue,
            "seconds_per_unit": per_unit,
            "max_amount": cap,
        })
    if not options:
        return {"status": "unavailable", "reason": "no-eligible-production-option", "requested_amount": requested, "allocated_amount": 0, "projected_completion_seconds": None, "allocations": [], "rejected_options": rejected}

    finite_total = sum(int(option["max_amount"] or 0) for option in options if option["max_amount"] is not None)
    has_unbounded = any(option["max_amount"] is None for option in options)
    target = requested if has_unbounded else min(requested, finite_total)
    if target <= 0:
        return {"status": "unavailable", "reason": "no-production-capacity", "requested_amount": requested, "allocated_amount": 0, "projected_completion_seconds": None, "allocations": [], "rejected_options": rejected}

    def capacity(deadline: int, option: Mapping[str, int | None]) -> int:
        queue = int(option["queue_remaining_seconds"] or 0)
        per_unit = int(option["seconds_per_unit"] or 1)
        count = max(0, (deadline - queue) // per_unit)
        cap = option["max_amount"]
        return min(count, int(cap)) if cap is not None else count

    high = max(
        int(option["queue_remaining_seconds"] or 0)
        + int(option["seconds_per_unit"] or 1)
        * min(target, int(option["max_amount"]) if option["max_amount"] is not None else target)
        for option in options
    )
    low = 0
    while low < high:
        middle = (low + high) // 2
        if sum(capacity(middle, option) for option in options) >= target:
            high = middle
        else:
            low = middle + 1
    deadline = low

    remaining = target
    allocations: list[dict[str, Any]] = []
    ordered = sorted(
        options,
        key=lambda option: (
            int(option["queue_remaining_seconds"] or 0) + int(option["seconds_per_unit"] or 1),
            int(option["planet_id"] or 0),
        ),
    )
    for option in ordered:
        assigned = min(remaining, capacity(deadline, option))
        if assigned <= 0:
            continue
        finish = int(option["queue_remaining_seconds"] or 0) + assigned * int(option["seconds_per_unit"] or 1)
        allocations.append({
            "planet_id": option["planet_id"],
            "amount": assigned,
            "queue_remaining_seconds": option["queue_remaining_seconds"],
            "seconds_per_unit": option["seconds_per_unit"],
            "projected_finish_seconds": finish,
        })
        remaining -= assigned
        if remaining <= 0:
            break
    status = "ready" if target == requested else "partial"
    return {
        "status": status,
        "reason": None if status == "ready" else "insufficient-production-capacity",
        "requested_amount": requested,
        "allocated_amount": target - remaining,
        "unallocated_amount": requested - (target - remaining),
        "projected_completion_seconds": max((row["projected_finish_seconds"] for row in allocations), default=None),
        "allocations": allocations,
        "rejected_options": rejected,
    }


def colonization_roi_inputs(
    empire_state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    upfront_cost: Mapping[str, int | float] | None = None,
    expected_production_per_hour: Mapping[str, int | float] | None = None,
    resource_weights: Mapping[str, int | float] | None = None,
) -> dict[str, Any]:
    """Build serializable colonization capacity, distance, cost, and payback inputs.

    Costs and expected production are caller-supplied or read from the candidate;
    missing values stay unavailable rather than receiving optimistic defaults.
    """
    root = empire_state.get("normalized_state", empire_state)
    if not isinstance(root, Mapping):
        root = {}
    planets = root.get("planets") if isinstance(root.get("planets"), list) else []
    account = root.get("account") if isinstance(root.get("account"), Mapping) else {}
    canonical_home_id = _int_or_none(root.get("canonical_home_id"))
    home = next(
        (planet for planet in planets if _int_or_none(planet.get("planet_id")) == canonical_home_id),
        None,
    )
    candidate_coord_source = candidate.get("coordinates")
    candidate_coord = _coordinate(
        candidate_coord_source if isinstance(candidate_coord_source, Mapping) else candidate
    )
    home_coord = home.get("coordinates") if isinstance(home, Mapping) else None
    if not isinstance(home_coord, Mapping):
        home_coord = {}

    research = account.get("research_levels") if isinstance(account.get("research_levels"), Mapping) else {}
    astro = _int_or_none(research.get(name_of(ASTROPHYSICS)))
    theoretical_capacity = 1 + ((astro + 1) // 2) if astro is not None else None
    fleet = account.get("fleet_slots") if isinstance(account.get("fleet_slots"), Mapping) else {}

    cost_source = upfront_cost if upfront_cost is not None else candidate.get("upfront_cost")
    production_source = (
        expected_production_per_hour
        if expected_production_per_hour is not None
        else candidate.get("expected_production_per_hour")
    )
    weights_source = resource_weights or {key: 1.0 for key in RESOURCE_KEYS}

    def vector(source: Any) -> dict[str, int | float | None]:
        values = source if isinstance(source, Mapping) else {}
        return {key: _number_or_none(values.get(key)) for key in RESOURCE_KEYS}

    costs = vector(cost_source)
    production = vector(production_source)
    weights = vector(weights_source)
    weighted_cost = (
        sum(float(costs[key]) * float(weights[key]) for key in RESOURCE_KEYS)
        if all(costs[key] is not None and weights[key] is not None for key in RESOURCE_KEYS)
        else None
    )
    weighted_hourly = (
        sum(float(production[key]) * float(weights[key]) for key in RESOURCE_KEYS)
        if all(production[key] is not None and weights[key] is not None for key in RESOURCE_KEYS)
        else None
    )
    payback = (
        weighted_cost / weighted_hourly
        if weighted_cost is not None and weighted_hourly is not None and weighted_hourly > 0
        else None
    )

    ships = account.get("ship_totals") if isinstance(account.get("ship_totals"), Mapping) else {}
    colony_ships = _int_or_none(ships.get("colony_ship"))
    deltas = {}
    for key in ("galaxy", "system", "position"):
        origin = _int_or_none(home_coord.get(key))
        destination = _int_or_none(candidate_coord.get(key))
        deltas[key] = abs(destination - origin) if origin is not None and destination is not None else None

    missing: list[str] = []
    for condition, reason in (
        (canonical_home_id is None, "canonical-home-unavailable"),
        (candidate_coord.get("label") is None, "candidate-coordinates-unavailable"),
        (astro is None, "astrophysics-unavailable"),
        (fleet.get("available") is not True, "fleet-slots-unavailable"),
        (colony_ships is None, "colony-ship-total-unavailable"),
        (weighted_cost is None, "upfront-cost-unavailable"),
        (weighted_hourly is None, "expected-production-unavailable"),
    ):
        if condition:
            missing.append(reason)
    return {
        "status": "ready" if not missing else "partial",
        "missing": missing,
        "candidate_coordinates": candidate_coord,
        "canonical_home_id": canonical_home_id,
        "canonical_home_coordinates": dict(home_coord),
        "coordinate_deltas": deltas,
        "planet_count": len(planets),
        "astrophysics_level": astro,
        "theoretical_planet_capacity": theoretical_capacity,
        "open_planet_slots": (
            max(0, theoretical_capacity - len(planets)) if theoretical_capacity is not None else None
        ),
        "fleet_slots_free": _int_or_none(fleet.get("free")) if fleet.get("available") is True else None,
        "colony_ships_account_total": colony_ships,
        "upfront_cost": costs,
        "expected_production_per_hour": production,
        "resource_weights": weights,
        "weighted_upfront_cost": weighted_cost,
        "weighted_production_per_hour": weighted_hourly,
        "simple_payback_hours": round(payback, 2) if payback is not None else None,
        "candidate_temperature_max": _int_or_none(candidate.get("temp_max")),
        "candidate_fields": _int_or_none(candidate.get("fields")),
    }
