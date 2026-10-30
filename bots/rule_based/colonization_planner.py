"""Pure, resumable colonization planning.

The planner performs no DB, HTTP, queue, or fleet work.  It compares a new
planet with the best supplied existing-planet investment over an explicit
horizon and emits a serializable command that can be resumed by ``colony.py``.

Candidate observations are intentionally optional.  Missing observations use
conservative, documented scores rather than optimistic averages:

* fields: 0.35 (small/unknown planet),
* deuterium and solar: 0.40 each (temperature benefit is unproven),
* distance: 20,000 distance units (cross-galaxy-like logistics),
* threat: 0.65 on a 0..1 risk scale (unknown space is not assumed safe).

Resource values use a transparent 1:1 sum.  Callers that use a different
exchange rate can pass ``resource_weights``.  All payback results include the
colony ship, missing research, bootstrap stock, setup delay, and the production
foregone by delaying the supplied existing-planet investment.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


ENERGY_TECHNOLOGY = 113
IMPULSE_DRIVE = 117
ESPIONAGE_TECHNOLOGY = 106
ASTROPHYSICS = 124
RESEARCH_LAB = 31
SHIPYARD = 21
COLONY_SHIP = 208

RESOURCE_KEYS = ("metal", "crystal", "deuterium")
COLONY_SHIP_COST = {"metal": 10_000, "crystal": 20_000, "deuterium": 10_000}
DEFAULT_BOOTSTRAP_RESOURCES = {
    "metal": 50_000,
    "crystal": 25_000,
    "deuterium": 10_000,
}
DEFAULT_RESOURCE_WEIGHTS = {"metal": 1.0, "crystal": 1.0, "deuterium": 1.0}

# (base metal, crystal, deuterium, cost factor, required target level)
RESEARCH_CHAIN = (
    (ENERGY_TECHNOLOGY, "energy_technology", (0, 800, 400), 2.0, 1),
    (IMPULSE_DRIVE, "impulse_drive", (2_000, 4_000, 600), 2.0, 3),
    (ESPIONAGE_TECHNOLOGY, "espionage_technology", (200, 1_000, 200), 2.0, 4),
)

ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {
        "fields": 0.30,
        "deuterium": 0.17,
        "solar": 0.13,
        "distance": 0.16,
        "threat": 0.24,
    },
    "mining": {
        "fields": 0.38,
        "deuterium": 0.18,
        "solar": 0.12,
        "distance": 0.14,
        "threat": 0.18,
    },
    "deuterium": {
        "fields": 0.22,
        "deuterium": 0.36,
        "solar": 0.07,
        "distance": 0.13,
        "threat": 0.22,
    },
    "solar": {
        "fields": 0.24,
        "deuterium": 0.06,
        "solar": 0.34,
        "distance": 0.15,
        "threat": 0.21,
    },
    "shipyard": {
        "fields": 0.34,
        "deuterium": 0.10,
        "solar": 0.14,
        "distance": 0.18,
        "threat": 0.24,
    },
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _first_number(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _finite_number(source.get(key))
        if number is not None:
            return number
    return None


def normalize_resources(resources: Mapping[str, Any] | None) -> dict[str, int]:
    """Return a non-negative, integer three-resource mapping."""
    source = resources if isinstance(resources, Mapping) else {}
    return {
        key: max(0, int(_finite_number(source.get(key)) or 0))
        for key in RESOURCE_KEYS
    }


def add_resources(*parts: Mapping[str, Any] | None) -> dict[str, int]:
    total = {key: 0 for key in RESOURCE_KEYS}
    for part in parts:
        clean = normalize_resources(part)
        for key in RESOURCE_KEYS:
            total[key] += clean[key]
    return total


def resource_value(
    resources: Mapping[str, Any] | None,
    weights: Mapping[str, Any] | None = None,
) -> float:
    clean = normalize_resources(resources)
    configured = weights if isinstance(weights, Mapping) else DEFAULT_RESOURCE_WEIGHTS
    return sum(
        clean[key] * max(0.0, _finite_number(configured.get(key)) or 0.0)
        for key in RESOURCE_KEYS
    )


def normalize_target(target: Mapping[str, Any] | Sequence[Any]) -> dict[str, int]:
    """Normalize a coordinate into ``{galaxy, system, position}`` or raise ValueError."""
    if isinstance(target, Mapping):
        nested = target.get("exact_target")
        if isinstance(nested, (Mapping, Sequence)) and not isinstance(nested, (str, bytes)):
            return normalize_target(nested)
        raw = (target.get("galaxy"), target.get("system"), target.get("position"))
    elif isinstance(target, Sequence) and not isinstance(target, (str, bytes)):
        if len(target) != 3:
            raise ValueError("target must contain galaxy, system, and position")
        raw = (target[0], target[1], target[2])
    else:
        raise ValueError("target must be a mapping or three-item sequence")
    try:
        galaxy, system, position = (int(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("target coordinates must be integers") from exc
    if galaxy < 1 or system < 1 or not 1 <= position <= 15:
        raise ValueError("target outside legal galaxy/system/position bounds")
    return {"galaxy": galaxy, "system": system, "position": position}


def deterministic_command_id(
    account: str,
    target: Mapping[str, Any] | Sequence[Any],
    goal: str = "colonize",
) -> str:
    """Stable ID derived only from normalized account, exact target, and goal."""
    exact = normalize_target(target)
    canonical = (
        f"{str(account).strip().lower()}|{exact['galaxy']}:{exact['system']}:"
        f"{exact['position']}|{str(goal).strip().lower()}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"colonize-{digest}"


def _coordinate_distance(
    target: Mapping[str, int], origin: Mapping[str, Any] | Sequence[Any] | None
) -> float | None:
    if origin is None:
        return None
    try:
        start = normalize_target(origin)
    except ValueError:
        return None
    galaxy_delta = abs(target["galaxy"] - start["galaxy"])
    if galaxy_delta:
        return float(galaxy_delta * 20_000)
    system_delta = abs(target["system"] - start["system"])
    if system_delta:
        return float(system_delta * 5 * 19 + 2_700)
    return float(abs(target["position"] - start["position"]) * 5 + 1_000)


def _factor_score(value: float) -> float:
    if value <= 1.0:
        return max(0.0, value)
    if value <= 2.0:
        return max(0.0, min(1.0, value / 2.0))
    return max(0.0, min(1.0, value / 100.0))


def score_candidate(
    candidate: Mapping[str, Any] | Sequence[Any],
    *,
    role: str = "balanced",
    origin: Mapping[str, Any] | Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Score one candidate from fields/temperature/output/distance/threat observations.

    Explicit ``deuterium``/``deuterium_factor`` and ``solar``/``solar_factor``
    observations win.  Otherwise temperature derives both tendencies.  Missing
    metrics receive the conservative fallbacks documented in the module docstring.
    """
    source: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else {}
    target = normalize_target(candidate)
    assumptions: list[str] = []

    fields = _first_number(source, "fields", "field_count", "fields_total")
    if fields is None:
        field_score = 0.35
        assumptions.append("fields_missing:score=0.35")
    else:
        field_score = max(0.0, min(1.0, (fields - 100.0) / 150.0))

    temperature = _first_number(source, "temperature", "temp_max", "max_temperature")
    deuterium = _first_number(
        source, "deuterium", "deuterium_factor", "deuterium_score"
    )
    if deuterium is not None:
        deuterium_score = _factor_score(deuterium)
    elif temperature is not None:
        deuterium_score = max(0.0, min(1.0, (100.0 - temperature) / 180.0))
        assumptions.append("deuterium_derived_from_temperature")
    else:
        deuterium_score = 0.40
        assumptions.append("deuterium_missing:score=0.40")

    solar = _first_number(source, "solar", "solar_factor", "solar_score")
    if solar is not None:
        solar_score = _factor_score(solar)
    elif temperature is not None:
        satellite_output = max(0.0, math.floor((temperature + 140.0) / 6.0))
        solar_score = max(0.0, min(1.0, satellite_output / 60.0))
        assumptions.append("solar_derived_from_temperature")
    else:
        solar_score = 0.40
        assumptions.append("solar_missing:score=0.40")

    distance = _first_number(source, "distance", "logistics_distance")
    if distance is None:
        distance = _coordinate_distance(target, origin)
    if distance is None:
        distance = 20_000.0
        assumptions.append("distance_missing:distance=20000")
    distance = max(0.0, distance)
    distance_score = 1.0 / (1.0 + distance / 5_000.0)

    threat = _first_number(source, "threat", "threat_score", "risk")
    if threat is None:
        threat_risk = 0.65
        assumptions.append("threat_missing:risk=0.65")
    else:
        threat_risk = _factor_score(max(0.0, threat))
    safety_score = 1.0 - threat_risk

    normalized_role = str(role).strip().lower()
    weights = ROLE_WEIGHTS.get(normalized_role, ROLE_WEIGHTS["balanced"])
    components = {
        "fields": field_score,
        "deuterium": deuterium_score,
        "solar": solar_score,
        "distance": distance_score,
        "threat": safety_score,
    }
    score = sum(components[key] * weights[key] for key in components)
    return {
        "exact_target": target,
        "role": normalized_role if normalized_role in ROLE_WEIGHTS else "balanced",
        "score": round(score, 6),
        "components": {key: round(value, 6) for key, value in components.items()},
        "weights": dict(weights),
        "observations": {
            "fields": fields,
            "temperature": temperature,
            "deuterium": deuterium,
            "solar": solar,
            "distance": distance,
            "threat_risk": threat_risk,
        },
        "fallbacks": assumptions,
    }


def rank_candidates(
    candidates: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    role: str = "balanced",
    origin: Mapping[str, Any] | Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic best-first candidate scores."""
    ranked = [score_candidate(candidate, role=role, origin=origin) for candidate in candidates]
    ranked.sort(
        key=lambda item: (
            -item["score"],
            item["exact_target"]["galaxy"],
            item["exact_target"]["system"],
            item["exact_target"]["position"],
        )
    )
    return ranked


def _research_level_cost(
    base: tuple[int, int, int], factor: float, target_level: int
) -> dict[str, int]:
    multiplier = factor ** max(0, target_level - 1)
    return {
        key: int(math.floor(amount * multiplier))
        for key, amount in zip(RESOURCE_KEYS, base)
    }


def astrophysics_target_for_planets(target_planets: int) -> int:
    return max(0, 2 * (max(1, int(target_planets)) - 1) - 1)


def prerequisite_chain(
    levels: Mapping[int, Any] | None,
    *,
    target_planets: int,
) -> list[dict[str, Any]]:
    """Describe every facility/research/ship prerequisite and its exact cost."""
    current = levels if isinstance(levels, Mapping) else {}
    astro_target = astrophysics_target_for_planets(target_planets)
    chain: list[dict[str, Any]] = [{
        "kind": "facility",
        "technology_id": RESEARCH_LAB,
        "name": "research_lab",
        "current_level": max(0, int(current.get(RESEARCH_LAB, 0) or 0)),
        "target_level": 3,
        "satisfied": int(current.get(RESEARCH_LAB, 0) or 0) >= 3,
        "cost": None,
        "executor": "building_queue",
    }]
    for technology_id, name, base, factor, target_level in RESEARCH_CHAIN:
        present = max(0, int(current.get(technology_id, 0) or 0))
        missing_cost = {key: 0 for key in RESOURCE_KEYS}
        for level in range(present + 1, target_level + 1):
            missing_cost = add_resources(
                missing_cost, _research_level_cost(base, factor, level)
            )
        chain.append({
            "kind": "research",
            "technology_id": technology_id,
            "name": name,
            "current_level": present,
            "target_level": target_level,
            "satisfied": present >= target_level,
            "cost": missing_cost,
            "executor": "research_queue",
        })

    astro_present = max(0, int(current.get(ASTROPHYSICS, 0) or 0))
    astro_cost = {key: 0 for key in RESOURCE_KEYS}
    for level in range(astro_present + 1, astro_target + 1):
        astro_cost = add_resources(
            astro_cost,
            _research_level_cost((0, 4_000, 8_000), 1.75, level),
        )
    chain.append({
        "kind": "research",
        "technology_id": ASTROPHYSICS,
        "name": "astrophysics",
        "current_level": astro_present,
        "target_level": astro_target,
        "satisfied": astro_present >= astro_target,
        "cost": astro_cost,
        "executor": "research_queue",
    })
    have_ship = max(0, int(current.get(COLONY_SHIP, 0) or 0))
    chain.append({
        "kind": "ship",
        "technology_id": COLONY_SHIP,
        "name": "colony_ship",
        "current_amount": have_ship,
        "target_amount": 1,
        "satisfied": have_ship >= 1,
        "cost": {key: 0 for key in RESOURCE_KEYS} if have_ship else dict(COLONY_SHIP_COST),
        "executor": "unit_queue",
        "facility_prerequisites": {SHIPYARD: 4, IMPULSE_DRIVE: 3},
    })
    return chain


def build_transfer_request(
    command_id: str,
    exact_target: Mapping[str, Any] | Sequence[Any],
    bootstrap_resources: Mapping[str, Any] | None,
    *,
    source: Mapping[str, Any] | int | None = None,
) -> dict[str, Any]:
    """Return a planned, deliberately unsent request for ``resource_transfer.py``."""
    target = normalize_target(exact_target)
    return {
        "request_id": f"{command_id}:bootstrap",
        "command_id": command_id,
        "status": "planned",
        "phase": "post_colonization_bootstrap",
        "purpose": "colony_bootstrap",
        "source": source,
        "destination": target,
        "exact_target": target,
        "resources": normalize_resources(bootstrap_resources),
        "not_before": "colony_established",
        "send": False,
    }


def _existing_investment(
    investment: Mapping[str, Any] | None,
    weights: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = investment if isinstance(investment, Mapping) else {}
    raw_cost = source.get("cost") if isinstance(source.get("cost"), Mapping) else source
    cost = normalize_resources(raw_cost)
    if not any(cost.values()):
        cost = {"metal": 10_000, "crystal": 5_000, "deuterium": 0}
        cost_source = "conservative_fallback"
    else:
        cost_source = "observed"
    hourly_gain = _first_number(source, "expected_hourly_gain", "hourly_gain")
    if hourly_gain is None:
        hourly_gain = 750.0
        gain_source = "conservative_fallback"
    else:
        hourly_gain = max(0.0, hourly_gain)
        gain_source = "observed"
    setup_hours = max(0.0, _first_number(source, "setup_hours", "delay_hours") or 0.0)
    value = resource_value(cost, weights)
    payback = value / hourly_gain if hourly_gain > 0 else None
    return {
        "name": str(source.get("name") or "existing_planet_investment"),
        "cost": cost,
        "cost_value": round(value, 3),
        "expected_hourly_gain": round(hourly_gain, 3),
        "setup_hours": round(setup_hours, 3),
        "payback_hours": None if payback is None else round(payback, 3),
        "fallbacks": [
            item
            for item, used in (
                ("existing_cost=15000", cost_source != "observed"),
                ("existing_hourly_gain=750", gain_source != "observed"),
            )
            if used
        ],
    }


def plan_colonization(
    account: str,
    candidates: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    levels: Mapping[int, Any] | None = None,
    planet_count: int = 1,
    goal: str = "balanced_expansion",
    role: str = "balanced",
    origin: Mapping[str, Any] | Sequence[Any] | None = None,
    source: Mapping[str, Any] | int | None = None,
    bootstrap_resources: Mapping[str, Any] | None = None,
    existing_investment: Mapping[str, Any] | None = None,
    expected_hourly_gain: float | None = None,
    setup_hours: float = 24.0,
    payback_horizon_hours: float = 24.0 * 30.0,
    resource_weights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose a target and return the complete structured colonization command.

    ``ValueError`` is raised for an empty candidate set because a resumable
    colonization command must always name one exact target.
    """
    ranked = rank_candidates(candidates, role=role, origin=origin)
    if not ranked:
        raise ValueError("at least one exact colonization candidate is required")
    selected = ranked[0]
    target = selected["exact_target"]
    command_id = deterministic_command_id(account, target, goal)
    bootstrap = normalize_resources(
        bootstrap_resources
        if bootstrap_resources is not None
        else DEFAULT_BOOTSTRAP_RESOURCES
    )
    target_planets = max(1, int(planet_count)) + 1
    prerequisites = prerequisite_chain(levels, target_planets=target_planets)
    prerequisite_cost = {key: 0 for key in RESOURCE_KEYS}
    for item in prerequisites:
        if isinstance(item.get("cost"), Mapping):
            prerequisite_cost = add_resources(prerequisite_cost, item["cost"])
    direct_cost = add_resources(bootstrap, prerequisite_cost)

    existing = _existing_investment(existing_investment, resource_weights)
    setup = max(0.0, float(setup_hours))
    horizon = max(setup, float(payback_horizon_hours))
    opportunity_cost = existing["expected_hourly_gain"] * setup
    direct_value = resource_value(direct_cost, resource_weights)
    total_expansion_cost = direct_value + opportunity_cost

    observed_gain = _finite_number(expected_hourly_gain)
    if observed_gain is None:
        # A mature low-tier colony is deliberately estimated below ideal mine output.
        hourly_gain = 2_500.0 + selected["score"] * 3_500.0
        gain_method = "conservative_candidate_score_model"
    else:
        hourly_gain = max(0.0, observed_gain)
        gain_method = "caller_supplied"
    expansion_payback = total_expansion_cost / hourly_gain if hourly_gain > 0 else None
    expansion_net = max(0.0, horizon - setup) * hourly_gain - total_expansion_cost
    existing_net = (
        max(0.0, horizon - existing["setup_hours"])
        * existing["expected_hourly_gain"]
        - existing["cost_value"]
    )
    expand = (
        expansion_payback is not None
        and expansion_payback <= horizon
        and expansion_net >= existing_net
    )

    missing = [item for item in prerequisites if not item["satisfied"]]
    if not expand:
        phase = "deferred"
    elif any(item["kind"] in {"facility", "research"} for item in missing):
        phase = "prerequisites"
    elif any(item["kind"] == "ship" for item in missing):
        phase = "colony_ship"
    else:
        phase = "ready"

    expected_payback = {
        "hours": None if expansion_payback is None else round(expansion_payback, 3),
        "horizon_hours": round(horizon, 3),
        "within_horizon": expansion_payback is not None and expansion_payback <= horizon,
        "setup_hours": round(setup, 3),
        "expected_hourly_gain": round(hourly_gain, 3),
        "gain_method": gain_method,
    }
    transfer_request = build_transfer_request(
        command_id, target, bootstrap, source=source
    )
    return {
        "schema_version": 1,
        "command_id": command_id,
        "phase": phase,
        "exact_target": target,
        "goal": str(goal),
        "role": selected["role"],
        "bootstrap_resources": bootstrap,
        "prerequisites": prerequisites,
        "expected_payback": expected_payback,
        "abort_conditions": [
            {"code": "target_occupied", "condition": "exact target is occupied before dispatch"},
            {"code": "target_state_unknown", "condition": "occupancy cannot be rechecked"},
            {"code": "strategy_cap_reached", "condition": "planet cap reached before dispatch"},
            {"code": "payback_exceeded", "condition": "expected payback exceeds horizon"},
            {"code": "threat_too_high", "condition": "observed threat exceeds accepted policy"},
            {"code": "fleet_slot_unavailable", "condition": "shared fleet reserve cannot be kept"},
        ],
        "costs": {
            "colony_ship_and_missing_research": prerequisite_cost,
            "bootstrap": bootstrap,
            "direct_total": direct_cost,
            "direct_value": round(direct_value, 3),
            "opportunity_cost": round(opportunity_cost, 3),
            "total_value": round(total_expansion_cost, 3),
            "resource_weights": dict(resource_weights or DEFAULT_RESOURCE_WEIGHTS),
        },
        "comparison": {
            "recommendation": "expand" if expand else "invest_existing",
            "expansion_horizon_net_value": round(expansion_net, 3),
            "existing_horizon_net_value": round(existing_net, 3),
            "existing_investment": existing,
        },
        "candidate_score": selected,
        "ranked_candidates": ranked,
        "transfer_request": transfer_request,
    }


make_command_id = deterministic_command_id
make_colonization_plan = plan_colonization
candidate_score = score_candidate

