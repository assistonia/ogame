"""Owned-moon Sensor Phalanx and Jump Gate helpers.

Ownership and parent-planet pairing come from the read-only bot DB connection.
Actions still go through OGameX's authenticated web controllers so their game
validation and side effects remain authoritative.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from html import unescape
from typing import Any

import requests

from bots.shared import planets as planets_mod
from bots.shared.db_reader import _conn
from bots.shared.game_objects import SHIPS

__all__ = [
    "list_owned_moons",
    "phalanx_scan",
    "jump_gate_status",
    "execute_jump_gate_transfer",
]

_NON_TRANSFERABLE_IDS = frozenset((212, 217))
_TRANSFERABLE_SHIPS = {
    technology_id: machine_name
    for technology_id, machine_name in SHIPS.items()
    if technology_id not in _NON_TRANSFERABLE_IDS
}
_TRANSFERABLE_IDS_BY_NAME = {
    machine_name: technology_id
    for technology_id, machine_name in _TRANSFERABLE_SHIPS.items()
}
_CSRF_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
_JUMPGATE_ERROR_RE = re.compile(
    r'id="jumpgateError".*?<p\s+class="error">\s*(.*?)\s*</p>', re.DOTALL
)
_TARGET_SELECT_RE = re.compile(
    r'<select\s+name="targetMoonId"[^>]*>(.*?)</select>', re.DOTALL
)
_OPTION_RE = re.compile(r'<option\s+value="(\d+)"')
_SHIP_MAX_RE = re.compile(r'var\s+shipMaxAmounts\s*=\s*(\{.*?\})\s*;', re.DOTALL)
_COOLDOWN_RE = re.compile(
    r'simpleCountdown\(\$\("#cooldown"\),\s*(\d+)', re.DOTALL
)


def _account_email(client_or_email: Any) -> str:
    email = client_or_email if isinstance(client_or_email, str) else getattr(
        client_or_email, "email", None
    )
    if not isinstance(email, str) or not email.strip():
        raise ValueError("client_or_email must provide a non-empty account email")
    return email.strip()


def list_owned_moons(client_or_email: Any) -> list[dict]:
    """Return owned moons from DB, paired to parent planets by coordinates."""
    email = _account_email(client_or_email)
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.name, m.galaxy, m.system, m.planet AS position, "
            "m.sensor_phalanx, m.jump_gate, m.jump_gate_cooldown, "
            "m.default_jump_gate_target_id, "
            "(m.id = u.planet_current) AS is_current, "
            "parent.id AS parent_id, parent.name AS parent_name "
            "FROM planets m "
            "JOIN users u ON u.id = m.user_id "
            "LEFT JOIN planets parent ON parent.user_id = m.user_id "
            "AND parent.galaxy = m.galaxy AND parent.system = m.system "
            "AND parent.planet = m.planet AND parent.planet_type = 1 "
            "WHERE u.email = %s AND m.planet_type = 3 "
            "ORDER BY m.galaxy, m.system, m.planet, m.id",
            (email,),
        )
        rows = cur.fetchall()

    moons: list[dict] = []
    for row in rows:
        galaxy = int(row["galaxy"])
        system = int(row["system"])
        position = int(row["position"])
        parent = None
        if row.get("parent_id") is not None:
            parent = {
                "id": int(row["parent_id"]),
                "name": row.get("parent_name") or "",
                "galaxy": galaxy,
                "system": system,
                "position": position,
                "coord": (galaxy, system, position),
            }
        moons.append(
            {
                "id": int(row["id"]),
                "name": row.get("name") or "",
                "galaxy": galaxy,
                "system": system,
                "position": position,
                "coord": (galaxy, system, position),
                "is_current": bool(row.get("is_current")),
                "sensor_phalanx": int(row.get("sensor_phalanx") or 0),
                "jump_gate": int(row.get("jump_gate") or 0),
                "jump_gate_cooldown": int(row.get("jump_gate_cooldown") or 0),
                "default_jump_gate_target_id": (
                    int(row["default_jump_gate_target_id"])
                    if row.get("default_jump_gate_target_id") is not None
                    else None
                ),
                "parent_planet_id": parent["id"] if parent else None,
                "parent": parent,
            }
        )
    return moons


def _error(code: str, message: str, **extra: Any) -> dict:
    return {
        "success": False,
        "is_error": True,
        "error_code": code,
        "error_message": message,
        **extra,
    }


def _owned_moon(moons: list[dict], moon_id: Any, role: str) -> tuple[dict | None, dict | None]:
    if isinstance(moon_id, bool):
        return None, _error(f"invalid_{role}_moon", f"{role} moon ID must be an integer")
    try:
        normalized_id = int(moon_id)
    except (TypeError, ValueError):
        return None, _error(f"invalid_{role}_moon", f"{role} moon ID must be an integer")
    if normalized_id <= 0 or str(moon_id).strip() != str(normalized_id):
        return None, _error(f"invalid_{role}_moon", f"{role} moon ID must be a positive integer")
    moon = next((item for item in moons if item["id"] == normalized_id), None)
    if moon is None:
        return None, _error(
            f"unowned_{role}_moon", f"{role} moon is not owned by this account"
        )
    return moon, None


def _run_on_owned_moon(
    client: Any, source_moon: dict, operation: Callable[[], dict]
) -> dict:
    """Switch to an owned moon and always attempt to restore the prior body."""
    original_id = planets_mod.current_planet_id(client)
    if original_id is None:
        return _error("current_body_unknown", "Could not determine the selected body")

    source_id = int(source_moon["id"])
    switched = original_id != source_id
    if switched and not planets_mod.switch_planet(client, source_id):
        restored = planets_mod.switch_planet(client, original_id)
        return _error(
            "moon_switch_failed",
            "Could not select the source moon",
            selection={
                "source_moon_id": source_id,
                "original_planet_id": original_id,
                "switched": False,
                "restored": restored,
            },
        )

    result: dict
    restored = True
    try:
        result = operation()
        if not isinstance(result, dict):
            result = _error("invalid_result", "Moon operation returned an invalid result")
    except Exception as exc:  # noqa: BLE001 - public helpers return structured failures
        result = _error("moon_operation_failed", str(exc) or exc.__class__.__name__)
    finally:
        if switched:
            restored = planets_mod.switch_planet(client, original_id)

    result["selection"] = {
        "source_moon_id": source_id,
        "original_planet_id": original_id,
        "switched": switched,
        "restored": restored,
    }
    if not restored:
        operation_success = bool(result.get("success"))
        result["operation_success"] = operation_success
        result["success"] = False
        result["is_error"] = True
        result["error_code"] = "moon_restore_failed"
        result["error_message"] = (
            "Operation completed but the previously selected body could not be restored"
            if operation_success
            else "The previously selected body could not be restored"
        )
    return result


def _csrf_token(client: Any, path: str) -> str:
    cached = getattr(client, "_token", None)
    if isinstance(cached, str) and cached:
        return cached
    response = client.session.get(f"{client.base_url}{path}", allow_redirects=True)
    response.raise_for_status()
    match = _CSRF_RE.search(response.text)
    if match is None:
        raise RuntimeError(f"CSRF token not found on {path}")
    return unescape(match.group(1))


def _json_payload(response: Any) -> Mapping[str, Any] | None:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _response_message(payload: Mapping[str, Any] | None, fallback: str) -> str:
    if payload is not None:
        for key in ("error_message", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _normalize_coordinate(value: Any, label: str) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {label} coordinates")
    return {
        "galaxy": int(value["galaxy"]),
        "system": int(value["system"]),
        "position": int(value["position"]),
        "planet_name": str(value.get("planet_name") or ""),
    }


def _normalize_fleet_movements(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("fleet_movements is not a list")

    normalized: list[dict] = []
    for movement in value:
        if not isinstance(movement, Mapping) or not isinstance(movement.get("ships"), Mapping):
            raise ValueError("invalid fleet movement record")
        ships = {
            str(name): int(amount)
            for name, amount in movement["ships"].items()
            if int(amount) > 0
        }
        record = {
            "mission_id": int(movement["mission_id"]),
            "mission_type": int(movement["mission_type"]),
            "mission_type_name": str(movement.get("mission_type_name") or ""),
            "is_incoming": bool(movement["is_incoming"]),
            "is_return_trip": bool(movement["is_return_trip"]),
            "time_departure": int(movement["time_departure"]),
            "time_arrival": int(movement["time_arrival"]),
            "fleet_direction": str(movement.get("fleet_direction") or ""),
            "fleet_owner_id": int(movement["fleet_owner_id"]),
            "fleet_icon": str(movement.get("fleet_icon") or ""),
            "display_time": int(movement["display_time"]),
            "origin": _normalize_coordinate(movement.get("origin"), "origin"),
            "destination": _normalize_coordinate(movement.get("destination"), "destination"),
            "ships": ships,
            "ship_count": int(movement["ship_count"]),
            "fleet_speed": int(movement["fleet_speed"]),
        }
        if record["ship_count"] != sum(ships.values()):
            raise ValueError("fleet movement ship count does not match ships")
        normalized.append(record)
    return normalized


def _normalize_scan_success(payload: Mapping[str, Any], source_moon: dict) -> dict:
    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("invalid scan target")
    position = target.get("position")
    normalized_target = {
        "galaxy": int(target["galaxy"]),
        "system": int(target["system"]),
        "position": int(position) if position is not None else None,
        "planet_name": str(target.get("planet_name") or ""),
        "player_name": str(target.get("player_name") or ""),
    }
    movements = _normalize_fleet_movements(payload.get("fleet_movements"))
    if int(payload.get("fleet_count", -1)) != len(movements):
        raise ValueError("fleet_count does not match fleet_movements")
    result = {
        "success": True,
        "is_error": False,
        "source_moon": source_moon,
        "server_time": int(payload["server_time"]),
        "target": normalized_target,
        "scan_cost": int(payload["scan_cost"]),
        "fleet_count": len(movements),
        "fleet_movements": movements,
    }
    if payload.get("scanned_planet_count") is not None:
        result["scanned_planet_count"] = int(payload["scanned_planet_count"])
    return result


def phalanx_scan(
    client: Any,
    source_moon_id: int,
    galaxy: int,
    system: int,
    position: int | None = None,
) -> dict:
    """POST one authorized phalanx scan and atomically normalize its records."""
    try:
        galaxy = int(galaxy)
        system = int(system)
        position = int(position) if position is not None else None
    except (TypeError, ValueError):
        return _error("invalid_coordinates", "Scan coordinates must be integers")
    if galaxy < 1 or system < 1 or (position is not None and position < 1):
        return _error("invalid_coordinates", "Scan coordinates must be positive")

    try:
        moons = list_owned_moons(client)
    except Exception as exc:  # noqa: BLE001
        return _error("moon_lookup_failed", str(exc) or exc.__class__.__name__)
    source_moon, error = _owned_moon(moons, source_moon_id, "source")
    if error is not None:
        return error
    assert source_moon is not None
    if source_moon["sensor_phalanx"] < 1:
        return _error("phalanx_unavailable", "Source moon has no Sensor Phalanx")

    def operation() -> dict:
        data: dict[str, Any] = {
            "_token": _csrf_token(client, "/galaxy"),
            "galaxy": galaxy,
            "system": system,
        }
        if position is not None:
            data["position"] = position
        try:
            response = client.session.post(
                f"{client.base_url}/ajax/phalanx/scan",
                data=data,
                headers={"X-Requested-With": "XMLHttpRequest"},
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return _error(
                "ambiguous_phalanx_post",
                str(exc) or "Phalanx POST outcome is unknown",
                ambiguous=True,
            )

        payload = _json_payload(response)
        if response.status_code >= 500:
            return _error(
                "ambiguous_phalanx_post",
                _response_message(payload, f"Phalanx scan failed ({response.status_code})"),
                ambiguous=True,
                http_status=response.status_code,
            )
        if response.status_code >= 400 or payload is None:
            return _error(
                "phalanx_request_failed",
                _response_message(payload, f"Phalanx scan failed ({response.status_code})"),
                http_status=response.status_code,
            )
        if bool(payload.get("is_error")) or not bool(payload.get("success")):
            return _error(
                "phalanx_rejected",
                _response_message(payload, "Phalanx scan was rejected"),
                http_status=response.status_code,
                target=payload.get("target"),
            )
        try:
            return _normalize_scan_success(payload, source_moon)
        except (KeyError, TypeError, ValueError) as exc:
            return _error("invalid_phalanx_response", str(exc))

    return _run_on_owned_moon(client, source_moon, operation)


def _parse_jump_gate_status(html: str, source_moon: dict, owned_moons: list[dict]) -> dict:
    error_match = _JUMPGATE_ERROR_RE.search(html)
    if error_match is not None:
        message = unescape(re.sub(r"<[^>]+>", "", error_match.group(1))).strip()
        return _error("jump_gate_unavailable", message or "Jump Gate is unavailable")

    token_match = _CSRF_RE.search(html)
    if token_match is None:
        return _error("invalid_jump_gate_response", "Jump Gate CSRF token is missing")

    moon_by_id = {moon["id"]: moon for moon in owned_moons}
    eligible_ids: list[int] = []
    target_match = _TARGET_SELECT_RE.search(html)
    if target_match is not None:
        eligible_ids = [
            target_id
            for target_id in (int(value) for value in _OPTION_RE.findall(target_match.group(1)))
            if target_id > 0
        ]
    if any(target_id not in moon_by_id for target_id in eligible_ids):
        return _error(
            "invalid_jump_gate_response", "Jump Gate returned a target not owned by the account"
        )

    available_amounts: dict[int, int] = {}
    ship_match = _SHIP_MAX_RE.search(html)
    if ship_match is not None:
        try:
            raw_amounts = json.loads(ship_match.group(1))
            if not isinstance(raw_amounts, dict):
                raise ValueError("shipMaxAmounts is not an object")
            available_amounts = {int(key): int(value) for key, value in raw_amounts.items()}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _error("invalid_jump_gate_response", str(exc))
    if any(ship_id not in _TRANSFERABLE_SHIPS for ship_id in available_amounts):
        return _error(
            "invalid_jump_gate_response", "Jump Gate returned a non-transferable ship"
        )

    cooldown_match = _COOLDOWN_RE.search(html)
    cooldown_remaining = int(cooldown_match.group(1)) if cooldown_match else 0
    on_cooldown = 'id="jumpgateNotReady"' in html
    available_ships = {
        machine_name: {
            "id": technology_id,
            "amount": max(0, int(available_amounts.get(technology_id, 0))),
        }
        for technology_id, machine_name in _TRANSFERABLE_SHIPS.items()
    }
    return {
        "success": True,
        "is_error": False,
        "source_moon": source_moon,
        "on_cooldown": on_cooldown,
        "cooldown_remaining": cooldown_remaining,
        "ready": not on_cooldown and bool(eligible_ids),
        "eligible_targets": [moon_by_id[target_id] for target_id in eligible_ids],
        "available_ships": available_ships,
        "_csrf_token": unescape(token_match.group(1)),
    }


def _jump_gate_status_selected(client: Any, source_moon: dict, moons: list[dict]) -> dict:
    try:
        response = client.session.get(
            f"{client.base_url}/ajax/jumpgate", allow_redirects=False
        )
    except requests.RequestException as exc:
        return _error("jump_gate_status_failed", str(exc))
    if response.status_code >= 400 or response.status_code in (301, 302, 303, 307, 308):
        return _error(
            "jump_gate_status_failed",
            f"Jump Gate status failed ({response.status_code})",
            http_status=response.status_code,
        )
    return _parse_jump_gate_status(response.text, source_moon, moons)


def jump_gate_status(client: Any, source_moon_id: int) -> dict:
    """Return authoritative cooldown, eligible targets, and available ships."""
    try:
        moons = list_owned_moons(client)
    except Exception as exc:  # noqa: BLE001
        return _error("moon_lookup_failed", str(exc) or exc.__class__.__name__)
    source_moon, error = _owned_moon(moons, source_moon_id, "source")
    if error is not None:
        return error
    assert source_moon is not None

    result = _run_on_owned_moon(
        client,
        source_moon,
        lambda: _jump_gate_status_selected(client, source_moon, moons),
    )
    result.pop("_csrf_token", None)
    return result


def _normalize_transfer_ships(ships: Any) -> tuple[dict[int, int] | None, dict | None]:
    if not isinstance(ships, Mapping) or not ships:
        return None, _error("invalid_ships", "ships must be a non-empty mapping")

    normalized: dict[int, int] = {}
    for ship_key, amount in ships.items():
        if isinstance(ship_key, bool):
            return None, _error("invalid_ship", "Ship identifiers must be IDs or machine names")
        if isinstance(ship_key, int) or (isinstance(ship_key, str) and ship_key.isdigit()):
            ship_id = int(ship_key)
        elif isinstance(ship_key, str):
            ship_id = _TRANSFERABLE_IDS_BY_NAME.get(ship_key, 0)
        else:
            ship_id = 0

        if ship_id in _NON_TRANSFERABLE_IDS:
            return None, _error(
                "non_transferable_ship", "Solar satellites and crawlers cannot use Jump Gates"
            )
        if ship_id not in _TRANSFERABLE_SHIPS:
            return None, _error("invalid_ship", f"Unknown transferable ship: {ship_key}")
        if ship_id in normalized:
            return None, _error("duplicate_ship", f"Ship {ship_id} was specified more than once")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return None, _error(
                "invalid_ship_amount", f"Ship {ship_id} amount must be a positive integer"
            )
        normalized[ship_id] = amount
    return normalized, None


def execute_jump_gate_transfer(
    client: Any,
    source_moon_id: int,
    destination_moon_id: int,
    ships: Mapping[int | str, int],
) -> dict:
    """Validate and execute exactly one Jump Gate transfer POST."""
    normalized_ships, error = _normalize_transfer_ships(ships)
    if error is not None:
        return error
    assert normalized_ships is not None

    try:
        moons = list_owned_moons(client)
    except Exception as exc:  # noqa: BLE001
        return _error("moon_lookup_failed", str(exc) or exc.__class__.__name__)
    source_moon, error = _owned_moon(moons, source_moon_id, "source")
    if error is not None:
        return error
    destination_moon, error = _owned_moon(moons, destination_moon_id, "destination")
    if error is not None:
        return error
    assert source_moon is not None and destination_moon is not None
    if source_moon["id"] == destination_moon["id"]:
        return _error("same_jump_gate", "Source and destination moons must differ")
    if source_moon["jump_gate"] < 1 or destination_moon["jump_gate"] < 1:
        return _error("jump_gate_unavailable", "Both owned moons must have a Jump Gate")

    def operation() -> dict:
        status = _jump_gate_status_selected(client, source_moon, moons)
        if not status.get("success"):
            status.pop("_csrf_token", None)
            return status
        if status.get("on_cooldown"):
            return _error(
                "source_on_cooldown",
                "Source Jump Gate is on cooldown",
                cooldown_remaining=int(status.get("cooldown_remaining") or 0),
            )
        eligible_ids = {moon["id"] for moon in status["eligible_targets"]}
        if destination_moon["id"] not in eligible_ids:
            return _error(
                "destination_not_eligible",
                "Destination Jump Gate is unavailable or on cooldown",
            )

        for ship_id, amount in normalized_ships.items():
            machine_name = _TRANSFERABLE_SHIPS[ship_id]
            available = int(status["available_ships"][machine_name]["amount"])
            if amount > available:
                return _error(
                    "insufficient_ships",
                    f"Requested {amount} {machine_name}, but only {available} are available",
                    ship_id=ship_id,
                    requested=amount,
                    available=available,
                )

        data: dict[str, Any] = {
            "_token": status["_csrf_token"],
            "targetMoonId": destination_moon["id"],
        }
        data.update({f"ship_{ship_id}": amount for ship_id, amount in normalized_ships.items()})
        try:
            response = client.session.post(
                f"{client.base_url}/ajax/jumpgate/execute",
                data=data,
                headers={"X-Requested-With": "XMLHttpRequest"},
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return _error(
                "ambiguous_jump_gate_post",
                str(exc) or "Jump Gate POST outcome is unknown",
                ambiguous=True,
            )

        payload = _json_payload(response)
        if response.status_code >= 500 or (response.status_code < 400 and payload is None):
            return _error(
                "ambiguous_jump_gate_post",
                _response_message(payload, f"Jump Gate transfer failed ({response.status_code})"),
                ambiguous=True,
                http_status=response.status_code,
            )
        if response.status_code >= 400 or payload is None:
            return _error(
                "jump_gate_transfer_rejected",
                _response_message(payload, f"Jump Gate transfer failed ({response.status_code})"),
                http_status=response.status_code,
            )
        if bool(payload.get("is_error")) or not bool(payload.get("success")):
            return _error(
                "jump_gate_transfer_rejected",
                _response_message(payload, "Jump Gate transfer was rejected"),
                http_status=response.status_code,
            )
        return {
            "success": True,
            "is_error": False,
            "source_moon": source_moon,
            "destination_moon": destination_moon,
            "ships": {
                _TRANSFERABLE_SHIPS[ship_id]: {"id": ship_id, "amount": amount}
                for ship_id, amount in normalized_ships.items()
            },
            "message": str(payload.get("message") or ""),
            "redirect": payload.get("redirect"),
            "ambiguous": False,
        }

    return _run_on_owned_moon(client, source_moon, operation)
