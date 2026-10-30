"""Fail-closed economic resource transfers between owned planets.

The dispatcher deliberately has a narrow contract.  A command is durable before any
HTTP dispatch, only cargo ships are selected, and an uncertain dispatch is never
retried.  Callers should retain ``command_id`` across retries so the ledger can
reconcile the command against an exact active transport mission.

Accepted command schema::

    {
        "command_id": "bootstrap-colony-42",
        "source_planet_id": 1001,
        "destination_planet_id": 1002,  # xor destination_coordinates
        "resources": {"metal": 50000, "crystal": 25000, "deuterium": 0},
        "reserve_floors": {"metal": 10000, "crystal": 10000, "deuterium": 5000},
        "purpose": "bootstrap new colony",
        "speed": 5,
        "deadline": 1780000000,          # optional Unix timestamp
    }

``destination_coordinates`` must be exactly
``{"galaxy": int, "system": int, "position": int}`` and must resolve to an
owned planet.  Unknown fields and boolean-as-integer values are rejected.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import threading
import time
from typing import Any

from bots.rule_based import explorer, fleetsave
from bots.shared import db_reader, fleet, planets


RESOURCE_NAMES = ("metal", "crystal", "deuterium")
TRANSFER_STATES = frozenset(
    {"planned", "sending", "sent", "confirmed", "rejected", "ambiguous"}
)

STATE_DIR = pathlib.Path(__file__).resolve().parent / ".resource_transfer_state"
LEDGER_VERSION = 1
MAX_LEDGER_BYTES = 16 * 1024 * 1024

SMALL_CARGO = fleet.SHIP_SMALL_CARGO
LARGE_CARGO = fleet.SHIP_LARGE_CARGO
_CARGO_CAPACITY = {SMALL_CARGO: 5_000, LARGE_CARGO: 25_000}
# Small cargo can change drive after research.  These values intentionally use a
# conservative upper consumption rather than trying to predict the selected drive.
_CARGO_FUEL = {SMALL_CARGO: 20, LARGE_CARGO: 50}

MAX_RESOURCE_AMOUNT = 2**63 - 1
MAX_PLANET_ID = 2**63 - 1
MAX_GALAXY = 99
MAX_SYSTEM = 499
MAX_POSITION = 15
MAX_CARGO_SHIPS = 1_000_000
MAX_DEADLINE = 253_402_300_799  # 9999-12-31 23:59:59 UTC

_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class _ValidationError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _PreflightRejection(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _LedgerError(RuntimeError):
    pass


def _reason(code: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if details:
        result["details"] = details
    return result


def _outcome(
    command_id: str | None,
    state: str,
    *,
    ok: bool = False,
    reason: dict[str, Any] | None = None,
    dispatch: dict[str, Any] | None = None,
    retry_safe: bool = False,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": bool(ok),
        "command_id": command_id,
        "state": state,
        "reason": reason,
        "retry_safe": bool(retry_safe),
        "ai_feedback": reason["message"] if reason else f"transfer {state}",
    }
    if dispatch is not None:
        result["dispatch"] = dispatch
    if warnings:
        result["warnings"] = warnings
    return result


def _strict_int(value: Any, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _ValidationError(
            "invalid_integer",
            f"{field} must be an integer in [{low}, {high}]",
            field=field,
        )
    return value


def _resource_map(value: Any, field: str) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(RESOURCE_NAMES):
        raise _ValidationError(
            "invalid_resource_map",
            f"{field} must contain exactly metal, crystal, and deuterium",
            field=field,
        )
    return {
        name: _strict_int(value[name], f"{field}.{name}", 0, MAX_RESOURCE_AMOUNT)
        for name in RESOURCE_NAMES
    }


def _coordinates(value: Any) -> dict[str, int]:
    keys = {"galaxy", "system", "position"}
    if type(value) is not dict or set(value) != keys:
        raise _ValidationError(
            "invalid_coordinates",
            "destination_coordinates must contain exactly galaxy, system, and position",
        )
    return {
        "galaxy": _strict_int(value["galaxy"], "destination_coordinates.galaxy", 1, MAX_GALAXY),
        "system": _strict_int(value["system"], "destination_coordinates.system", 1, MAX_SYSTEM),
        "position": _strict_int(
            value["position"], "destination_coordinates.position", 1, MAX_POSITION
        ),
    }


def _validate_text(value: Any, field: str, maximum: int) -> str:
    if type(value) is not str:
        raise _ValidationError("invalid_text", f"{field} must be a string", field=field)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(ch) < 32 for ch in normalized):
        raise _ValidationError(
            "invalid_text",
            f"{field} must be non-empty, printable, and at most {maximum} characters",
            field=field,
        )
    return normalized


def _validate_speed(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ValidationError("invalid_speed", "speed must be a number from 1 to 10")
    speed = float(value)
    if not math.isfinite(speed) or speed < 1 or speed > 10 or speed * 2 != int(speed * 2):
        raise _ValidationError(
            "invalid_speed", "speed must be from 1 to 10 in increments of 0.5"
        )
    return int(speed) if speed.is_integer() else speed


def _validate_plan(plan: Any) -> dict[str, Any]:
    if type(plan) is not dict:
        raise _ValidationError("invalid_plan", "transfer plan must be a plain object")

    destination_fields = {
        name for name in ("destination_planet_id", "destination_coordinates") if name in plan
    }
    if len(destination_fields) != 1:
        raise _ValidationError(
            "invalid_destination_selector",
            "specify exactly one of destination_planet_id or destination_coordinates",
        )

    required = {
        "command_id",
        "source_planet_id",
        "resources",
        "reserve_floors",
        "purpose",
        "speed",
    }
    allowed = required | destination_fields | {"deadline"}
    missing = required - set(plan)
    unknown = set(plan) - allowed
    if missing or unknown:
        raise _ValidationError(
            "invalid_schema_fields",
            "transfer plan has missing or unknown fields",
            missing=sorted(str(item) for item in missing),
            unknown=sorted(str(item) for item in unknown),
        )

    command_id = _validate_text(plan["command_id"], "command_id", 128)
    if not _COMMAND_ID_RE.fullmatch(command_id):
        raise _ValidationError(
            "invalid_command_id",
            "command_id must use only letters, digits, dot, underscore, colon, or hyphen",
        )

    resources = _resource_map(plan["resources"], "resources")
    if sum(resources.values()) <= 0:
        raise _ValidationError("empty_transfer", "at least one transferred resource must be positive")

    normalized: dict[str, Any] = {
        "command_id": command_id,
        "source_planet_id": _strict_int(
            plan["source_planet_id"], "source_planet_id", 1, MAX_PLANET_ID
        ),
        "resources": resources,
        "reserve_floors": _resource_map(plan["reserve_floors"], "reserve_floors"),
        "purpose": _validate_text(plan["purpose"], "purpose", 160),
        "speed": _validate_speed(plan["speed"]),
    }
    if "destination_planet_id" in destination_fields:
        normalized["destination_planet_id"] = _strict_int(
            plan["destination_planet_id"], "destination_planet_id", 1, MAX_PLANET_ID
        )
    else:
        normalized["destination_coordinates"] = _coordinates(
            plan["destination_coordinates"]
        )

    if "deadline" in plan:
        normalized["deadline"] = _strict_int(plan["deadline"], "deadline", 1, MAX_DEADLINE)
    return normalized


def _validate_email(email: Any) -> str:
    return _validate_text(email, "email", 320)


def _validate_execute_options(
    minimum_cargo_reserve: Any,
    maximum_cargo_ships: Any,
    emergency_slot_reserve: Any,
    colonization_slot_reservation: Any,
    fuel_safety_factor: Any,
) -> tuple[int, int, int, int, float]:
    minimum = _strict_int(
        minimum_cargo_reserve, "minimum_cargo_reserve", 0, MAX_CARGO_SHIPS
    )
    maximum = _strict_int(maximum_cargo_ships, "maximum_cargo_ships", 1, MAX_CARGO_SHIPS)
    emergency = _strict_int(
        emergency_slot_reserve, "emergency_slot_reserve", 1, MAX_CARGO_SHIPS
    )
    colonization = _strict_int(
        colonization_slot_reservation,
        "colonization_slot_reservation",
        0,
        MAX_CARGO_SHIPS,
    )
    if isinstance(fuel_safety_factor, bool) or not isinstance(fuel_safety_factor, (int, float)):
        raise _ValidationError(
            "invalid_fuel_safety_factor", "fuel_safety_factor must be a number from 1 to 10"
        )
    fuel_factor = float(fuel_safety_factor)
    if not math.isfinite(fuel_factor) or not 1 <= fuel_factor <= 10:
        raise _ValidationError(
            "invalid_fuel_safety_factor", "fuel_safety_factor must be a number from 1 to 10"
        )
    # Even a configured zero may not permit consuming every cargo ship.
    return max(1, minimum), maximum, emergency, colonization, fuel_factor


def _ledger_path(email: str, ledger_key: str | None) -> pathlib.Path:
    identity = email if ledger_key is None else _validate_text(ledger_key, "ledger_key", 320)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in identity)[:48]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"{safe or 'account'}-{digest}.json"


def _thread_lock(path: pathlib.Path) -> threading.RLock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _serialized(path: pathlib.Path):
    lock = _thread_lock(path)
    with lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise _LedgerError(f"cannot open transfer lock: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise _LedgerError(f"transfer lock failed: {exc}") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _empty_ledger() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "commands": {}, "updated_at": int(time.time())}


def _load_ledger(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_ledger()
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            raise _LedgerError("transfer ledger exceeds size limit")
        data = json.loads(path.read_text(encoding="utf-8"))
    except _LedgerError:
        raise
    except (OSError, ValueError) as exc:
        raise _LedgerError(f"transfer ledger is unreadable: {exc}") from exc
    if type(data) is not dict or data.get("version") != LEDGER_VERSION:
        raise _LedgerError("transfer ledger has an unsupported shape or version")
    commands = data.get("commands")
    if type(commands) is not dict:
        raise _LedgerError("transfer ledger commands must be an object")
    for command_id, record in commands.items():
        if (
            type(command_id) is not str
            or type(record) is not dict
            or record.get("command_id") != command_id
            or record.get("state") not in TRANSFER_STATES
            or type(record.get("plan")) is not dict
        ):
            raise _LedgerError("transfer ledger contains a malformed command record")
    return data


def _atomic_write_ledger(path: pathlib.Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = int(time.time())
    try:
        payload = json.dumps(
            ledger, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise _LedgerError(f"transfer ledger is not serializable: {exc}") from exc

    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise _LedgerError(f"atomic transfer ledger write failed: {exc}") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _new_record(plan: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    return {
        "command_id": plan["command_id"],
        "state": "planned",
        "plan": plan,
        "created_at": now,
        "updated_at": now,
        "history": [{"state": "planned", "at": now}],
    }


def _transition(
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    state: str,
    **fields: Any,
) -> None:
    if state not in TRANSFER_STATES:
        raise _LedgerError(f"invalid transfer state {state!r}")
    now = int(time.time())
    record.update(fields)
    record["state"] = state
    record["updated_at"] = now
    history = record.setdefault("history", [])
    history.append({"state": state, "at": now})
    ledger["commands"][record["command_id"]] = record
    _atomic_write_ledger(path, ledger)


def _record_dispatch(record: dict[str, Any]) -> dict[str, Any] | None:
    dispatch = record.get("dispatch")
    return dispatch if type(dispatch) is dict else None


def _record_outcome(record: dict[str, Any], *, reason: dict[str, Any] | None = None) -> dict[str, Any]:
    state = record["state"]
    stored_reason = record.get("reason")
    if reason is None and type(stored_reason) is dict:
        reason = stored_reason
    return _outcome(
        record["command_id"],
        state,
        ok=state in {"sent", "confirmed"},
        reason=reason,
        dispatch=_record_dispatch(record),
        retry_safe=state == "planned",
    )


def _owned_planets(client) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, int, int], dict[str, Any]]]:
    rows = planets.list_planets(client)
    if not rows:
        raise _PreflightRejection(
            "ownership_unavailable", "owned planet list could not be established"
        )
    by_id: dict[int, dict[str, Any]] = {}
    by_coord: dict[tuple[int, int, int], dict[str, Any]] = {}
    try:
        for raw in rows:
            planet_id = int(raw["id"])
            coord = (int(raw["galaxy"]), int(raw["system"]), int(raw["position"]))
            if planet_id <= 0 or planet_id in by_id or coord in by_coord:
                raise ValueError("duplicate or invalid owned planet")
            row = {
                "id": planet_id,
                "name": str(raw.get("name") or ""),
                "galaxy": coord[0],
                "system": coord[1],
                "position": coord[2],
                "coord": coord,
            }
            by_id[planet_id] = row
            by_coord[coord] = row
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _PreflightRejection(
            "ownership_unavailable", "owned planet list was malformed"
        ) from exc
    return by_id, by_coord


def _resolve_owned_route(
    plan: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    by_coord: dict[tuple[int, int, int], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = by_id.get(plan["source_planet_id"])
    if source is None:
        raise _PreflightRejection(
            "source_not_owned",
            "source_planet_id is not in the live owned planet list",
            source_planet_id=plan["source_planet_id"],
        )
    if "destination_planet_id" in plan:
        destination = by_id.get(plan["destination_planet_id"])
    else:
        coords = plan["destination_coordinates"]
        destination = by_coord.get((coords["galaxy"], coords["system"], coords["position"]))
    if destination is None:
        raise _PreflightRejection(
            "destination_not_owned", "destination does not exactly match an owned planet"
        )
    if source["id"] == destination["id"]:
        raise _PreflightRejection(
            "same_planet", "source and destination must be different owned planets"
        )
    return source, destination


def _resource_snapshot(email: str, planet_id: int) -> dict[str, int]:
    try:
        raw = db_reader.get_status_db(email, planet_id=planet_id)
        result = {name: int(raw[name]) for name in RESOURCE_NAMES}
    except Exception as exc:  # noqa: BLE001 - economic actions fail closed on DB uncertainty
        raise _PreflightRejection(
            "source_resources_unavailable", "source resources could not be established"
        ) from exc
    if any(value < 0 for value in result.values()):
        raise _PreflightRejection(
            "source_resources_unavailable", "source resource snapshot contained invalid values"
        )
    return result


def _source_levels(email: str, planet_id: int) -> dict[int, int]:
    try:
        levels = db_reader.get_levels_db(email, planet_id=planet_id)
        small = int(levels.get(SMALL_CARGO, 0))
        large = int(levels.get(LARGE_CARGO, 0))
    except Exception as exc:  # noqa: BLE001
        raise _PreflightRejection(
            "source_fleet_unavailable", "source cargo inventory could not be established"
        ) from exc
    if small < 0 or large < 0:
        raise _PreflightRejection(
            "source_fleet_unavailable", "source cargo inventory contained invalid values"
        )
    return levels


def _distance(source: dict[str, Any], destination: dict[str, Any]) -> int:
    galaxy_delta = abs(source["galaxy"] - destination["galaxy"])
    if galaxy_delta:
        return galaxy_delta * 20_000
    direct_systems = abs(source["system"] - destination["system"])
    system_delta = min(direct_systems, MAX_SYSTEM - direct_systems)
    if system_delta:
        return system_delta * 5 * 19 + 2_700
    position_delta = abs(source["position"] - destination["position"])
    return position_delta * 5 + 1_000 if position_delta else 5


def _fuel_per_ship(
    technology_id: int, distance: int, speed: int | float, safety_factor: float
) -> int:
    # This is an intentional upper envelope: maximum base consumption, no drive
    # efficiency discount, and the largest speed multiplier used by the dispatcher.
    multiplier = (float(speed) + 1.0) ** 2
    estimate = _CARGO_FUEL[technology_id] * distance * multiplier / 35_000
    return max(1, int(math.ceil(estimate * safety_factor)))


def _select_cargo(
    levels: dict[int, int],
    payload: int,
    source: dict[str, Any],
    destination: dict[str, Any],
    speed: int | float,
    minimum_reserve: int,
    maximum_ships: int,
    fuel_safety_factor: float,
) -> tuple[dict[int, int], int, int]:
    available = {
        SMALL_CARGO: max(0, int(levels.get(SMALL_CARGO, 0))),
        LARGE_CARGO: max(0, int(levels.get(LARGE_CARGO, 0))),
    }
    total_available = sum(available.values())
    dispatchable = min(maximum_ships, max(0, total_available - minimum_reserve))
    if dispatchable <= 0:
        raise _PreflightRejection(
            "cargo_reserve_violation",
            "no cargo ship can be sent while preserving the source cargo reserve",
            available=total_available,
            required_reserve=minimum_reserve,
        )

    distance = _distance(source, destination)
    per_fuel = {
        technology_id: _fuel_per_ship(
            technology_id, distance, speed, fuel_safety_factor
        )
        for technology_id in (SMALL_CARGO, LARGE_CARGO)
    }
    effective_capacity = {
        technology_id: _CARGO_CAPACITY[technology_id] - per_fuel[technology_id]
        for technology_id in (SMALL_CARGO, LARGE_CARGO)
    }
    if max(effective_capacity.values()) <= 0:
        raise _PreflightRejection(
            "fuel_capacity_unavailable", "conservative fuel estimate leaves no cargo capacity"
        )

    ships: dict[int, int] = {}
    remaining_payload = payload
    remaining_ship_limit = dispatchable
    for technology_id in sorted(
        (SMALL_CARGO, LARGE_CARGO),
        key=lambda tid: (effective_capacity[tid], _CARGO_CAPACITY[tid]),
        reverse=True,
    ):
        effective = effective_capacity[technology_id]
        if effective <= 0 or remaining_ship_limit <= 0:
            continue
        required = int(math.ceil(remaining_payload / effective))
        amount = min(available[technology_id], remaining_ship_limit, required)
        if amount > 0:
            ships[technology_id] = amount
            remaining_payload = max(0, remaining_payload - amount * effective)
            remaining_ship_limit -= amount
        if remaining_payload <= 0:
            break

    if remaining_payload > 0:
        raise _PreflightRejection(
            "insufficient_cargo_capacity",
            "bounded cargo ships cannot carry the resources plus conservative fuel allowance",
            payload=payload,
            maximum_cargo_ships=maximum_ships,
            available_cargo_ships=total_available,
            preserved_cargo_ships=minimum_reserve,
        )

    cargo_capacity = sum(_CARGO_CAPACITY[tid] * amount for tid, amount in ships.items())
    estimated_fuel = sum(per_fuel[tid] * amount for tid, amount in ships.items())
    if (
        not ships
        or sum(ships.values()) >= total_available
        or cargo_capacity < payload + estimated_fuel
    ):
        raise _PreflightRejection(
            "cargo_preflight_inconsistent", "cargo selection did not prove a safe dispatch"
        )
    return ships, cargo_capacity, estimated_fuel


def _check_resource_floors(
    snapshot: dict[str, int],
    resources: dict[str, int],
    floors: dict[str, int],
    estimated_fuel: int,
) -> None:
    required = {
        name: resources[name] + floors[name] + (estimated_fuel if name == "deuterium" else 0)
        for name in RESOURCE_NAMES
    }
    shortfalls = {
        name: required[name] - snapshot[name]
        for name in RESOURCE_NAMES
        if snapshot[name] < required[name]
    }
    if shortfalls:
        raise _PreflightRejection(
            "reserve_floor_violation",
            "source resources cannot fund payload, conservative fuel, and reserve floors",
            shortfalls=shortfalls,
            source_resources=snapshot,
            required=required,
        )


def _check_fleet_slots(
    email: str,
    levels: dict[int, int],
    emergency_reserve: int,
    colonization_reservation: int,
) -> tuple[int, int]:
    try:
        conservative_cap = int(fleetsave.fleet_slots_max(levels))
        class_aware_cap = int(explorer.fleet_slots(levels, email))
        maximum = min(conservative_cap, class_aware_cap)
        used = int(fleetsave.active_slots_in_use(email))
    except Exception as exc:  # noqa: BLE001
        raise _PreflightRejection(
            "fleet_slots_unavailable", "fleet slot feasibility could not be established"
        ) from exc
    reserved = emergency_reserve + colonization_reservation
    if maximum < 1 or used < 0 or used >= 10**6:
        raise _PreflightRejection(
            "fleet_slots_unavailable", "fleet slot state is unavailable or invalid"
        )
    if used + 1 + reserved > maximum:
        raise _PreflightRejection(
            "fleet_slot_reserve_violation",
            "transport would consume an emergency/fleetsave or colonization-reserved slot",
            used=used,
            maximum=maximum,
            emergency_slot_reserve=emergency_reserve,
            colonization_slot_reservation=colonization_reservation,
        )
    return used, maximum


def _active_matching_transport(dispatch: dict[str, Any]) -> dict[str, Any]:
    try:
        ships = dispatch["ships"]
        resources = dispatch["resources"]
        source_coord = dispatch["source_coordinates"]
        destination_coord = dispatch["destination_coordinates"]
        params = (
            dispatch["email"],
            int(dispatch["source_planet_id"]),
            int(dispatch["destination_planet_id"]),
            int(source_coord["galaxy"]),
            int(source_coord["system"]),
            int(source_coord["position"]),
            int(destination_coord["galaxy"]),
            int(destination_coord["system"]),
            int(destination_coord["position"]),
            int(resources["metal"]),
            int(resources["crystal"]),
            int(resources["deuterium"]),
            int(ships.get(str(SMALL_CARGO), ships.get(SMALL_CARGO, 0))),
            int(ships.get(str(LARGE_CARGO), ships.get(LARGE_CARGO, 0))),
        )
        conn = db_reader._conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fm.id, fm.time_arrival FROM fleet_missions fm "
                "JOIN users u ON u.id=fm.user_id "
                "WHERE u.email=%s AND fm.processed=0 AND fm.canceled=0 "
                "AND fm.mission_type=3 AND fm.parent_id IS NULL "
                "AND fm.planet_id_from=%s AND fm.planet_id_to=%s "
                "AND fm.galaxy_from=%s AND fm.system_from=%s AND fm.position_from=%s "
                "AND fm.galaxy_to=%s AND fm.system_to=%s AND fm.position_to=%s "
                "AND COALESCE(fm.metal,0)=%s AND COALESCE(fm.crystal,0)=%s "
                "AND COALESCE(fm.deuterium,0)=%s "
                "AND COALESCE(fm.small_cargo,0)=%s AND COALESCE(fm.large_cargo,0)=%s "
                "ORDER BY fm.id",
                params,
            )
            rows = cur.fetchall()
        return {
            "available": True,
            "matches": [
                {"mission_id": int(row["id"]), "arrival_ts": int(row["time_arrival"])}
                for row in rows
            ],
        }
    except Exception as exc:  # noqa: BLE001 - lack of reconciliation must block a send
        return {
            "available": False,
            "matches": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _reject_record(
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    rejection = _reason(code, message, **details)
    try:
        _transition(ledger, path, record, "rejected", reason=rejection)
    except _LedgerError as exc:
        rejection = _reason(
            "ledger_update_failed",
            "transfer was not sent, but its rejection could not be persisted",
            original_reason=rejection,
            error=str(exc),
        )
    return _outcome(
        record["command_id"], "rejected", reason=rejection, retry_safe=False
    )


def _persist_uncertain(
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    reason: dict[str, Any],
) -> dict[str, Any]:
    try:
        _transition(ledger, path, record, "ambiguous", reason=reason)
    except _LedgerError as exc:
        reason = _reason(
            "ledger_update_failed_after_send_boundary",
            "dispatch state is uncertain and the ambiguous state could not be persisted",
            original_reason=reason,
            error=str(exc),
        )
    return _outcome(
        record["command_id"],
        "ambiguous",
        reason=reason,
        dispatch=_record_dispatch(record),
        retry_safe=False,
    )


def _reconcile_existing(
    ledger: dict[str, Any], path: pathlib.Path, record: dict[str, Any]
) -> dict[str, Any]:
    state = record["state"]
    if state in {"confirmed", "rejected"}:
        return _record_outcome(record)
    dispatch = _record_dispatch(record)
    if dispatch is None:
        if state == "planned":
            return _record_outcome(record)
        reason = _reason(
            "missing_dispatch_fingerprint",
            "non-planned command lacks an exact mission fingerprint and cannot be retried",
        )
        if state != "ambiguous":
            return _persist_uncertain(ledger, path, record, reason)
        return _record_outcome(record, reason=reason)

    reconciliation = _active_matching_transport(dispatch)
    if not reconciliation["available"]:
        reason = _reason(
            "mission_reconciliation_unavailable",
            "active transport missions could not be reconciled; no retry is allowed",
            error=reconciliation.get("error"),
        )
        if state in {"sending"}:
            return _persist_uncertain(ledger, path, record, reason)
        return _record_outcome(record, reason=reason)

    matches = reconciliation["matches"]
    if matches:
        if state != "sent":
            try:
                _transition(
                    ledger,
                    path,
                    record,
                    "sent",
                    mission_matches=matches,
                    reconciled_at=int(time.time()),
                )
            except _LedgerError as exc:
                return _outcome(
                    record["command_id"],
                    "ambiguous",
                    reason=_reason(
                        "ledger_reconciliation_write_failed",
                        "matching transport exists but sent state could not be persisted",
                        error=str(exc),
                    ),
                    dispatch=dispatch,
                )
        return _record_outcome(record)

    if state == "sent":
        try:
            _transition(
                ledger,
                path,
                record,
                "confirmed",
                confirmed_at=int(time.time()),
                confirmation="previously acknowledged transport is no longer active",
            )
        except _LedgerError as exc:
            return _record_outcome(
                record,
                reason=_reason(
                    "confirmation_write_failed",
                    "transport is no longer active but confirmation could not be persisted",
                    error=str(exc),
                ),
            )
        return _record_outcome(record)

    if state == "sending":
        return _persist_uncertain(
            ledger,
            path,
            record,
            _reason(
                "send_outcome_unknown",
                "sending state has no exact active match; blind retry is forbidden",
            ),
        )
    return _record_outcome(
        record,
        reason=_reason(
            "ambiguous_without_active_match",
            "no exact active mission currently resolves this ambiguous command",
        ),
    )


def _dispatch_transport(
    client,
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    plan: dict[str, Any],
    dispatch: dict[str, Any],
) -> dict[str, Any]:
    try:
        _transition(
            ledger,
            path,
            record,
            "sending",
            dispatch=dispatch,
            send_started_at=int(time.time()),
            reason=None,
        )
    except _LedgerError as exc:
        return _outcome(
            record["command_id"],
            "ambiguous",
            reason=_reason(
                "send_boundary_persistence_failed",
                "transport was not attempted because durable sending state was uncertain",
                error=str(exc),
            ),
            dispatch=dispatch,
            retry_safe=False,
        )

    target = (
        dispatch["destination_coordinates"]["galaxy"],
        dispatch["destination_coordinates"]["system"],
        dispatch["destination_coordinates"]["position"],
    )
    ships = {int(tid): int(amount) for tid, amount in dispatch["ships"].items()}
    try:
        response = fleet.transport(
            client,
            target,
            ships,
            plan["resources"],
            speed=plan["speed"],
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 - crossing the POST boundary is ambiguous
        response = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    message = ""
    if isinstance(response, dict):
        message = str(response.get("message") or "")[:500]
    if isinstance(response, dict) and response.get("ok") is True:
        try:
            _transition(
                ledger,
                path,
                record,
                "sent",
                sent_at=int(time.time()),
                server_message=message,
                reason=None,
            )
        except _LedgerError as exc:
            return _outcome(
                record["command_id"],
                "ambiguous",
                reason=_reason(
                    "sent_ack_persistence_failed",
                    "server acknowledged the transport but sent state was not durable",
                    error=str(exc),
                ),
                dispatch=dispatch,
                retry_safe=False,
            )
        return _record_outcome(record)

    reconciliation = _active_matching_transport(dispatch)
    if reconciliation["available"] and reconciliation["matches"]:
        try:
            _transition(
                ledger,
                path,
                record,
                "sent",
                sent_at=int(time.time()),
                mission_matches=reconciliation["matches"],
                server_message=message,
                reason=None,
            )
        except _LedgerError as exc:
            return _outcome(
                record["command_id"],
                "ambiguous",
                reason=_reason(
                    "reconciled_send_persistence_failed",
                    "an exact active transport exists but sent state was not durable",
                    error=str(exc),
                ),
                dispatch=dispatch,
            )
        return _record_outcome(record)

    # This message is produced before send-fleet is called, so it is a definitive
    # check-target rejection.  Every post-send failure remains ambiguous.
    if message.startswith(f"mission {fleet.MISSION_TRANSPORT} not allowed for target"):
        return _reject_record(
            ledger,
            path,
            record,
            "target_rejected_transport",
            "target precheck definitively rejected the transport mission",
            server_message=message,
        )

    return _persist_uncertain(
        ledger,
        path,
        record,
        _reason(
            "send_result_ambiguous",
            "transport response did not prove failure; blind retry is forbidden",
            server_message=message or "no structured response",
            reconciliation_available=reconciliation["available"],
        ),
    )


def _execute_planned(
    client,
    email: str,
    plan: dict[str, Any],
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    minimum_cargo_reserve: int,
    maximum_cargo_ships: int,
    emergency_slot_reserve: int,
    colonization_slot_reservation: int,
    fuel_safety_factor: float,
) -> dict[str, Any]:
    original_planet_id: int | None = None
    restore_required = False
    outcome: dict[str, Any] | None = None
    try:
        if "deadline" in plan and int(time.time()) >= plan["deadline"]:
            raise _PreflightRejection(
                "deadline_expired", "transfer deadline expired before dispatch"
            )

        by_id, by_coord = _owned_planets(client)
        source, destination = _resolve_owned_route(plan, by_id, by_coord)
        original_planet_id = planets.current_planet_id(client)
        if original_planet_id is None or original_planet_id not in by_id:
            raise _PreflightRejection(
                "current_planet_unavailable", "original current planet could not be captured"
            )

        restore_required = original_planet_id != source["id"]
        if restore_required and not planets.switch_planet(client, source["id"]):
            raise _PreflightRejection(
                "source_switch_failed", "could not switch to the owned source planet"
            )
        if planets.current_planet_id(client) != source["id"]:
            raise _PreflightRejection(
                "source_switch_unconfirmed", "source planet selection could not be confirmed"
            )

        # Initial sizing pass.  Everything is repeated below at the send boundary.
        initial_levels = _source_levels(email, source["id"])
        initial_resources = _resource_snapshot(email, source["id"])
        initial_ships, _, initial_fuel = _select_cargo(
            initial_levels,
            sum(plan["resources"].values()),
            source,
            destination,
            plan["speed"],
            minimum_cargo_reserve,
            maximum_cargo_ships,
            fuel_safety_factor,
        )
        _check_resource_floors(
            initial_resources, plan["resources"], plan["reserve_floors"], initial_fuel
        )
        if not initial_ships:
            raise _PreflightRejection(
                "cargo_preflight_inconsistent", "initial cargo selection was empty"
            )

        # Immediate send-boundary rechecks: resources, ships/capacity, slots,
        # matching transport, target ownership/coordinates, and current source.
        live_levels = _source_levels(email, source["id"])
        live_resources = _resource_snapshot(email, source["id"])
        ships, cargo_capacity, estimated_fuel = _select_cargo(
            live_levels,
            sum(plan["resources"].values()),
            source,
            destination,
            plan["speed"],
            minimum_cargo_reserve,
            maximum_cargo_ships,
            fuel_safety_factor,
        )
        _check_resource_floors(
            live_resources, plan["resources"], plan["reserve_floors"], estimated_fuel
        )
        slots_used, slots_maximum = _check_fleet_slots(
            email,
            live_levels,
            emergency_slot_reserve,
            colonization_slot_reservation,
        )

        dispatch = {
            "email": email,
            "source_planet_id": source["id"],
            "source_coordinates": {
                "galaxy": source["galaxy"],
                "system": source["system"],
                "position": source["position"],
            },
            "destination_planet_id": destination["id"],
            "destination_coordinates": {
                "galaxy": destination["galaxy"],
                "system": destination["system"],
                "position": destination["position"],
            },
            "resources": dict(plan["resources"]),
            "ships": {str(tid): amount for tid, amount in sorted(ships.items())},
            "cargo_capacity": cargo_capacity,
            "estimated_fuel": estimated_fuel,
            "minimum_cargo_reserve": minimum_cargo_reserve,
            "fleet_slots_used": slots_used,
            "fleet_slots_maximum": slots_maximum,
            "emergency_slot_reserve": emergency_slot_reserve,
            "colonization_slot_reservation": colonization_slot_reservation,
        }

        matching = _active_matching_transport(dispatch)
        if not matching["available"]:
            raise _PreflightRejection(
                "mission_reconciliation_unavailable",
                "active matching transports could not be checked immediately before send",
                error=matching.get("error"),
            )
        if matching["matches"]:
            try:
                _transition(
                    ledger,
                    path,
                    record,
                    "sent",
                    dispatch=dispatch,
                    mission_matches=matching["matches"],
                    reconciled_at=int(time.time()),
                    reason=None,
                )
            except _LedgerError as exc:
                outcome = _outcome(
                    record["command_id"],
                    "ambiguous",
                    reason=_reason(
                        "deduplication_write_failed",
                        "matching active transport exists but ledger update failed",
                        error=str(exc),
                    ),
                    dispatch=dispatch,
                )
            else:
                outcome = _record_outcome(record)
        else:
            final_by_id, final_by_coord = _owned_planets(client)
            final_source, final_destination = _resolve_owned_route(plan, final_by_id, final_by_coord)
            if final_source["coord"] != source["coord"]:
                raise _PreflightRejection(
                    "source_changed", "source ownership or coordinates changed before send"
                )
            if (
                final_destination["id"] != destination["id"]
                or final_destination["coord"] != destination["coord"]
            ):
                raise _PreflightRejection(
                    "target_changed", "destination ownership or coordinates changed before send"
                )
            if planets.current_planet_id(client) != source["id"]:
                raise _PreflightRejection(
                    "source_changed", "current planet changed immediately before send"
                )
            if "deadline" in plan and int(time.time()) >= plan["deadline"]:
                raise _PreflightRejection(
                    "deadline_expired", "transfer deadline expired during preflight"
                )
            outcome = _dispatch_transport(client, ledger, path, record, plan, dispatch)

    except _PreflightRejection as exc:
        outcome = _reject_record(
            ledger, path, record, exc.code, exc.message, **exc.details
        )
    except Exception as exc:  # noqa: BLE001 - no economic action on unknown preflight errors
        outcome = _reject_record(
            ledger,
            path,
            record,
            "unexpected_preflight_error",
            "unexpected error prevented a proven-safe transfer",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if restore_required and original_planet_id is not None:
            try:
                restored = planets.switch_planet(client, original_planet_id)
            except Exception:  # noqa: BLE001
                restored = False
            if not restored:
                warning = _reason(
                    "planet_restore_failed",
                    "original current planet could not be restored after transfer sequence",
                    original_planet_id=original_planet_id,
                )
                if outcome is None:
                    outcome = _outcome(
                        record["command_id"], "rejected", reason=warning, retry_safe=False
                    )
                else:
                    outcome.setdefault("warnings", []).append(warning)

    if outcome is None:
        return _reject_record(
            ledger,
            path,
            record,
            "missing_outcome",
            "transfer sequence ended without a safe outcome",
        )
    return outcome


def execute_transfer_plan(
    client,
    email: str,
    plan: dict[str, Any],
    *,
    ledger_key: str | None = None,
    minimum_cargo_reserve: int = 1,
    maximum_cargo_ships: int = 250,
    emergency_slot_reserve: int = 1,
    colonization_slot_reservation: int = 0,
    fuel_safety_factor: float = 1.25,
) -> dict[str, Any]:
    """Validate, plan, and at most once dispatch one owned-planet transport.

    ``colonization_slot_reservation`` is supplied by the caller and should be at
    least one while a colonization launch owns a pending slot reservation.  The
    emergency reserve is independently required to be at least one.  A returned
    ``ambiguous`` outcome is never safe to retry with a new command id.
    """
    command_id = plan.get("command_id") if type(plan) is dict else None
    try:
        normalized_email = _validate_email(email)
        normalized_plan = _validate_plan(plan)
        (
            cargo_reserve,
            cargo_limit,
            emergency_reserve,
            colonization_reserve,
            fuel_factor,
        ) = _validate_execute_options(
            minimum_cargo_reserve,
            maximum_cargo_ships,
            emergency_slot_reserve,
            colonization_slot_reservation,
            fuel_safety_factor,
        )
        path = _ledger_path(normalized_email, ledger_key)
    except _ValidationError as exc:
        return _outcome(
            command_id if type(command_id) is str else None,
            "rejected",
            reason=_reason(exc.code, exc.message, **exc.details),
            retry_safe=False,
        )

    command_id = normalized_plan["command_id"]
    try:
        with _serialized(path):
            ledger = _load_ledger(path)
            existing = ledger["commands"].get(command_id)
            if existing is not None:
                if existing["plan"] != normalized_plan:
                    return _outcome(
                        command_id,
                        "rejected",
                        reason=_reason(
                            "command_id_conflict",
                            "command_id already belongs to a different transfer plan",
                            existing_state=existing["state"],
                        ),
                        retry_safe=False,
                    )
                if existing["state"] != "planned":
                    return _reconcile_existing(ledger, path, existing)
                record = existing
            else:
                record = _new_record(normalized_plan)
                ledger["commands"][command_id] = record
                try:
                    _atomic_write_ledger(path, ledger)
                except _LedgerError as exc:
                    return _outcome(
                        command_id,
                        "rejected",
                        reason=_reason(
                            "ledger_unavailable",
                            "transfer was not attempted because planned state was not durable",
                            error=str(exc),
                        ),
                        retry_safe=False,
                    )
            return _execute_planned(
                client,
                normalized_email,
                normalized_plan,
                ledger,
                path,
                record,
                cargo_reserve,
                cargo_limit,
                emergency_reserve,
                colonization_reserve,
                fuel_factor,
            )
    except _LedgerError as exc:
        return _outcome(
            command_id,
            "rejected",
            reason=_reason(
                "ledger_unavailable",
                "transfer ledger or serialization lock is unavailable; nothing was sent",
                error=str(exc),
            ),
            retry_safe=False,
        )


def suggest_bootstrap_equalization_transfers(
    email: str,
    *,
    reserve_floors: dict[str, int],
    target_floors: dict[str, int],
    purpose: str = "bootstrap/equalization",
    speed: int | float = 5,
    deadline: int | None = None,
    command_namespace: str = "equalize",
) -> dict[str, Any]:
    """Suggest deterministic owned-planet transfers from surplus to deficits.

    The helper is read-only.  Donors retain the greater of ``reserve_floors`` and
    ``target_floors`` in the suggestion snapshot; execution repeats all safety
    checks.  Returned plans conform to the strict schema accepted by
    :func:`execute_transfer_plan`.
    """
    try:
        normalized_email = _validate_email(email)
        reserves = _resource_map(reserve_floors, "reserve_floors")
        targets = _resource_map(target_floors, "target_floors")
        normalized_purpose = _validate_text(purpose, "purpose", 160)
        normalized_speed = _validate_speed(speed)
        namespace = _validate_text(command_namespace, "command_namespace", 48)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}", namespace):
            raise _ValidationError(
                "invalid_command_namespace",
                "command_namespace contains unsupported characters",
            )
        normalized_deadline = None
        if deadline is not None:
            normalized_deadline = _strict_int(deadline, "deadline", 1, MAX_DEADLINE)
            if normalized_deadline <= int(time.time()):
                raise _ValidationError("deadline_expired", "suggestion deadline has expired")
        if any(targets[name] < reserves[name] for name in RESOURCE_NAMES):
            raise _ValidationError(
                "target_below_reserve",
                "target_floors must be greater than or equal to reserve_floors",
            )
    except _ValidationError as exc:
        return {
            "ok": False,
            "state": "rejected",
            "reason": _reason(exc.code, exc.message, **exc.details),
            "suggestions": [],
        }

    try:
        owned = db_reader.list_planets_db(normalized_email)
        if len(owned) < 2:
            raise _PreflightRejection(
                "insufficient_owned_planets",
                "at least two owned planets are required for equalization",
            )
        planet_ids = [int(row["id"]) for row in owned]
        if len(set(planet_ids)) != len(planet_ids) or any(pid <= 0 for pid in planet_ids):
            raise _PreflightRejection(
                "ownership_unavailable", "DB owned planet snapshot was malformed"
            )
        working = {
            planet_id: _resource_snapshot(normalized_email, planet_id)
            for planet_id in planet_ids
        }
    except _PreflightRejection as exc:
        return {
            "ok": False,
            "state": "rejected",
            "reason": _reason(exc.code, exc.message, **exc.details),
            "suggestions": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "rejected",
            "reason": _reason(
                "ownership_unavailable",
                "owned planet snapshot could not be established",
                error_type=type(exc).__name__,
            ),
            "suggestions": [],
        }

    suggestions: list[dict[str, Any]] = []
    recipients = sorted(
        planet_ids,
        key=lambda pid: (
            -sum(max(0, targets[name] - working[pid][name]) for name in RESOURCE_NAMES),
            pid,
        ),
    )
    donor_floor = {
        name: max(reserves[name], targets[name]) for name in RESOURCE_NAMES
    }
    for destination_id in recipients:
        deficits = {
            name: max(0, targets[name] - working[destination_id][name])
            for name in RESOURCE_NAMES
        }
        if not any(deficits.values()):
            continue
        donors = sorted(
            (pid for pid in planet_ids if pid != destination_id),
            key=lambda pid: (
                -sum(
                    max(0, working[pid][name] - donor_floor[name])
                    for name in RESOURCE_NAMES
                ),
                pid,
            ),
        )
        for source_id in donors:
            resources = {
                name: min(
                    deficits[name],
                    max(0, working[source_id][name] - donor_floor[name]),
                )
                for name in RESOURCE_NAMES
            }
            if not any(resources.values()):
                continue
            base_plan: dict[str, Any] = {
                "source_planet_id": source_id,
                "destination_planet_id": destination_id,
                "resources": resources,
                "reserve_floors": dict(reserves),
                "purpose": normalized_purpose,
                "speed": normalized_speed,
            }
            if normalized_deadline is not None:
                base_plan["deadline"] = normalized_deadline
            fingerprint = hashlib.sha256(
                json.dumps(
                    base_plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("utf-8")
            ).hexdigest()[:24]
            suggested_plan = {"command_id": f"{namespace}:{fingerprint}", **base_plan}
            # This is an internal assertion through the same strict normalizer, not a
            # permissive alternate schema.
            suggestions.append(_validate_plan(suggested_plan))
            for name in RESOURCE_NAMES:
                working[source_id][name] -= resources[name]
                working[destination_id][name] += resources[name]
                deficits[name] -= resources[name]
            if not any(deficits.values()):
                break

    return {
        "ok": True,
        "state": "suggested" if suggestions else "no_action",
        "reason": None,
        "suggestions": suggestions,
        "unmet_deficits": {
            str(planet_id): {
                name: max(0, targets[name] - working[planet_id][name])
                for name in RESOURCE_NAMES
            }
            for planet_id in planet_ids
            if any(working[planet_id][name] < targets[name] for name in RESOURCE_NAMES)
        },
    }


__all__ = [
    "RESOURCE_NAMES",
    "TRANSFER_STATES",
    "execute_transfer_plan",
    "suggest_bootstrap_equalization_transfers",
]
