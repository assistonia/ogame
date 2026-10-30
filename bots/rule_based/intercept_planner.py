"""Return-intercept planning and fail-closed, at-most-once execution.

Existing raid gates are inputs to this executor, not replacements for it.  A
caller must wrap its current espionage/full-intel, player-status, bashing,
combat-profitability, fleet-slot, ship/resource, and ownership decisions in
``InterceptSafetyCallbacks``.  The executor then adds exact phalanx and timing
revalidation immediately before the existing checked fleet API is called.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from bots.rule_based.phalanx_intel import (
    Coordinate,
    FleetMovement,
    ObservationValidationError,
    PhalanxObservation,
    PhalanxObservationCache,
    Timestamp,
    coordinate_dict,
    normalize_coordinate,
)


_LEDGER_VERSION = 1
_DEFAULT_LEDGER_DIR = pathlib.Path(__file__).with_name(".intercept_state")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[pathlib.Path, threading.RLock] = {}
_PUBLIC_STATUSES = {"planned", "waiting", "sent", "rejected", "ambiguous"}
_TERMINAL_STATUSES = {"sent", "rejected", "ambiguous"}


class InterceptError(RuntimeError):
    """Base class for invalid or unsafe intercept operations."""


class InterceptPlanError(InterceptError, ValueError):
    """Raised when an intercept cannot be proven from exact inputs."""


class InterceptLedgerError(InterceptError):
    """Raised when durable at-most-once state cannot be trusted."""


def _number(value: Any, field: str, minimum: float | None = None) -> Timestamp:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InterceptPlanError(f"{field} must be a finite number")
    if not math.isfinite(float(value)):
        raise InterceptPlanError(f"{field} must be a finite number")
    if minimum is not None and float(value) < minimum:
        raise InterceptPlanError(f"{field} must be >= {minimum}")
    return value


def _strict_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InterceptPlanError(f"{field} must be an integer >= {minimum}")
    return value


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise InterceptPlanError("agent_id must be a non-empty string")
    return value.strip()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _ships(value: Mapping[int | str, int], field: str = "ships") -> tuple[tuple[int, int], ...]:
    if not isinstance(value, Mapping):
        raise InterceptPlanError(f"{field} must be a mapping")
    result: dict[int, int] = {}
    for raw_id, raw_count in value.items():
        try:
            ship_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise InterceptPlanError(f"{field} has an invalid technology id") from exc
        if isinstance(raw_id, bool) or ship_id <= 0:
            raise InterceptPlanError(f"{field} has an invalid technology id")
        count = _strict_int(raw_count, f"{field}.{ship_id}", 1)
        if ship_id in result:
            raise InterceptPlanError(f"{field} contains duplicate technology ids")
        result[ship_id] = count
    if not result:
        raise InterceptPlanError(f"{field} must contain at least one ship")
    return tuple(sorted(result.items()))


def _ship_dict(value: tuple[tuple[int, int], ...]) -> dict[int, int]:
    return {ship_id: count for ship_id, count in value}


def _reason(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


@dataclass(frozen=True)
class JumpGateStagingRequest:
    source_moon: Coordinate
    ships: Mapping[int | str, int]
    lead_seconds: int = 30
    latest_margin_seconds: int = 5


@dataclass(frozen=True)
class JumpGateStagingPlan:
    command_id: str
    source_moon: Coordinate
    destination_moon: Coordinate
    ships: tuple[tuple[int, int], ...]
    execute_at: Timestamp
    latest_at: Timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "source_moon": coordinate_dict(self.source_moon),
            "destination_moon": coordinate_dict(self.destination_moon),
            "ships": {str(key): value for key, value in self.ships},
            "execute_at": self.execute_at,
            "latest_at": self.latest_at,
        }


@dataclass(frozen=True)
class ReturnInterceptPlan:
    version: int
    command_id: str
    agent_id: str
    created_at: Timestamp
    observation_id: str
    observed_server_time: Timestamp
    observation_expires_at: Timestamp
    movement_id: str
    movement_fingerprint: str
    hostile_owner_id: str
    source_coordinates: Coordinate
    target_coordinates: Coordinate
    target_type: int
    ships: tuple[tuple[int, int], ...]
    speed: int | float
    own_flight_seconds: int
    hostile_return_eta: Timestamp
    desired_impact_at: Timestamp
    dispatch_at: Timestamp
    dispatch_margin_seconds: int
    send_window_seconds: int
    max_phalanx_age_seconds: int
    jump_gate_staging: JumpGateStagingPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "command_id": self.command_id,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "observation_id": self.observation_id,
            "observed_server_time": self.observed_server_time,
            "observation_expires_at": self.observation_expires_at,
            "movement_id": self.movement_id,
            "movement_fingerprint": self.movement_fingerprint,
            "hostile_owner_id": self.hostile_owner_id,
            "source_coordinates": coordinate_dict(self.source_coordinates),
            "target_coordinates": coordinate_dict(self.target_coordinates),
            "target_type": self.target_type,
            "ships": {str(key): value for key, value in self.ships},
            "speed": self.speed,
            "own_flight_seconds": self.own_flight_seconds,
            "hostile_return_eta": self.hostile_return_eta,
            "desired_impact_at": self.desired_impact_at,
            "dispatch_at": self.dispatch_at,
            "dispatch_margin_seconds": self.dispatch_margin_seconds,
            "send_window_seconds": self.send_window_seconds,
            "max_phalanx_age_seconds": self.max_phalanx_age_seconds,
            "jump_gate_staging": (
                None if self.jump_gate_staging is None else self.jump_gate_staging.to_dict()
            ),
        }

    @property
    def latest_dispatch_at(self) -> float:
        return float(self.dispatch_at) + self.send_window_seconds


def normalize_intercept_timing(plan: ReturnInterceptPlan) -> ReturnInterceptPlan:
    """Return a timing-normalized plan or raise before any timing field is used.

    Structured plans can instantiate the frozen dataclasses without runtime type
    enforcement.  Keep that untrusted boundary here: booleans, strings, NaN/inf,
    fractional integer fields, negative durations, and inconsistent jump/dispatch
    windows are rejected rather than reaching ``float(...)`` in the executor.
    """

    if not isinstance(plan, ReturnInterceptPlan):
        raise InterceptPlanError("ReturnInterceptPlan is required")

    created_at = float(_number(plan.created_at, "created_at", 0))
    observed_server_time = float(
        _number(plan.observed_server_time, "observed_server_time", 0)
    )
    observation_expires_at = float(
        _number(plan.observation_expires_at, "observation_expires_at", 0)
    )
    hostile_return_eta = float(
        _number(plan.hostile_return_eta, "hostile_return_eta", 0)
    )
    desired_impact_at = float(
        _number(plan.desired_impact_at, "desired_impact_at", 0)
    )
    dispatch_at = float(_number(plan.dispatch_at, "dispatch_at", 0))
    speed = float(_number(plan.speed, "speed", 1))
    if speed > 100:
        raise InterceptPlanError("speed must be in the fleet API's 1..100 range")
    own_flight_seconds = _strict_int(
        plan.own_flight_seconds, "own_flight_seconds", 1
    )
    dispatch_margin_seconds = _strict_int(
        plan.dispatch_margin_seconds, "dispatch_margin_seconds", 0
    )
    send_window_seconds = _strict_int(
        plan.send_window_seconds, "send_window_seconds", 0
    )
    max_phalanx_age_seconds = _strict_int(
        plan.max_phalanx_age_seconds, "max_phalanx_age_seconds", 1
    )

    if observation_expires_at < observed_server_time:
        raise InterceptPlanError("observation expiry precedes its server time")
    if hostile_return_eta > desired_impact_at:
        raise InterceptPlanError("desired impact precedes the hostile return")
    if dispatch_at < created_at or dispatch_at >= desired_impact_at:
        raise InterceptPlanError("dispatch time is inconsistent with plan creation/impact")
    calculated_impact = (
        dispatch_at + own_flight_seconds + dispatch_margin_seconds
    )
    if not math.isclose(calculated_impact, desired_impact_at, abs_tol=1e-6):
        raise InterceptPlanError("dispatch timing does not reproduce desired impact")

    stage = plan.jump_gate_staging
    normalized_stage = None
    if stage is not None:
        if not isinstance(stage, JumpGateStagingPlan):
            raise InterceptPlanError("jump_gate_staging must be JumpGateStagingPlan")
        execute_at = float(_number(stage.execute_at, "jump_gate.execute_at", 0))
        latest_at = float(_number(stage.latest_at, "jump_gate.latest_at", 0))
        if execute_at > latest_at:
            raise InterceptPlanError("jump gate execute time exceeds its latest time")
        if latest_at > dispatch_at:
            raise InterceptPlanError("jump gate latest time exceeds intercept dispatch")
        normalized_stage = replace(
            stage,
            execute_at=execute_at,
            latest_at=latest_at,
        )

    return replace(
        plan,
        created_at=created_at,
        observed_server_time=observed_server_time,
        observation_expires_at=observation_expires_at,
        speed=speed,
        own_flight_seconds=own_flight_seconds,
        hostile_return_eta=hostile_return_eta,
        desired_impact_at=desired_impact_at,
        dispatch_at=dispatch_at,
        dispatch_margin_seconds=dispatch_margin_seconds,
        send_window_seconds=send_window_seconds,
        max_phalanx_age_seconds=max_phalanx_age_seconds,
        jump_gate_staging=normalized_stage,
    )


def _movement_for_return(
    observation: PhalanxObservation,
    movement_id: str,
    *,
    server_now: Timestamp,
    max_age_seconds: int,
) -> FleetMovement:
    if not isinstance(observation, PhalanxObservation):
        raise InterceptPlanError("an exact PhalanxObservation is required")
    try:
        fresh = observation.is_fresh(server_now, max_age_seconds)
    except ObservationValidationError as exc:
        raise InterceptPlanError(str(exc)) from exc
    if not fresh:
        raise InterceptPlanError("phalanx observation is stale, expired, or future-dated")
    movement = observation.exact_movement(movement_id)
    if movement is None:
        raise InterceptPlanError("exact movement is absent; absence is not safety evidence")
    if movement.hostile is not True:
        raise InterceptPlanError("movement is not explicitly marked hostile")
    if movement.direction != "return" or movement.state != "returning":
        raise InterceptPlanError("movement is not explicitly on its return flight")
    if movement.return_eta is None:
        raise InterceptPlanError("return ETA was not exposed")
    if movement.owner_id is None:
        raise InterceptPlanError("hostile owner id was not exposed")
    if float(movement.return_eta) <= float(server_now):
        raise InterceptPlanError("hostile return ETA has already passed")
    return movement


def plan_return_intercept(
    *,
    agent_id: str,
    observation: PhalanxObservation,
    movement_id: str,
    source_coordinates: Coordinate,
    target_type: int,
    ships: Mapping[int | str, int],
    own_flight_seconds: int,
    server_now: Timestamp,
    speed: int | float = 10,
    impact_after_return_seconds: int = 1,
    dispatch_margin_seconds: int = 2,
    send_window_seconds: int = 2,
    max_phalanx_age_seconds: int = 30,
    jump_gate_staging: JumpGateStagingRequest | None = None,
) -> ReturnInterceptPlan:
    """Create a deterministic attack timed against one exact return ETA."""

    agent = _agent_id(agent_id)
    now = _number(server_now, "server_now", 0)
    flight = _strict_int(own_flight_seconds, "own_flight_seconds", 1)
    after_return = _strict_int(
        impact_after_return_seconds, "impact_after_return_seconds", 0
    )
    margin = _strict_int(dispatch_margin_seconds, "dispatch_margin_seconds", 0)
    window = _strict_int(send_window_seconds, "send_window_seconds", 0)
    max_age = _strict_int(max_phalanx_age_seconds, "max_phalanx_age_seconds", 1)
    source = normalize_coordinate(source_coordinates, "source_coordinates")
    if isinstance(target_type, bool) or target_type not in (1, 3):
        raise InterceptPlanError("target_type must explicitly identify a planet or moon")
    normalized_ships = _ships(ships)
    normalized_speed = _number(speed, "speed", 1)
    if float(normalized_speed) > 100:
        raise InterceptPlanError("speed must be in the fleet API's 1..100 range")
    movement = _movement_for_return(
        observation, movement_id, server_now=now, max_age_seconds=max_age
    )
    target = movement.source
    if movement.source_type is not None and movement.source_type != target_type:
        raise InterceptPlanError("target_type conflicts with the observed return destination")
    desired = float(movement.return_eta) + after_return
    dispatch_at = desired - flight - margin
    if dispatch_at < float(now):
        raise InterceptPlanError("dispatch time is already missed")

    identity = {
        "version": _LEDGER_VERSION,
        "agent_id": agent,
        "movement_id": movement.movement_id,
        "movement_fingerprint": movement.fingerprint,
        "hostile_owner_id": movement.owner_id,
        "hostile_return_eta": movement.return_eta,
        "source_coordinates": coordinate_dict(source),
        "target_coordinates": coordinate_dict(target),
        "target_type": target_type,
        "ships": {str(key): value for key, value in normalized_ships},
        "speed": normalized_speed,
        "own_flight_seconds": flight,
        "desired_impact_at": desired,
        "dispatch_at": dispatch_at,
        "dispatch_margin_seconds": margin,
        "send_window_seconds": window,
    }
    command_id = "intercept:" + hashlib.sha256(_canonical(identity)).hexdigest()[:32]
    stage_plan = None
    if jump_gate_staging is not None:
        if not isinstance(jump_gate_staging, JumpGateStagingRequest):
            raise InterceptPlanError("jump_gate_staging must be JumpGateStagingRequest")
        stage_source = normalize_coordinate(
            jump_gate_staging.source_moon, "jump_gate.source_moon"
        )
        if stage_source == source:
            raise InterceptPlanError("jump gate staging source and destination are identical")
        stage_ships = _ships(jump_gate_staging.ships, "jump_gate.ships")
        if any(dict(stage_ships).get(ship_id, 0) < count for ship_id, count in normalized_ships):
            raise InterceptPlanError("jump gate staging does not include the intercept fleet")
        lead = _strict_int(jump_gate_staging.lead_seconds, "jump_gate.lead_seconds", 1)
        latest_margin = _strict_int(
            jump_gate_staging.latest_margin_seconds,
            "jump_gate.latest_margin_seconds",
            1,
        )
        latest_at = dispatch_at - latest_margin
        execute_at = dispatch_at - lead
        if latest_at < float(now) or execute_at > latest_at:
            raise InterceptPlanError("jump gate staging window is already invalid")
        stage_identity = {
            "parent_command_id": command_id,
            "source_moon": coordinate_dict(stage_source),
            "destination_moon": coordinate_dict(source),
            "ships": {str(key): value for key, value in stage_ships},
            "execute_at": execute_at,
            "latest_at": latest_at,
        }
        stage_plan = JumpGateStagingPlan(
            command_id="jump-stage:"
            + hashlib.sha256(_canonical(stage_identity)).hexdigest()[:32],
            source_moon=stage_source,
            destination_moon=source,
            ships=stage_ships,
            execute_at=execute_at,
            latest_at=latest_at,
        )
    return ReturnInterceptPlan(
        version=_LEDGER_VERSION,
        command_id=command_id,
        agent_id=agent,
        created_at=now,
        observation_id=observation.observation_id,
        observed_server_time=observation.server_time,
        observation_expires_at=observation.expires_at,
        movement_id=movement.movement_id,
        movement_fingerprint=movement.fingerprint,
        hostile_owner_id=movement.owner_id,
        source_coordinates=source,
        target_coordinates=target,
        target_type=target_type,
        ships=normalized_ships,
        speed=normalized_speed,
        own_flight_seconds=flight,
        hostile_return_eta=movement.return_eta,
        desired_impact_at=desired,
        dispatch_at=dispatch_at,
        dispatch_margin_seconds=margin,
        send_window_seconds=window,
        max_phalanx_age_seconds=max_age,
        jump_gate_staging=stage_plan,
    )


@dataclass(frozen=True)
class GateDecision:
    """An explicit result from one existing safety gate."""

    passed: bool
    checked_at: Timestamp
    code: str
    retryable: bool = False
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TimingRevalidation:
    """Authoritative send-boundary timing and route calculation."""

    server_time: Timestamp
    own_flight_seconds: int
    source_coordinates: Coordinate
    target_coordinates: Coordinate
    dispatch_ready: bool


GateCallback = Callable[[Any, ReturnInterceptPlan], GateDecision]
PhalanxCallback = Callable[[Any, ReturnInterceptPlan], PhalanxObservation]
TimingCallback = Callable[[Any, ReturnInterceptPlan], TimingRevalidation]


@dataclass(frozen=True)
class InterceptSafetyCallbacks:
    """Mandatory adapters around all pre-existing raid safety gates."""

    espionage_full_intel: GateCallback
    admin_vacation_banned: GateCallback
    bashing: GateCallback
    combat_profitability: GateCallback
    fleet_slot: GateCallback
    ship_resource: GateCallback
    target_ownership: GateCallback
    phalanx_revalidation: PhalanxCallback
    timing_revalidation: TimingCallback
    jump_gate_ready: GateCallback | None = None


def raider_full_intel_decision(
    report: Mapping[str, Any] | None,
    *,
    checked_at: Timestamp,
    retryable: bool = True,
) -> GateDecision:
    """Adapter that reuses, rather than reimplements, raider's full-intel gate."""

    from bots.rule_based import raider

    passed = bool(raider._full_intel_report(report))
    return GateDecision(
        passed=passed,
        checked_at=checked_at,
        code="full_intel" if passed else "full_intel_required",
        retryable=retryable and not passed,
        evidence={"existing_raider_gate": "_full_intel_report"},
    )


def raider_bashing_decision(
    state: dict[str, Any],
    target: Coordinate,
    *,
    checked_at: Timestamp,
    maximum_attacks: int,
) -> GateDecision:
    """Adapter around raider's existing rolling bashing gate."""

    from bots.rule_based import raider

    limited = bool(
        raider._bashing_limited(
            state,
            normalize_coordinate(target, "target"),
            float(checked_at),
            int(maximum_attacks),
        )
    )
    return GateDecision(
        passed=not limited,
        checked_at=checked_at,
        code="bashing_clear" if not limited else "bashing_limited",
        retryable=limited,
        evidence={"existing_raider_gate": "_bashing_limited"},
    )


def build_intercept_safety_callbacks(
    client: Any,
    email: str,
    plan: ReturnInterceptPlan,
    *,
    raider_module: Any = None,
    context: Mapping[str, Any] | None = None,
) -> InterceptSafetyCallbacks:
    """Adapt the existing raider gates to one explicit intercept.

    ``context`` must provide ``levels``.  It may tighten the normal raider defaults
    with ``min_loot``, ``max_active_missions``, ``bashing_max_attacks``,
    ``attack_cooldown_sec``, ``fleet_risk_percent``,
    ``min_fleet_value_limit``, ``allow_defended_targets``,
    ``allow_strong_targets``, and ``excluded_systems``.  The existing asynchronous
    raid preflight request can be supplied as ``preflight_request``; otherwise it is
    read from raider state and its absence rejects the intercept.

    ``phalanx_revalidation`` and ``timing_revalidation`` may be callables for a
    stricter caller-owned recheck.  Without them, the adapter uses the exact phalanx
    cache and raider route calculation.  A staged jump additionally requires a
    ``jump_gate_ready`` GateDecision callback.  Phalanx data is consulted only by the
    movement/timing callback and is never an input to combat evaluation.
    """

    normalized_plan = normalize_intercept_timing(plan)
    ctx: Mapping[str, Any] = context if isinstance(context, Mapping) else {}
    if raider_module is None:
        from bots.rule_based import raider as raider_module
    raider = raider_module
    target = normalized_plan.target_coordinates
    combat_state: dict[str, Any] = {}

    def rejected(code: str, gate: str, exc: Exception | None = None) -> GateDecision:
        evidence: dict[str, Any] = {
            "adapter": "build_intercept_safety_callbacks",
            "gate": gate,
        }
        if exc is not None:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
        return GateDecision(False, time.time(), code, False, evidence)

    def passed(code: str, gate: str, **evidence: Any) -> GateDecision:
        return GateDecision(
            True,
            time.time(),
            code,
            False,
            {
                "adapter": "build_intercept_safety_callbacks",
                "gate": gate,
                **evidence,
            },
        )

    def checked_plan(candidate: ReturnInterceptPlan) -> ReturnInterceptPlan:
        normalized = normalize_intercept_timing(candidate)
        if normalized.command_id != normalized_plan.command_id:
            raise InterceptPlanError("safety callback received another intercept")
        return normalized

    def levels() -> dict[int, int]:
        raw = ctx.get("levels")
        if not isinstance(raw, Mapping):
            raise InterceptPlanError("current levels/ship snapshot is required")
        result: dict[int, int] = {}
        for raw_id, raw_amount in raw.items():
            ship_id = int(raw_id)
            amount = int(raw_amount)
            if isinstance(raw_id, bool) or isinstance(raw_amount, bool) or amount < 0:
                raise InterceptPlanError("current levels snapshot is malformed")
            result[ship_id] = amount
        return result

    def context_int(name: str, default: int, minimum: int = 0) -> int:
        value = ctx.get(name, default)
        return _strict_int(value, f"context.{name}", minimum)

    def context_number(name: str, default: float, minimum: float = 0) -> float:
        return float(_number(ctx.get(name, default), f"context.{name}", minimum))

    def current_report() -> Mapping[str, Any] | None:
        reports = raider.read_spy_reports(email)
        if not isinstance(reports, Mapping):
            raise InterceptPlanError("raider spy report reader is unavailable")
        report = reports.get(target)
        return report if isinstance(report, Mapping) else None

    def current_target_row(candidate: ReturnInterceptPlan) -> Mapping[str, Any]:
        rows = raider.fleet.scan_galaxy(client, target[0], target[1])
        if not isinstance(rows, list):
            raise InterceptPlanError("live target scan was not structured")
        matching = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                coord = (int(row["galaxy"]), int(row["system"]), int(row["position"]))
            except (KeyError, TypeError, ValueError):
                continue
            if coord == target:
                matching.append(row)
        if len(matching) != 1:
            raise InterceptPlanError("target is absent or ambiguous in live galaxy state")
        row = matching[0]
        excluded = ctx.get("excluded_systems")
        if excluded is not None and not isinstance(excluded, (set, frozenset)):
            raise InterceptPlanError("excluded_systems must be an exact set")
        if not raider._is_biddable(
            row,
            allow_strong_targets=bool(ctx.get("allow_strong_targets", True)),
            excluded_systems=excluded,
        ):
            raise InterceptPlanError("target is not legal under the raider target gate")
        if candidate.target_type == 3 and row.get("has_moon") is not True:
            raise InterceptPlanError("planned target moon is not present")
        return row

    def espionage_full_intel(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> GateDecision:
        try:
            checked_plan(candidate)
            report = current_report()
            if not raider._full_intel_report(report):
                return rejected("full_intel_required", "espionage_full_intel")
            return passed(
                "full_intel",
                "espionage_full_intel",
                existing_raider_gate="_full_intel_report",
            )
        except Exception as exc:
            return rejected("full_intel_unavailable", "espionage_full_intel", exc)

    def admin_vacation_banned(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> GateDecision:
        try:
            checked_plan(candidate)
            current_target_row(candidate)
            return passed(
                "target_legal",
                "admin_vacation_banned",
                existing_raider_gate="_is_biddable",
            )
        except Exception as exc:
            return rejected("target_legal_state_unavailable", "admin_vacation_banned", exc)

    def bashing(_client: Any, candidate: ReturnInterceptPlan) -> GateDecision:
        try:
            checked_plan(candidate)
            now = time.time()
            state = raider._load_state(email)
            if not isinstance(state, dict):
                raise InterceptPlanError("raider attack state is unavailable")
            maximum = context_int(
                "bashing_max_attacks", int(raider.BASH_MAX_ATTACKS), 0
            )
            if raider._bashing_limited(state, target, now, maximum):
                return rejected("bashing_limited", "bashing")
            cooldown = context_int(
                "attack_cooldown_sec", int(raider.ATTACK_COOLDOWN_SEC), 0
            )
            if now - float(raider._last(state, target, "attack")) < cooldown:
                return rejected("attack_cooldown", "bashing")
            return passed(
                "bashing_and_cooldown_clear",
                "bashing",
                existing_raider_gates="_bashing_limited,_last",
            )
        except Exception as exc:
            return rejected("bashing_state_unavailable", "bashing", exc)

    def combat_profitability(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> GateDecision:
        try:
            current = checked_plan(candidate)
            current_levels = levels()
            ships = _ship_dict(current.ships)
            report = current_report()
            allow_defended = bool(ctx.get("allow_defended_targets", False))
            eligible = (
                raider._full_intel_report(report)
                if allow_defended
                else raider._safe_zero_defense_report(report)
            )
            if not eligible or report is None:
                return rejected("combat_intel_ineligible", "combat_profitability")
            peak = raider.read_spy_combat_peak(email, target)
            if not isinstance(peak, Mapping):
                return rejected("espionage_combat_peak_unavailable", "combat_profitability")
            loot_percent = context_int(
                "loot_percent", int(raider.raid_loot_percent(email)), 0
            )
            if loot_percent > 100:
                raise InterceptPlanError("context.loot_percent must be <= 100")
            loot = int(raider.expected_raid_loot(report, loot_percent))
            have_rec = int(current_levels.get(raider.RECYCLER, 0))
            default_debris_factor = (
                1.0 if have_rec > 0 else float(raider.DEBRIS_NO_RECYCLER_FACTOR)
            )
            debris_factor = context_number(
                "debris_factor", default_debris_factor, 0
            )
            if debris_factor > 1:
                raise InterceptPlanError("context.debris_factor must be <= 1")
            debris_fn = (
                raider.all_ship_debris_value
                if allow_defended
                else raider.ship_debris_value
            )
            value = loot + int(debris_fn(report.get("ships") or {}) * debris_factor)
            minimum = context_int("min_loot", int(raider.MIN_EXPECTED_LOOT), 0)
            if value < minimum:
                return rejected("intercept_unprofitable", "combat_profitability")
            defender = raider._merge_combat_peak(
                raider._merge_defender_units(dict(report)), peak
            )
            realized_loot = min(loot, int(raider._sizing_cargo_capacity(ships)))
            decision = raider.battle_cache.cached_decide(
                attacker=ships,
                defender=defender,
                loot_estimate=realized_loot,
                has_recycler=have_rec > 0,
                att_tech=current_levels,
                def_tech=report["research"],
                mode="log",
                samples=int(raider.SIZING_BAND_SAMPLES),
                seed=int(raider.SIZING_SEED),
            )
            if not isinstance(decision, dict) or decision.get("recommend") is not True:
                return rejected("combat_recommendation_rejected", "combat_profitability")
            risk_percent = context_number(
                "fleet_risk_percent",
                float(raider.PREFLIGHT_FLEET_VALUE_RISK_PERCENT),
                0,
            )
            minimum_limit = context_int(
                "min_fleet_value_limit",
                int(raider.PREFLIGHT_MIN_FLEET_VALUE_LIMIT),
                0,
            )
            budget = raider._preflight_fleet_value_budget(
                email,
                risk_percent=risk_percent,
                minimum_limit=minimum_limit,
            )
            if not isinstance(budget, tuple) or len(budget) != 2:
                return rejected("fleet_value_budget_unavailable", "combat_profitability")
            fleet_value = int(raider.battle_sim.fleet_cost_value(ships))
            if fleet_value > int(budget[0]):
                return rejected("fleet_value_risk_limit", "combat_profitability")
            combat_state.clear()
            combat_state.update(
                {
                    "report": dict(report),
                    "peak": {int(key): int(amount) for key, amount in peak.items()},
                    "decision": decision,
                    "ships": ships,
                    "levels": current_levels,
                    "have_rec": have_rec,
                    "min_loot": minimum,
                    "debris_factor": debris_factor,
                    "max_active_missions": context_int(
                        "max_active_missions", int(raider.MAX_ACTIVE_MISSIONS), 1
                    ),
                    "attack_cooldown_sec": context_int(
                        "attack_cooldown_sec", int(raider.ATTACK_COOLDOWN_SEC), 0
                    ),
                    "allow_defended_targets": allow_defended,
                    "loot_percent": loot_percent,
                }
            )
            return passed(
                "combat_profitable",
                "combat_profitability",
                existing_raider_gates=(
                    "_full_intel_report,_safe_zero_defense_report,"
                    "read_spy_combat_peak,battle_cache.cached_decide,"
                    "_preflight_fleet_value_budget"
                ),
            )
        except Exception as exc:
            return rejected("combat_profitability_unavailable", "combat_profitability", exc)

    def fleet_slot(_client: Any, candidate: ReturnInterceptPlan) -> GateDecision:
        try:
            checked_plan(candidate)
            info = raider._active_fleet_info(client)
            if (
                not isinstance(info, tuple)
                or len(info) != 3
                or any(isinstance(value, bool) or not isinstance(value, int) for value in info)
                or any(value < 0 for value in info)
            ):
                raise InterceptPlanError("active fleet state is not exact")
            active, attacking, _harvesting = info
            maximum = context_int(
                "max_active_missions", int(raider.MAX_ACTIVE_MISSIONS), 1
            )
            if active >= maximum:
                return rejected("fleet_slot_unavailable", "fleet_slot")
            if attacking:
                return rejected("attack_already_active", "fleet_slot")
            return passed(
                "fleet_slot_available",
                "fleet_slot",
                existing_raider_gate="_active_fleet_info",
            )
        except Exception as exc:
            return rejected("fleet_slot_state_unavailable", "fleet_slot", exc)

    def ship_resource(_client: Any, candidate: ReturnInterceptPlan) -> GateDecision:
        try:
            current = checked_plan(candidate)
            current_levels = levels()
            ships = _ship_dict(current.ships)
            if not raider._fleet_available(ships, current_levels):
                return rejected("intercept_ships_unavailable", "ship_resource")
            fuel_table = getattr(raider.battle_sim, "_FUEL", None)
            if not isinstance(fuel_table, Mapping) or any(
                int(ship_id) not in fuel_table for ship_id in ships
            ):
                return rejected("ship_fuel_data_unavailable", "ship_resource")
            status = raider.db_reader.get_status_db(email)
            if not isinstance(status, Mapping):
                raise InterceptPlanError("current resource state is unavailable")
            distance = int(
                raider.battle_sim.flight_distance(
                    current.source_coordinates, current.target_coordinates
                )
            )
            if distance <= 0:
                raise InterceptPlanError("intercept route distance is unavailable")
            fuel = int(math.ceil(raider.battle_sim.fuel_estimate(ships, distance)))
            deuterium = status.get("deuterium")
            if isinstance(deuterium, bool) or not isinstance(deuterium, (int, float)):
                raise InterceptPlanError("current deuterium is unavailable")
            if float(deuterium) < fuel:
                return rejected("intercept_fuel_unavailable", "ship_resource")
            return passed(
                "ships_and_resources_available",
                "ship_resource",
                existing_raider_gates="_fleet_available,battle_sim.fuel_estimate",
                fuel_estimate=fuel,
            )
        except Exception as exc:
            return rejected("ship_resource_state_unavailable", "ship_resource", exc)

    def target_ownership(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> GateDecision:
        try:
            current = checked_plan(candidate)
            row = current_target_row(current)
            owner = row.get("player_id")
            if owner is None or str(owner) != current.hostile_owner_id:
                return rejected("target_owner_changed", "target_ownership")
            own_planets = raider.db_reader.list_planets_db(email)
            if not isinstance(own_planets, list):
                raise InterceptPlanError("owned planet state is unavailable")
            owned_coordinates = {
                (int(item["galaxy"]), int(item["system"]), int(item["position"]))
                for item in own_planets
                if isinstance(item, Mapping)
            }
            if current.source_coordinates not in owned_coordinates:
                return rejected("dispatch_source_not_owned", "target_ownership")
            if current.target_coordinates in owned_coordinates:
                return rejected("target_is_owned", "target_ownership")
            current_source = raider.own_coords(client)
            if current_source != current.source_coordinates:
                return rejected("dispatch_source_changed", "target_ownership")

            if not combat_state:
                return rejected("combat_preflight_state_unavailable", "target_ownership")
            state = raider._load_state(email)
            if not isinstance(state, dict):
                raise InterceptPlanError("raider attack state is unavailable")
            request = ctx.get("preflight_request")
            if request is None:
                request = raider._preflight_request(state, target)
            if not isinstance(request, dict):
                return rejected("raid_preflight_request_required", "target_ownership")
            latest_report, revalidated = raider._revalidate_preflight_dispatch(
                client,
                email,
                target,
                request,
                combat_state["report"],
                combat_state["peak"],
                combat_state["decision"],
                combat_state["ships"],
                combat_state["levels"],
                state,
                time.time(),
                have_rec=combat_state["have_rec"],
                min_loot=combat_state["min_loot"],
                debris_factor=combat_state["debris_factor"],
                max_active_missions=combat_state["max_active_missions"],
                attack_cooldown_sec=combat_state["attack_cooldown_sec"],
                allow_defended_targets=combat_state["allow_defended_targets"],
                loot_percent=combat_state["loot_percent"],
            )
            if latest_report is None or not isinstance(revalidated, dict):
                return rejected("raid_preflight_revalidation_failed", "target_ownership")

            check = raider.fleet.check_target(
                client,
                target,
                ships=combat_state["ships"],
                target_type=current.target_type,
            )
            if not isinstance(check, Mapping) or check.get("status") != "success":
                return rejected("immediate_target_check_failed", "target_ownership")
            orders = check.get("orders")
            allowed = None
            if isinstance(orders, Mapping):
                allowed = orders.get(str(raider.fleet.MISSION_ATTACK))
                if allowed is None:
                    allowed = orders.get(raider.fleet.MISSION_ATTACK)
            if allowed is not True:
                return rejected("attack_mission_not_explicitly_legal", "target_ownership")

            stricter = ctx.get("immediate_target_revalidation")
            if stricter is not None:
                if not callable(stricter):
                    raise InterceptPlanError(
                        "context.immediate_target_revalidation must be callable"
                    )
                decision = stricter(client, current)
                if not isinstance(decision, GateDecision) or decision.passed is not True:
                    return rejected("caller_target_revalidation_failed", "target_ownership")
            return passed(
                "target_ownership_and_preflight_valid",
                "target_ownership",
                existing_raider_gate="_revalidate_preflight_dispatch",
            )
        except Exception as exc:
            return rejected("target_preflight_unavailable", "target_ownership", exc)

    def phalanx_revalidation(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> PhalanxObservation:
        current = checked_plan(candidate)
        stricter = ctx.get("phalanx_revalidation")
        if stricter is not None:
            if not callable(stricter):
                raise InterceptPlanError("context.phalanx_revalidation must be callable")
            observation = stricter(client, current)
            if not isinstance(observation, PhalanxObservation):
                raise InterceptPlanError("caller phalanx revalidation is unavailable")
            return observation
        source_moon = ctx.get("phalanx_source_coordinates")
        cache = PhalanxObservationCache(email)
        observation = cache.latest_exact(
            current.target_coordinates,
            server_now=time.time(),
            max_age_seconds=current.max_phalanx_age_seconds,
            source_moon=source_moon,
        )
        if observation is None:
            raise InterceptPlanError("fresh exact phalanx reobservation is unavailable")
        return observation

    def timing_revalidation(
        _client: Any, candidate: ReturnInterceptPlan
    ) -> TimingRevalidation:
        current = checked_plan(candidate)
        stricter = ctx.get("timing_revalidation")
        if stricter is not None:
            if not callable(stricter):
                raise InterceptPlanError("context.timing_revalidation must be callable")
            timing = stricter(client, current)
            if not isinstance(timing, TimingRevalidation):
                raise InterceptPlanError("caller timing revalidation is unavailable")
            return timing
        source = raider.own_coords(client)
        if source != current.source_coordinates:
            raise InterceptPlanError("dispatch source changed")
        current_levels = levels()
        flight = raider._attack_eta_seconds(
            source,
            current.target_coordinates,
            _ship_dict(current.ships),
            current_levels,
        )
        if isinstance(flight, bool) or not isinstance(flight, int) or flight <= 0:
            raise InterceptPlanError("authoritative route timing is unavailable")
        now = time.time()
        return TimingRevalidation(
            server_time=now,
            own_flight_seconds=flight,
            source_coordinates=source,
            target_coordinates=current.target_coordinates,
            dispatch_ready=(current.dispatch_at <= now <= current.latest_dispatch_at),
        )

    jump_gate_ready = None
    if normalized_plan.jump_gate_staging is not None:
        def jump_gate_ready(
            _client: Any, candidate: ReturnInterceptPlan
        ) -> GateDecision:
            try:
                current = checked_plan(candidate)
                stricter = ctx.get("jump_gate_ready")
                if not callable(stricter):
                    return rejected("jump_gate_state_unavailable", "jump_gate_ready")
                decision = stricter(client, current)
                if not isinstance(decision, GateDecision):
                    return rejected("jump_gate_state_unavailable", "jump_gate_ready")
                return decision
            except Exception as exc:
                return rejected("jump_gate_state_unavailable", "jump_gate_ready", exc)

    return InterceptSafetyCallbacks(
        espionage_full_intel=espionage_full_intel,
        admin_vacation_banned=admin_vacation_banned,
        bashing=bashing,
        combat_profitability=combat_profitability,
        fleet_slot=fleet_slot,
        ship_resource=ship_resource,
        target_ownership=target_ownership,
        phalanx_revalidation=phalanx_revalidation,
        timing_revalidation=timing_revalidation,
        jump_gate_ready=jump_gate_ready,
    )


def _agent_digest(agent_id: str) -> str:
    return hashlib.sha256(_agent_id(agent_id).encode("utf-8")).hexdigest()[:24]


def _ledger_path(agent_id: str, ledger_dir: pathlib.Path | str | None) -> pathlib.Path:
    root = pathlib.Path(ledger_dir) if ledger_dir is not None else _DEFAULT_LEDGER_DIR
    return root / f"{_agent_digest(agent_id)}.json"


def _thread_lock(path: pathlib.Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


@contextlib.contextmanager
def _serialized(path: pathlib.Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _thread_lock(path):
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except InterceptLedgerError:
        raise
    except OSError as exc:
        raise InterceptLedgerError(f"ledger lock unavailable: {exc}") from exc


def _atomic_write(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise InterceptLedgerError(f"atomic ledger write failed: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _empty_ledger(agent_id: str) -> dict[str, Any]:
    return {"version": _LEDGER_VERSION, "agent_id": agent_id, "commands": {}}


def _load_ledger(path: pathlib.Path, agent_id: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_ledger(agent_id)
    try:
        with path.open("r", encoding="ascii") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InterceptLedgerError(f"ledger is unreadable; refusing empty fallback: {exc}") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != _LEDGER_VERSION
        or data.get("agent_id") != agent_id
        or not isinstance(data.get("commands"), dict)
    ):
        raise InterceptLedgerError("ledger header is invalid or belongs to another agent")
    for command_id, record in data["commands"].items():
        if (
            not isinstance(command_id, str)
            or not isinstance(record, dict)
            or not isinstance(record.get("plan"), dict)
            or record.get("status") not in _PUBLIC_STATUSES | {"sending"}
        ):
            raise InterceptLedgerError("ledger contains an invalid command record")
    return data


def _outcome(record: Mapping[str, Any], *, override_status: str | None = None) -> dict[str, Any]:
    status = override_status or record["status"]
    if status == "sending":
        status = "ambiguous"
    return {
        "command_id": record["command_id"],
        "status": status,
        "reason": record.get("reason"),
        "retry_safe": status in {"planned", "waiting"},
        "stage": record.get("stage"),
        "dispatch": record.get("dispatch"),
    }


def _transition(
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    status: str,
    *,
    reason: Mapping[str, Any] | None = None,
    **updates: Any,
) -> None:
    if status not in _PUBLIC_STATUSES | {"sending"}:
        raise InterceptLedgerError("invalid ledger transition")
    record.update(updates)
    record["status"] = status
    record["reason"] = reason
    record["updated_at"] = time.time()
    _atomic_write(path, ledger)


def _new_record(plan: ReturnInterceptPlan) -> dict[str, Any]:
    stage = None
    if plan.jump_gate_staging is not None:
        stage = {
            "command_id": plan.jump_gate_staging.command_id,
            "status": "planned",
            "reason": None,
        }
    return {
        "command_id": plan.command_id,
        "status": "planned",
        "reason": _reason("planned", "intercept is durably planned"),
        "plan": plan.to_dict(),
        "stage": stage,
        "dispatch": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def record_intercept_plan(
    plan: ReturnInterceptPlan,
    *,
    ledger_dir: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    """Durably record a deterministic plan without attempting any action."""

    if not isinstance(plan, ReturnInterceptPlan):
        return {
            "command_id": getattr(plan, "command_id", None),
            "status": "rejected",
            "reason": _reason("invalid_plan", "ReturnInterceptPlan is required"),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
    path = _ledger_path(plan.agent_id, ledger_dir)
    try:
        with _serialized(path):
            ledger = _load_ledger(path, plan.agent_id)
            existing = ledger["commands"].get(plan.command_id)
            if existing is not None:
                if existing["plan"] != plan.to_dict():
                    return {
                        "command_id": plan.command_id,
                        "status": "rejected",
                        "reason": _reason(
                            "command_id_conflict",
                            "command id already belongs to a different plan",
                        ),
                        "retry_safe": False,
                        "stage": existing.get("stage"),
                        "dispatch": existing.get("dispatch"),
                    }
                return _outcome(existing)
            record = _new_record(plan)
            ledger["commands"][plan.command_id] = record
            _atomic_write(path, ledger)
            return _outcome(record)
    except InterceptLedgerError as exc:
        return {
            "command_id": plan.command_id,
            "status": "rejected",
            "reason": _reason("ledger_unavailable", "plan was not durably recorded", error=str(exc)),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }


def _validate_gate(
    name: str,
    callback: GateCallback,
    client: Any,
    plan: ReturnInterceptPlan,
) -> GateDecision:
    if not callable(callback):
        raise InterceptPlanError(f"required {name} callback is unavailable")
    decision = callback(client, plan)
    if not isinstance(decision, GateDecision):
        raise InterceptPlanError(f"{name} callback did not return GateDecision")
    if not isinstance(decision.passed, bool) or not isinstance(decision.retryable, bool):
        raise InterceptPlanError(f"{name} callback returned non-exact booleans")
    _number(decision.checked_at, f"{name}.checked_at", 0)
    if not isinstance(decision.code, str) or not decision.code:
        raise InterceptPlanError(f"{name} callback omitted its decision code")
    return decision


def _validate_reobservation(
    plan: ReturnInterceptPlan,
    observation: PhalanxObservation,
    server_now: Timestamp,
) -> None:
    if not isinstance(observation, PhalanxObservation):
        raise InterceptPlanError("phalanx revalidation did not return an exact observation")
    if observation.agent_id != plan.agent_id:
        raise InterceptPlanError("phalanx revalidation belongs to another agent")
    if not observation.is_fresh(server_now, plan.max_phalanx_age_seconds):
        raise InterceptPlanError("phalanx revalidation is stale, expired, or future-dated")
    movement = observation.exact_movement(plan.movement_id)
    if movement is None:
        raise InterceptPlanError("exact movement disappeared; no safety inference is permitted")
    if movement.fingerprint != plan.movement_fingerprint:
        raise InterceptPlanError("exact movement changed since planning")
    if movement.hostile is not True:
        raise InterceptPlanError("movement is no longer explicitly hostile")
    if movement.direction != "return" or movement.state != "returning":
        raise InterceptPlanError("movement is no longer explicitly returning")
    if movement.return_eta != plan.hostile_return_eta:
        raise InterceptPlanError("hostile return ETA changed")
    if movement.owner_id != plan.hostile_owner_id or movement.source != plan.target_coordinates:
        raise InterceptPlanError("hostile owner or return target changed")


def _default_attack_sender(client: Any, plan: ReturnInterceptPlan) -> Mapping[str, Any]:
    from bots.shared import fleet

    return fleet.attack(
        client,
        plan.target_coordinates,
        _ship_dict(plan.ships),
        speed=plan.speed,
        target_type=plan.target_type,
        check=True,
    )


def _default_jump_gate_sender(
    client: Any, stage: JumpGateStagingPlan
) -> Mapping[str, Any]:
    from bots.shared.moons import execute_jump_gate_transfer

    return execute_jump_gate_transfer(
        client,
        stage.source_moon,
        stage.destination_moon,
        _ship_dict(stage.ships),
    )


def _run_stage(
    client: Any,
    plan: ReturnInterceptPlan,
    callbacks: InterceptSafetyCallbacks,
    ledger: dict[str, Any],
    path: pathlib.Path,
    record: dict[str, Any],
    *,
    now: float,
    jump_gate_sender: Callable[[Any, JumpGateStagingPlan], Mapping[str, Any]],
) -> dict[str, Any] | None:
    stage_plan = plan.jump_gate_staging
    stage_record = record.get("stage")
    if stage_plan is None:
        return None
    if not isinstance(stage_record, dict):
        _transition(
            ledger,
            path,
            record,
            "rejected",
            reason=_reason("stage_ledger_missing", "jump gate stage record is missing"),
        )
        return _outcome(record)
    stage_status = stage_record.get("status")
    if stage_status == "sent":
        return None
    if stage_status in {"sending", "ambiguous"}:
        stage_record["status"] = "ambiguous"
        _transition(
            ledger,
            path,
            record,
            "ambiguous",
            reason=_reason(
                "jump_gate_outcome_unknown",
                "jump gate send boundary is uncertain; attack and retry are forbidden",
            ),
        )
        return _outcome(record)
    if stage_status == "rejected":
        _transition(
            ledger,
            path,
            record,
            "rejected",
            reason=_reason("jump_gate_rejected", "jump gate staging was rejected"),
        )
        return _outcome(record)
    if now < float(stage_plan.execute_at):
        _transition(
            ledger,
            path,
            record,
            "waiting",
            reason=_reason("waiting_for_jump_gate", "jump gate staging is not due"),
        )
        return _outcome(record)
    if now > float(stage_plan.latest_at):
        stage_record["status"] = "rejected"
        _transition(
            ledger,
            path,
            record,
            "rejected",
            reason=_reason("jump_gate_window_missed", "jump gate staging window was missed"),
        )
        return _outcome(record)
    if callbacks.jump_gate_ready is None:
        stage_record["status"] = "rejected"
        _transition(
            ledger,
            path,
            record,
            "rejected",
            reason=_reason("jump_gate_check_missing", "jump gate readiness callback is required"),
        )
        return _outcome(record)
    try:
        decision = _validate_gate(
            "jump_gate_ready", callbacks.jump_gate_ready, client, plan
        )
    except Exception as exc:
        stage_record["status"] = "rejected"
        _transition(
            ledger,
            path,
            record,
            "rejected",
            reason=_reason("jump_gate_check_error", "jump gate readiness is uncertain", error=str(exc)),
        )
        return _outcome(record)
    if decision.passed is not True:
        status = "waiting" if decision.retryable else "rejected"
        stage_record["status"] = status
        _transition(
            ledger,
            path,
            record,
            status,
            reason=_reason(decision.code, "jump gate readiness gate did not pass"),
        )
        return _outcome(record)
    stage_record.update({"status": "sending", "send_started_at": time.time()})
    try:
        _transition(
            ledger,
            path,
            record,
            "waiting",
            reason=_reason("jump_gate_sending", "durable jump gate send boundary entered"),
        )
    except InterceptLedgerError as exc:
        return {
            "command_id": plan.command_id,
            "status": "rejected",
            "reason": _reason(
                "jump_gate_boundary_unavailable",
                "jump gate was not attempted because sending state was not durable",
                error=str(exc),
            ),
            "retry_safe": False,
            "stage": stage_record,
            "dispatch": None,
        }
    try:
        result = jump_gate_sender(client, stage_plan)
    except Exception as exc:
        stage_record.update(
            {
                "status": "ambiguous",
                "reason": _reason(
                    "jump_gate_exception",
                    "jump gate outcome is uncertain; no retry is allowed",
                    error=f"{type(exc).__name__}: {exc}",
                ),
            }
        )
        try:
            _transition(ledger, path, record, "ambiguous", reason=stage_record["reason"])
        except InterceptLedgerError:
            pass
        return _outcome(record, override_status="ambiguous")
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        stage_record.update(
            {
                "status": "rejected",
                "reason": _reason(
                    "jump_gate_rejected", "jump gate API explicitly rejected staging"
                ),
                "response": dict(result) if isinstance(result, Mapping) else None,
            }
        )
        _transition(ledger, path, record, "rejected", reason=stage_record["reason"])
        return _outcome(record)
    stage_record.update({"status": "sent", "sent_at": time.time(), "response": dict(result)})
    try:
        _transition(
            ledger,
            path,
            record,
            "waiting",
            reason=_reason("jump_gate_staged", "jump gate staging was acknowledged"),
        )
    except InterceptLedgerError:
        return _outcome(record, override_status="ambiguous")
    return None


def execute_due_intercept(
    client: Any,
    plan: ReturnInterceptPlan,
    callbacks: InterceptSafetyCallbacks,
    *,
    ledger_dir: pathlib.Path | str | None = None,
    attack_sender: Callable[[Any, ReturnInterceptPlan], Mapping[str, Any]] | None = None,
    jump_gate_sender: Callable[[Any, JumpGateStagingPlan], Mapping[str, Any]] | None = None,
    preflight_max_age_seconds: int = 3,
    clock: Callable[[], Timestamp] = time.time,
) -> dict[str, Any]:
    """Safely execute a due intercept once, or fail closed with no blind retry.

    ``waiting`` is retryable only before a send boundary.  ``sent``,
    ``rejected``, and ``ambiguous`` are terminal for this command id.
    """

    if not isinstance(plan, ReturnInterceptPlan):
        return {
            "command_id": getattr(plan, "command_id", None),
            "status": "rejected",
            "reason": _reason("invalid_plan", "ReturnInterceptPlan is required"),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
    try:
        plan = normalize_intercept_timing(plan)
    except Exception as exc:
        return {
            "command_id": getattr(plan, "command_id", None),
            "status": "rejected",
            "reason": _reason(
                "invalid_plan_timing",
                "intercept or jump-gate timing fields are malformed",
                error=f"{type(exc).__name__}: {exc}",
            ),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
    if not isinstance(callbacks, InterceptSafetyCallbacks):
        return {
            "command_id": plan.command_id,
            "status": "rejected",
            "reason": _reason("missing_safety_callbacks", "all safety callbacks are required"),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
    try:
        max_gate_age = _strict_int(
            preflight_max_age_seconds, "preflight_max_age_seconds", 1
        )
        local_now = float(_number(clock(), "clock", 0))
        path = _ledger_path(plan.agent_id, ledger_dir)
    except Exception as exc:
        return {
            "command_id": plan.command_id,
            "status": "rejected",
            "reason": _reason("invalid_execution_context", "execution context is invalid", error=str(exc)),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
    send_attack = attack_sender or _default_attack_sender
    send_jump = jump_gate_sender or _default_jump_gate_sender

    try:
        with _serialized(path):
            ledger = _load_ledger(path, plan.agent_id)
            record = ledger["commands"].get(plan.command_id)
            if record is None:
                record = _new_record(plan)
                ledger["commands"][plan.command_id] = record
                _atomic_write(path, ledger)
            elif record["plan"] != plan.to_dict():
                return {
                    "command_id": plan.command_id,
                    "status": "rejected",
                    "reason": _reason(
                        "command_id_conflict", "command id belongs to a different intercept"
                    ),
                    "retry_safe": False,
                    "stage": record.get("stage"),
                    "dispatch": record.get("dispatch"),
                }
            if record["status"] == "sending":
                _transition(
                    ledger,
                    path,
                    record,
                    "ambiguous",
                    reason=_reason(
                        "interrupted_send_boundary",
                        "previous send outcome is unknown; blind retry is forbidden",
                    ),
                )
                return _outcome(record)
            if record["status"] in _TERMINAL_STATUSES:
                return _outcome(record)

            stage_outcome = _run_stage(
                client,
                plan,
                callbacks,
                ledger,
                path,
                record,
                now=local_now,
                jump_gate_sender=send_jump,
            )
            if stage_outcome is not None:
                return stage_outcome
            if local_now < float(plan.dispatch_at):
                _transition(
                    ledger,
                    path,
                    record,
                    "waiting",
                    reason=_reason("dispatch_not_due", "intercept dispatch time has not arrived"),
                )
                return _outcome(record)
            if local_now > plan.latest_dispatch_at:
                _transition(
                    ledger,
                    path,
                    record,
                    "rejected",
                    reason=_reason("timing_window_missed", "intercept send window was missed"),
                )
                return _outcome(record)

            named_callbacks = (
                ("espionage_full_intel", callbacks.espionage_full_intel),
                ("admin_vacation_banned", callbacks.admin_vacation_banned),
                ("bashing", callbacks.bashing),
                ("combat_profitability", callbacks.combat_profitability),
                ("fleet_slot", callbacks.fleet_slot),
                ("ship_resource", callbacks.ship_resource),
                ("target_ownership", callbacks.target_ownership),
            )
            decisions: list[tuple[str, GateDecision]] = []
            for name, callback in named_callbacks:
                try:
                    decision = _validate_gate(name, callback, client, plan)
                except Exception as exc:
                    _transition(
                        ledger,
                        path,
                        record,
                        "rejected",
                        reason=_reason(
                            f"{name}_uncertain",
                            f"{name} safety gate is unavailable or malformed",
                            error=f"{type(exc).__name__}: {exc}",
                        ),
                    )
                    return _outcome(record)
                decisions.append((name, decision))
                if decision.passed is not True:
                    status = "waiting" if decision.retryable else "rejected"
                    _transition(
                        ledger,
                        path,
                        record,
                        status,
                        reason=_reason(
                            decision.code,
                            f"{name} safety gate did not explicitly pass",
                            evidence=dict(decision.evidence or {}),
                        ),
                    )
                    return _outcome(record)

            try:
                reobservation = callbacks.phalanx_revalidation(client, plan)
                timing = callbacks.timing_revalidation(client, plan)
                if not isinstance(timing, TimingRevalidation):
                    raise InterceptPlanError(
                        "timing callback did not return TimingRevalidation"
                    )
                server_now = float(_number(timing.server_time, "timing.server_time", 0))
                if timing.dispatch_ready is not True:
                    raise InterceptPlanError("timing callback did not explicitly report ready")
                if _strict_int(
                    timing.own_flight_seconds, "timing.own_flight_seconds", 1
                ) != plan.own_flight_seconds:
                    raise InterceptPlanError("own fleet flight time changed")
                if normalize_coordinate(
                    timing.source_coordinates, "timing.source_coordinates"
                ) != plan.source_coordinates:
                    raise InterceptPlanError("dispatch source changed")
                if normalize_coordinate(
                    timing.target_coordinates, "timing.target_coordinates"
                ) != plan.target_coordinates:
                    raise InterceptPlanError("dispatch target changed")
                if server_now < float(plan.dispatch_at) or server_now > plan.latest_dispatch_at:
                    raise InterceptPlanError("authoritative send window is not open")
                predicted_impact = (
                    server_now
                    + timing.own_flight_seconds
                    + plan.dispatch_margin_seconds
                )
                if abs(predicted_impact - float(plan.desired_impact_at)) > plan.send_window_seconds:
                    raise InterceptPlanError("revalidated impact falls outside timing tolerance")
                for name, decision in decisions:
                    age = server_now - float(decision.checked_at)
                    if age < 0 or age > max_gate_age:
                        raise InterceptPlanError(f"{name} gate is stale or future-dated")
                _validate_reobservation(plan, reobservation, server_now)
            except Exception as exc:
                _transition(
                    ledger,
                    path,
                    record,
                    "rejected",
                    reason=_reason(
                        "send_boundary_uncertain",
                        "exact phalanx/timing revalidation did not pass",
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                return _outcome(record)

            dispatch = {
                "source_coordinates": coordinate_dict(plan.source_coordinates),
                "target_coordinates": coordinate_dict(plan.target_coordinates),
                "target_type": plan.target_type,
                "ships": {str(key): value for key, value in plan.ships},
                "speed": plan.speed,
                "authoritative_server_time": timing.server_time,
                "own_flight_seconds": timing.own_flight_seconds,
                "desired_impact_at": plan.desired_impact_at,
                "revalidated_observation_id": reobservation.observation_id,
            }
            try:
                _transition(
                    ledger,
                    path,
                    record,
                    "sending",
                    reason=_reason("sending", "durable attack send boundary entered"),
                    dispatch=dispatch,
                    send_started_at=time.time(),
                )
            except InterceptLedgerError as exc:
                return {
                    "command_id": plan.command_id,
                    "status": "rejected",
                    "reason": _reason(
                        "send_boundary_unavailable",
                        "attack was not attempted because sending state was not durable",
                        error=str(exc),
                    ),
                    "retry_safe": False,
                    "stage": record.get("stage"),
                    "dispatch": dispatch,
                }
            try:
                result = send_attack(client, plan)
            except Exception as exc:
                reason = _reason(
                    "attack_exception",
                    "attack outcome is uncertain; no retry is allowed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                try:
                    _transition(ledger, path, record, "ambiguous", reason=reason)
                except InterceptLedgerError:
                    pass
                return _outcome(record, override_status="ambiguous")
            if not isinstance(result, Mapping):
                reason = _reason(
                    "attack_response_ambiguous",
                    "attack API response was not structured; no retry is allowed",
                )
                try:
                    _transition(ledger, path, record, "ambiguous", reason=reason)
                except InterceptLedgerError:
                    pass
                return _outcome(record, override_status="ambiguous")
            if result.get("ok") is not True:
                _transition(
                    ledger,
                    path,
                    record,
                    "rejected",
                    reason=_reason(
                        "attack_rejected",
                        "checked fleet API explicitly rejected the attack",
                        message=result.get("message"),
                    ),
                    send_response=dict(result),
                )
                return _outcome(record)
            try:
                _transition(
                    ledger,
                    path,
                    record,
                    "sent",
                    reason=_reason("sent", "checked fleet API acknowledged the attack"),
                    send_response=dict(result),
                    sent_at=time.time(),
                )
            except InterceptLedgerError:
                return _outcome(record, override_status="ambiguous")
            return _outcome(record)
    except InterceptLedgerError as exc:
        return {
            "command_id": plan.command_id,
            "status": "rejected",
            "reason": _reason(
                "ledger_unavailable", "intercept ledger is unavailable; nothing was sent", error=str(exc)
            ),
            "retry_safe": False,
            "stage": None,
            "dispatch": None,
        }
