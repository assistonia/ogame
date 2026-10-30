"""Exact, bounded phalanx intelligence and conservative scan scheduling.

This module deliberately does not expose an "is safe" query.  A missing,
expired, malformed, or empty observation is evidence of nothing.  Consumers
must select an exact movement from a fresh observation and revalidate it at
their own send boundary.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


Coordinate = tuple[int, int, int]
Timestamp = int | float

_CACHE_VERSION = 1
_DEFAULT_CACHE_DIR = pathlib.Path(__file__).with_name(".phalanx_intel_state")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[pathlib.Path, threading.RLock] = {}


class PhalanxIntelError(RuntimeError):
    """Base class for fail-closed phalanx intelligence errors."""


class ObservationValidationError(PhalanxIntelError, ValueError):
    """Raised when scan data is incomplete, ambiguous, or malformed."""


class ObservationCacheError(PhalanxIntelError):
    """Raised when the durable observation cache cannot be trusted."""


def _strict_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservationValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: Any, field: str, minimum: float | None = None) -> Timestamp:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationValidationError(f"{field} must be a finite number")
    if not math.isfinite(float(value)):
        raise ObservationValidationError(f"{field} must be a finite number")
    if minimum is not None and float(value) < minimum:
        raise ObservationValidationError(f"{field} must be >= {minimum}")
    return value


def normalize_coordinate(value: Any, field: str = "coordinates") -> Coordinate:
    if isinstance(value, Mapping):
        raw = (value.get("galaxy"), value.get("system"), value.get("position"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 3:
            raise ObservationValidationError(f"{field} must contain exactly 3 values")
        raw = tuple(value)
    else:
        raise ObservationValidationError(f"{field} must be a coordinate mapping or tuple")
    return (
        _strict_int(raw[0], f"{field}.galaxy", 1),
        _strict_int(raw[1], f"{field}.system", 1),
        _strict_int(raw[2], f"{field}.position", 1),
    )


def coordinate_dict(value: Coordinate) -> dict[str, int]:
    return {"galaxy": value[0], "system": value[1], "position": value[2]}


def _aliased(data: Mapping[str, Any], names: tuple[str, ...], field: str) -> Any:
    present = [(name, data[name]) for name in names if name in data]
    if not present:
        raise ObservationValidationError(f"{field} is required")
    first = present[0][1]
    if any(value != first for _, value in present[1:]):
        raise ObservationValidationError(f"conflicting aliases for {field}")
    return first


def _optional_aliased(data: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    present = [(name, data[name]) for name in names if name in data]
    if not present:
        return None
    first = present[0][1]
    if any(value != first for _, value in present[1:]):
        raise ObservationValidationError(f"conflicting aliases for {names[0]}")
    return first


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ObservationValidationError(f"{field} must be a string or integer")
    result = str(value).strip()
    if not result or len(result) > 256:
        raise ObservationValidationError(f"{field} is empty or too long")
    return result


def _mission(value: Any) -> int | str:
    if isinstance(value, bool):
        raise ObservationValidationError("mission must be an integer or non-empty string")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip() and len(value.strip()) <= 128:
        return value.strip()
    raise ObservationValidationError("mission must be an integer or non-empty string")


def _planet_type(value: Any, field: str) -> int | None:
    if value is None:
        return None
    result = _strict_int(value, field, 1)
    if result not in (1, 2, 3):
        raise ObservationValidationError(f"{field} is not a known planet type")
    return result


def _ships(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ObservationValidationError("ships must be a mapping when exposed")
    result: dict[str, int] = {}
    for raw_id, raw_count in value.items():
        ship_id = _optional_text(raw_id, "ships technology id")
        if ship_id is None:
            raise ObservationValidationError("ships technology id is required")
        result[ship_id] = _strict_int(raw_count, f"ships.{ship_id}", 0)
    return dict(sorted(result.items()))


def _direction_and_state(data: Mapping[str, Any]) -> tuple[str, str]:
    raw_direction = _optional_aliased(data, ("direction",))
    raw_state = _optional_aliased(data, ("state", "flight_state"))
    return_flight = _optional_aliased(data, ("return_flight", "is_returning"))
    if return_flight is not None and not isinstance(return_flight, bool):
        raise ObservationValidationError("return_flight must be an exact boolean")

    direction_map = {
        "outbound": "outbound",
        "out": "outbound",
        "return": "return",
        "returning": "return",
    }
    state_map = {
        "outbound": "outbound",
        "out": "outbound",
        "return": "returning",
        "returning": "returning",
    }
    direction = None
    state = None
    if raw_direction is not None:
        direction = direction_map.get(str(raw_direction).strip().lower())
        if direction is None:
            raise ObservationValidationError("direction must explicitly be outbound or return")
    if raw_state is not None:
        state = state_map.get(str(raw_state).strip().lower())
        if state is None:
            raise ObservationValidationError("state must explicitly be outbound or returning")
    if return_flight is not None:
        explicit_direction = "return" if return_flight else "outbound"
        explicit_state = "returning" if return_flight else "outbound"
        if direction is not None and direction != explicit_direction:
            raise ObservationValidationError("direction conflicts with return_flight")
        if state is not None and state != explicit_state:
            raise ObservationValidationError("state conflicts with return_flight")
        direction, state = explicit_direction, explicit_state
    if direction is None and state is not None:
        direction = "return" if state == "returning" else "outbound"
    if state is None and direction is not None:
        state = "returning" if direction == "return" else "outbound"
    if direction is None or state is None:
        raise ObservationValidationError("exact outbound/return state is required")
    if (direction == "return") != (state == "returning"):
        raise ObservationValidationError("direction and state are inconsistent")
    return direction, state


@dataclass(frozen=True)
class FleetMovement:
    """One exact fleet movement exposed by a single phalanx response."""

    movement_id: str
    owner_id: str | None
    owner_name: str | None
    hostile: bool | None
    source: Coordinate
    target: Coordinate
    source_type: int | None
    target_type: int | None
    mission: int | str
    direction: str
    state: str
    departure_eta: Timestamp | None
    arrival_eta: Timestamp | None
    return_eta: Timestamp | None
    ships: dict[str, int] | None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FleetMovement":
        if not isinstance(data, Mapping):
            raise ObservationValidationError("fleet movement must be a mapping")
        movement_id = _optional_text(
            _aliased(data, ("movement_id", "fleet_id", "id"), "movement_id"),
            "movement_id",
        )
        if movement_id is None:
            raise ObservationValidationError("movement_id is required")
        hostile = data.get("hostile")
        if hostile is not None and not isinstance(hostile, bool):
            raise ObservationValidationError("hostile must be an exact boolean when exposed")
        direction, state = _direction_and_state(data)

        def eta(names: tuple[str, ...], field: str) -> Timestamp | None:
            raw = _optional_aliased(data, names)
            return None if raw is None else _number(raw, field, 0)

        return cls(
            movement_id=movement_id,
            owner_id=_optional_text(data.get("owner_id"), "owner_id"),
            owner_name=_optional_text(data.get("owner_name"), "owner_name"),
            hostile=hostile,
            source=normalize_coordinate(
                _aliased(data, ("source", "origin", "source_coordinates"), "source"),
                "source",
            ),
            target=normalize_coordinate(
                _aliased(data, ("target", "destination", "target_coordinates"), "target"),
                "target",
            ),
            source_type=_planet_type(data.get("source_type"), "source_type"),
            target_type=_planet_type(data.get("target_type"), "target_type"),
            mission=_mission(_aliased(data, ("mission", "mission_type"), "mission")),
            direction=direction,
            state=state,
            departure_eta=eta(
                ("departure_eta", "departure_at", "departure_ts"), "departure_eta"
            ),
            arrival_eta=eta(("arrival_eta", "arrival_at", "arrival_ts"), "arrival_eta"),
            return_eta=eta(("return_eta", "return_at", "return_ts"), "return_eta"),
            ships=_ships(data["ships"]) if "ships" in data else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "movement_id": self.movement_id,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "hostile": self.hostile,
            "source": coordinate_dict(self.source),
            "target": coordinate_dict(self.target),
            "source_type": self.source_type,
            "target_type": self.target_type,
            "mission": self.mission,
            "direction": self.direction,
            "state": self.state,
            "departure_eta": self.departure_eta,
            "arrival_eta": self.arrival_eta,
            "return_eta": self.return_eta,
            "ships": None if self.ships is None else dict(self.ships),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


@dataclass(frozen=True)
class PhalanxObservation:
    """An immutable, expiring snapshot from one source moon to one target."""

    observation_id: str
    agent_id: str
    source_moon: Coordinate
    target: Coordinate
    scan_time: Timestamp
    server_time: Timestamp
    expires_at: Timestamp
    movements: tuple[FleetMovement, ...]

    @classmethod
    def build(
        cls,
        *,
        agent_id: str,
        source_moon: Any,
        target: Any,
        scan_time: Any,
        server_time: Any,
        expires_at: Any,
        movements: Sequence[Mapping[str, Any] | FleetMovement],
    ) -> "PhalanxObservation":
        normalized_agent = _agent_id(agent_id)
        normalized_scan = _number(scan_time, "scan_time", 0)
        normalized_server = _number(server_time, "server_time", 0)
        normalized_expiry = _number(expires_at, "expires_at", 0)
        if float(normalized_expiry) <= float(normalized_server):
            raise ObservationValidationError("expires_at must be after server_time")
        parsed = tuple(
            item if isinstance(item, FleetMovement) else FleetMovement.from_mapping(item)
            for item in movements
        )
        ids = [item.movement_id for item in parsed]
        if len(ids) != len(set(ids)):
            raise ObservationValidationError("movement_id values must be unique per observation")
        body = {
            "agent_id": normalized_agent,
            "source_moon": coordinate_dict(normalize_coordinate(source_moon, "source_moon")),
            "target": coordinate_dict(normalize_coordinate(target, "target")),
            "scan_time": normalized_scan,
            "server_time": normalized_server,
            "expires_at": normalized_expiry,
            "movements": [item.to_dict() for item in parsed],
        }
        observation_id = "phx:" + hashlib.sha256(_canonical_json(body)).hexdigest()[:32]
        return cls(
            observation_id=observation_id,
            agent_id=normalized_agent,
            source_moon=normalize_coordinate(source_moon, "source_moon"),
            target=normalize_coordinate(target, "target"),
            scan_time=normalized_scan,
            server_time=normalized_server,
            expires_at=normalized_expiry,
            movements=parsed,
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PhalanxObservation":
        if not isinstance(data, Mapping):
            raise ObservationValidationError("observation must be a mapping")
        movements = data.get("movements")
        if not isinstance(movements, list):
            raise ObservationValidationError("observation movements must be a list")
        rebuilt = cls.build(
            agent_id=data.get("agent_id"),
            source_moon=data.get("source_moon"),
            target=data.get("target"),
            scan_time=data.get("scan_time"),
            server_time=data.get("server_time"),
            expires_at=data.get("expires_at"),
            movements=movements,
        )
        if data.get("observation_id") != rebuilt.observation_id:
            raise ObservationValidationError("observation_id does not match observation content")
        return rebuilt

    @classmethod
    def from_scan_response(
        cls,
        *,
        agent_id: str,
        source_moon: Any,
        target: Any,
        response: Mapping[str, Any],
        scan_time: Timestamp,
        ttl_seconds: int,
    ) -> "PhalanxObservation":
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise ObservationValidationError("phalanx response must explicitly report ok=true")
        server_time = _aliased(
            response, ("server_time", "server_ts"), "response.server_time"
        )
        movements = _optional_aliased(response, ("movements", "fleet_movements"))
        if not isinstance(movements, list):
            raise ObservationValidationError("phalanx response must expose a movements list")
        ttl = _strict_int(ttl_seconds, "ttl_seconds", 1)
        expires_at = response.get("expires_at")
        if expires_at is None:
            expires_at = float(_number(server_time, "server_time", 0)) + ttl
        return cls.build(
            agent_id=agent_id,
            source_moon=source_moon,
            target=target,
            scan_time=scan_time,
            server_time=server_time,
            expires_at=expires_at,
            movements=movements,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "agent_id": self.agent_id,
            "source_moon": coordinate_dict(self.source_moon),
            "target": coordinate_dict(self.target),
            "scan_time": self.scan_time,
            "server_time": self.server_time,
            "expires_at": self.expires_at,
            "movements": [movement.to_dict() for movement in self.movements],
        }

    def exact_movement(self, movement_id: str) -> FleetMovement | None:
        wanted = str(movement_id)
        return next((item for item in self.movements if item.movement_id == wanted), None)

    def is_fresh(self, server_now: Timestamp, max_age_seconds: int) -> bool:
        now = float(_number(server_now, "server_now", 0))
        max_age = _strict_int(max_age_seconds, "max_age_seconds", 1)
        age = now - float(self.server_time)
        return 0 <= age <= max_age and now < float(self.expires_at)


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise ObservationValidationError("agent_id must be a non-empty string")
    return value.strip()


def _agent_digest(agent_id: str) -> str:
    return hashlib.sha256(_agent_id(agent_id).encode("utf-8")).hexdigest()[:24]


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
    except ObservationCacheError:
        raise
    except OSError as exc:
        raise ObservationCacheError(f"cache lock unavailable: {exc}") from exc


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
        raise ObservationCacheError(f"atomic cache write failed: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class PhalanxObservationCache:
    """Atomic, process-safe, bounded observation cache for exactly one agent."""

    def __init__(
        self,
        agent_id: str,
        *,
        cache_dir: pathlib.Path | str | None = None,
        max_observations: int = 128,
        max_movements_per_observation: int = 128,
        max_ttl_seconds: int = 900,
    ) -> None:
        self.agent_id = _agent_id(agent_id)
        self.max_observations = _strict_int(max_observations, "max_observations", 1)
        self.max_movements = _strict_int(
            max_movements_per_observation, "max_movements_per_observation", 1
        )
        self.max_ttl_seconds = _strict_int(max_ttl_seconds, "max_ttl_seconds", 1)
        root = pathlib.Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.path = root / f"{_agent_digest(self.agent_id)}.json"

    def _empty(self) -> dict[str, Any]:
        return {"version": _CACHE_VERSION, "agent_id": self.agent_id, "observations": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            with self.path.open("r", encoding="ascii") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ObservationCacheError(f"cache is unreadable; refusing empty fallback: {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != _CACHE_VERSION
            or data.get("agent_id") != self.agent_id
            or not isinstance(data.get("observations"), list)
        ):
            raise ObservationCacheError("cache header is invalid or belongs to another agent")
        if len(data["observations"]) > self.max_observations:
            raise ObservationCacheError("cache exceeds configured observation bound")
        try:
            parsed = [PhalanxObservation.from_mapping(item) for item in data["observations"]]
        except ObservationValidationError as exc:
            raise ObservationCacheError(f"cached observation is invalid: {exc}") from exc
        if any(len(item.movements) > self.max_movements for item in parsed):
            raise ObservationCacheError("cached movement list exceeds configured bound")
        return data

    def put(self, observation: PhalanxObservation) -> PhalanxObservation:
        if not isinstance(observation, PhalanxObservation):
            raise ObservationValidationError("observation must be PhalanxObservation")
        if observation.agent_id != self.agent_id:
            raise ObservationValidationError("observation belongs to another agent")
        if len(observation.movements) > self.max_movements:
            raise ObservationValidationError("movement list exceeds configured bound; not truncated")
        ttl = float(observation.expires_at) - float(observation.server_time)
        if ttl > self.max_ttl_seconds:
            raise ObservationValidationError("observation expiry exceeds configured TTL bound")
        with _serialized(self.path):
            data = self._load()
            observations = [
                item
                for item in data["observations"]
                if item.get("observation_id") != observation.observation_id
            ]
            observations.append(observation.to_dict())
            observations.sort(
                key=lambda item: (
                    float(item["server_time"]),
                    float(item["scan_time"]),
                    item["observation_id"],
                )
            )
            data["observations"] = observations[-self.max_observations :]
            _atomic_write(self.path, data)
        return observation

    def record_scan_response(
        self,
        *,
        source_moon: Any,
        target: Any,
        response: Mapping[str, Any],
        scan_time: Timestamp,
        ttl_seconds: int,
    ) -> PhalanxObservation:
        observation = PhalanxObservation.from_scan_response(
            agent_id=self.agent_id,
            source_moon=source_moon,
            target=target,
            response=response,
            scan_time=scan_time,
            ttl_seconds=ttl_seconds,
        )
        return self.put(observation)

    def latest_exact(
        self,
        target: Any,
        *,
        server_now: Timestamp,
        max_age_seconds: int,
        source_moon: Any | None = None,
    ) -> PhalanxObservation | None:
        wanted_target = normalize_coordinate(target, "target")
        wanted_source = (
            None if source_moon is None else normalize_coordinate(source_moon, "source_moon")
        )
        with _serialized(self.path):
            data = self._load()
        matching = []
        for raw in data["observations"]:
            observation = PhalanxObservation.from_mapping(raw)
            if observation.target != wanted_target:
                continue
            if wanted_source is not None and observation.source_moon != wanted_source:
                continue
            if observation.is_fresh(server_now, max_age_seconds):
                matching.append(observation)
        if not matching:
            return None
        return max(
            matching,
            key=lambda item: (float(item.server_time), float(item.scan_time), item.observation_id),
        )


@dataclass(frozen=True)
class MoonPhalanxSource:
    coordinates: Coordinate
    phalanx_level: int
    deuterium_available: int
    deuterium_reserve: int
    scan_cost: int = 5_000
    max_scans_per_tick: int | None = None


@dataclass(frozen=True)
class PhalanxTargetCandidate:
    coordinates: Coordinate
    target_value: float
    last_scan_server_time: Timestamp | None = None
    known_hostile_activity: float = 0.0


@dataclass(frozen=True)
class ScanOrder:
    source_moon: Coordinate
    target: Coordinate
    deuterium_cost: int
    deuterium_reserve: int
    range_systems: int
    system_distance: int
    score: float
    target_value: float
    freshness_age: float | None
    known_hostile_activity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_moon": coordinate_dict(self.source_moon),
            "target": coordinate_dict(self.target),
            "deuterium_cost": self.deuterium_cost,
            "deuterium_reserve": self.deuterium_reserve,
            "range_systems": self.range_systems,
            "system_distance": self.system_distance,
            "score": self.score,
            "target_value": self.target_value,
            "freshness_age": self.freshness_age,
            "known_hostile_activity": self.known_hostile_activity,
        }


@dataclass(frozen=True)
class CandidateScanPlan:
    agent_id: str
    server_time: Timestamp
    max_scans_per_tick: int
    orders: tuple[ScanOrder, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "server_time": self.server_time,
            "max_scans_per_tick": self.max_scans_per_tick,
            "orders": [order.to_dict() for order in self.orders],
        }


def _source(source: MoonPhalanxSource) -> MoonPhalanxSource:
    if not isinstance(source, MoonPhalanxSource):
        raise ObservationValidationError("moon source must be MoonPhalanxSource")
    coordinates = normalize_coordinate(source.coordinates, "moon.coordinates")
    level = _strict_int(source.phalanx_level, "phalanx_level", 1)
    available = _strict_int(source.deuterium_available, "deuterium_available", 0)
    reserve = _strict_int(source.deuterium_reserve, "deuterium_reserve", 0)
    cost = _strict_int(source.scan_cost, "scan_cost", 1)
    limit = source.max_scans_per_tick
    if limit is not None:
        limit = _strict_int(limit, "moon.max_scans_per_tick", 1)
    return MoonPhalanxSource(coordinates, level, available, reserve, cost, limit)


def _target(candidate: PhalanxTargetCandidate) -> PhalanxTargetCandidate:
    if not isinstance(candidate, PhalanxTargetCandidate):
        raise ObservationValidationError("target must be PhalanxTargetCandidate")
    value = float(_number(candidate.target_value, "target_value", 0))
    activity = float(_number(candidate.known_hostile_activity, "known_hostile_activity", 0))
    last = candidate.last_scan_server_time
    if last is not None:
        last = _number(last, "last_scan_server_time", 0)
    return PhalanxTargetCandidate(
        normalize_coordinate(candidate.coordinates, "target.coordinates"), value, last, activity
    )


def plan_candidate_scans(
    agent_id: str,
    moons: Sequence[MoonPhalanxSource],
    targets: Sequence[PhalanxTargetCandidate],
    *,
    server_now: Timestamp,
    max_scans_per_tick: int = 3,
    freshness_horizon_seconds: int = 300,
    value_weight: float = 1.0,
    freshness_weight: float = 2.0,
    hostile_activity_weight: float = 3.0,
    cost_weight: float = 1.0,
) -> CandidateScanPlan:
    """Choose valuable stale/hostile targets without violating moon reserves.

    Phalanx range is ``level ** 2 - 1`` systems.  No safety meaning is
    attached to an unselected target or to a target with no cached scan.
    """

    agent = _agent_id(agent_id)
    now = float(_number(server_now, "server_now", 0))
    tick_limit = _strict_int(max_scans_per_tick, "max_scans_per_tick", 1)
    horizon = _strict_int(freshness_horizon_seconds, "freshness_horizon_seconds", 1)
    weights = (
        float(_number(value_weight, "value_weight", 0)),
        float(_number(freshness_weight, "freshness_weight", 0)),
        float(_number(hostile_activity_weight, "hostile_activity_weight", 0)),
        float(_number(cost_weight, "cost_weight", 0)),
    )
    normalized_moons = [_source(item) for item in moons]
    normalized_targets = [_target(item) for item in targets]
    if len({item.coordinates for item in normalized_moons}) != len(normalized_moons):
        raise ObservationValidationError("moon coordinates must be unique")
    if len({item.coordinates for item in normalized_targets}) != len(normalized_targets):
        raise ObservationValidationError("target coordinates must be unique")

    edges: list[tuple[tuple[Any, ...], ScanOrder]] = []
    for target in normalized_targets:
        if target.last_scan_server_time is None:
            age = None
            freshness = 2.0
        else:
            age = now - float(target.last_scan_server_time)
            if age < 0:
                raise ObservationValidationError("last scan time is ahead of server_now")
            freshness = min(2.0, age / horizon)
        for moon in normalized_moons:
            if moon.coordinates[0] != target.coordinates[0]:
                continue
            distance = abs(moon.coordinates[1] - target.coordinates[1])
            scan_range = moon.phalanx_level**2 - 1
            if distance > scan_range:
                continue
            if moon.deuterium_available - moon.scan_cost < moon.deuterium_reserve:
                continue
            score = (
                weights[0] * math.log1p(target.target_value)
                + weights[1] * freshness
                + weights[2] * math.log1p(target.known_hostile_activity)
                - weights[3] * (moon.scan_cost / max(1, moon.deuterium_available))
                - (distance / max(1, scan_range)) * 0.05
            )
            order = ScanOrder(
                source_moon=moon.coordinates,
                target=target.coordinates,
                deuterium_cost=moon.scan_cost,
                deuterium_reserve=moon.deuterium_reserve,
                range_systems=scan_range,
                system_distance=distance,
                score=score,
                target_value=target.target_value,
                freshness_age=age,
                known_hostile_activity=target.known_hostile_activity,
            )
            rank = (
                -score,
                moon.scan_cost,
                target.coordinates,
                moon.coordinates,
            )
            edges.append((rank, order))

    budgets = {item.coordinates: item.deuterium_available for item in normalized_moons}
    source_limits = {
        item.coordinates: item.max_scans_per_tick or tick_limit for item in normalized_moons
    }
    source_counts = {item.coordinates: 0 for item in normalized_moons}
    selected_targets: set[Coordinate] = set()
    selected: list[ScanOrder] = []
    for _, order in sorted(edges, key=lambda item: item[0]):
        if len(selected) >= tick_limit:
            break
        if order.target in selected_targets:
            continue
        if source_counts[order.source_moon] >= source_limits[order.source_moon]:
            continue
        remaining = budgets[order.source_moon] - order.deuterium_cost
        if remaining < order.deuterium_reserve:
            continue
        budgets[order.source_moon] = remaining
        source_counts[order.source_moon] += 1
        selected_targets.add(order.target)
        selected.append(order)
    return CandidateScanPlan(agent, server_now, tick_limit, tuple(selected))


def execute_candidate_scan_plan(
    client: Any,
    plan: CandidateScanPlan,
    cache: PhalanxObservationCache,
    *,
    deuterium_available: Callable[[Coordinate], int],
    scan_fn: Callable[[Any, Coordinate, Coordinate], Mapping[str, Any]] | None = None,
    ttl_seconds: int = 120,
    clock: Callable[[], Timestamp] = time.time,
) -> tuple[dict[str, Any], ...]:
    """Execute a bounded plan with a live reserve check before every scan."""

    if not isinstance(plan, CandidateScanPlan) or plan.agent_id != cache.agent_id:
        raise ObservationValidationError("scan plan/cache agent mismatch")
    if len(plan.orders) > plan.max_scans_per_tick:
        raise ObservationValidationError("scan plan exceeds its per-tick bound")
    if not callable(deuterium_available):
        raise ObservationValidationError("live deuterium callback is required")
    if scan_fn is None:
        from bots.shared.moons import phalanx_scan

        scan_fn = phalanx_scan
    outcomes: list[dict[str, Any]] = []
    for order in plan.orders:
        try:
            live_deuterium = _strict_int(
                deuterium_available(order.source_moon), "live_deuterium", 0
            )
            if live_deuterium - order.deuterium_cost < order.deuterium_reserve:
                outcomes.append(
                    {
                        "status": "rejected",
                        "reason": "deuterium_reserve",
                        "source_moon": coordinate_dict(order.source_moon),
                        "target": coordinate_dict(order.target),
                    }
                )
                continue
            response = scan_fn(client, order.source_moon, order.target)
            observation = cache.record_scan_response(
                source_moon=order.source_moon,
                target=order.target,
                response=response,
                scan_time=clock(),
                ttl_seconds=ttl_seconds,
            )
            outcomes.append(
                {
                    "status": "observed",
                    "observation_id": observation.observation_id,
                    "source_moon": coordinate_dict(order.source_moon),
                    "target": coordinate_dict(order.target),
                    "server_time": observation.server_time,
                    "expires_at": observation.expires_at,
                    "movement_count": len(observation.movements),
                }
            )
        except Exception as exc:  # fail closed; a failed scan creates no negative evidence
            outcomes.append(
                {
                    "status": "rejected",
                    "reason": "scan_unavailable",
                    "source_moon": coordinate_dict(order.source_moon),
                    "target": coordinate_dict(order.target),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return tuple(outcomes)
