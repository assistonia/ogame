"""Live production-queue capacity shared by fleet and facility planners.

The OGameX unit queue stores authoritative start/end timestamps.  Queue caps use
those timestamps rather than reconstructing old rows, and use the server formula
only for the new batch being considered.  All reads are SELECT-only through the
thread-local connection maintained by :mod:`bots.shared.db_reader`.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import math
import os
import time

from bots.shared import db_reader


DEFAULT_SHIP_QUEUE_HORIZON_SECONDS = 3 * 3600
MIN_QUEUE_HORIZON_SECONDS = 5 * 60
MAX_QUEUE_HORIZON_SECONDS = 24 * 3600
SHIPYARD = 21
NANITE_FACTORY = 15

# OGameX structural_integrity.rawValue for units is their metal+crystal cost.
# Prerequisite checks remain in the planners/server; this table only models time.
UNIT_STRUCTURE: dict[int, int] = {
    202: 4_000,
    203: 12_000,
    204: 4_000,
    205: 10_000,
    206: 27_000,
    207: 60_000,
    208: 30_000,
    209: 16_000,
    210: 1_000,
    211: 75_000,
    212: 2_000,
    213: 110_000,
    214: 9_000_000,
    215: 70_000,
    217: 4_000,
    218: 140_000,
    219: 23_000,
    401: 2_000,
    402: 2_000,
    403: 8_000,
    404: 35_000,
    405: 8_000,
    406: 100_000,
    407: 20_000,
    408: 100_000,
}


@dataclass(frozen=True)
class UnitQueueItem:
    object_id: int
    requested_amount: int
    completed_amount: int
    remaining_amount: int
    time_start: int
    time_end: int


@dataclass(frozen=True)
class ProductionQueueSnapshot:
    """Selected planet's live, unprocessed production queues."""

    available: bool
    email: str
    planet_id: int | None
    observed_at: int
    economy_speed: int
    building_queue_depth: int
    building_queue_seconds: int
    building_targets: tuple[int, ...]
    unit_queue_depth: int
    unit_queue_seconds: int
    unit_remaining_amount: int
    unit_items: tuple[UnitQueueItem, ...]
    error: str | None = None

    @property
    def nanite_queue_permitted(self) -> bool:
        return (
            self.available
            and self.building_queue_depth == 0
            and self.unit_queue_depth == 0
        )


def default_ship_queue_horizon_seconds() -> int:
    """Configured normal fleet horizon, conservatively defaulting to three hours."""
    raw = os.environ.get(
        "OGAME_SHIP_QUEUE_HORIZON_SECONDS",
        str(DEFAULT_SHIP_QUEUE_HORIZON_SECONDS),
    )
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = DEFAULT_SHIP_QUEUE_HORIZON_SECONDS
    return max(MIN_QUEUE_HORIZON_SECONDS, min(MAX_QUEUE_HORIZON_SECONDS, value))


def _fallback_economy_speed() -> int:
    for name in ("OGAME_ECONOMY_SPEED", "OGAME_UNIVERSE_SPEED"):
        try:
            value = int(float(os.environ.get(name, "")))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return max(1, value)
    return 100


def production_queue_snapshot(
    email: str, planet_id: int | None = None
) -> ProductionQueueSnapshot:
    """Read selected-planet building and unit queues without mutating them.

    Failures are represented by ``available=False`` so planning callers can fail
    closed without duplicating exception handling or accidentally treating an
    unavailable queue as empty.
    """
    now = int(time.time())
    fallback_speed = _fallback_economy_speed()
    try:
        conn = db_reader._conn()
        with conn.cursor() as cur:
            if planet_id is None:
                cur.execute(
                    "SELECT planet_current AS planet_id FROM users WHERE email=%s",
                    (email,),
                )
            else:
                cur.execute(
                    "SELECT p.id AS planet_id FROM planets p "
                    "JOIN users u ON u.id=p.user_id WHERE u.email=%s AND p.id=%s",
                    (email, int(planet_id)),
                )
            user = cur.fetchone()
            if not user or int(user.get("planet_id") or 0) <= 0:
                raise RuntimeError("selected planet unavailable")
            planet_id = int(user["planet_id"])

            cur.execute(
                "SELECT object_id, object_amount, object_amount_progress, "
                "time_start, time_end FROM unit_queues "
                "WHERE planet_id=%s AND processed=0 ORDER BY time_start, id",
                (planet_id,),
            )
            unit_rows = cur.fetchall()

            cur.execute(
                "SELECT object_id, time_start, time_end FROM building_queues "
                "WHERE planet_id=%s AND processed=0 AND canceled=0 ORDER BY id",
                (planet_id,),
            )
            building_rows = cur.fetchall()

            cur.execute(
                "SELECT value FROM settings WHERE `key`='economy_speed' LIMIT 1"
            )
            setting = cur.fetchone()

        speed = fallback_speed
        if setting:
            try:
                speed = max(1, int(float(setting.get("value") or speed)))
            except (TypeError, ValueError):
                speed = fallback_speed

        items: list[UnitQueueItem] = []
        for row in unit_rows:
            requested = max(0, int(row.get("object_amount") or 0))
            completed = max(0, int(row.get("object_amount_progress") or 0))
            items.append(UnitQueueItem(
                object_id=int(row.get("object_id") or 0),
                requested_amount=requested,
                completed_amount=completed,
                remaining_amount=max(0, requested - completed),
                time_start=int(row.get("time_start") or 0),
                time_end=int(row.get("time_end") or 0),
            ))

        unit_end = max((item.time_end for item in items), default=now)
        building_end = max(
            (int(row.get("time_end") or 0) for row in building_rows),
            default=now,
        )
        return ProductionQueueSnapshot(
            available=True,
            email=email,
            planet_id=planet_id,
            observed_at=now,
            economy_speed=speed,
            building_queue_depth=len(building_rows),
            building_queue_seconds=max(0, building_end - now),
            building_targets=tuple(
                int(row.get("object_id") or 0) for row in building_rows
            ),
            unit_queue_depth=len(items),
            unit_queue_seconds=max(0, unit_end - now),
            unit_remaining_amount=sum(item.remaining_amount for item in items),
            unit_items=tuple(items),
        )
    except Exception as exc:  # noqa: BLE001 - unavailable state must fail closed
        return ProductionQueueSnapshot(
            available=False,
            email=email,
            planet_id=None,
            observed_at=now,
            economy_speed=fallback_speed,
            building_queue_depth=0,
            building_queue_seconds=0,
            building_targets=(),
            unit_queue_depth=0,
            unit_queue_seconds=0,
            unit_remaining_amount=0,
            unit_items=(),
            error=type(exc).__name__,
        )


# Stable short alias for modules that only need a snapshot.
queue_snapshot = production_queue_snapshot


def unit_construction_seconds(
    object_id: int,
    *,
    levels: Mapping[int, int],
    economy_speed: int,
) -> int | None:
    """OGameX ``PlanetService::getUnitConstructionTime`` in seconds."""
    structure = UNIT_STRUCTURE.get(int(object_id))
    if structure is None:
        return None
    shipyard = max(0, int(levels.get(SHIPYARD, 0) or 0))
    nanite = max(0, int(levels.get(NANITE_FACTORY, 0) or 0))
    speed = max(1, int(economy_speed or 1))
    seconds = int(
        (structure / (2500 * (1 + shipyard) * speed * (2**nanite))) * 3600
    )
    return max(1, seconds)


def cap_ship_amount_from_snapshot(
    snapshot: ProductionQueueSnapshot,
    object_id: int,
    requested: int,
    *,
    levels: Mapping[int, int],
    horizon_seconds: int | None = None,
) -> int:
    """Cap a dynamic batch against an already-read live queue snapshot."""
    try:
        amount = max(0, int(requested))
    except (TypeError, ValueError):
        return 0
    if amount == 0 or not snapshot.available:
        return 0
    # OGameX rejects units while Shipyard or Nanite is upgrading.
    if {SHIPYARD, NANITE_FACTORY}.intersection(snapshot.building_targets):
        return 0
    horizon = (
        default_ship_queue_horizon_seconds()
        if horizon_seconds is None
        else max(
            MIN_QUEUE_HORIZON_SECONDS,
            min(MAX_QUEUE_HORIZON_SECONDS, int(horizon_seconds)),
        )
    )
    if snapshot.unit_queue_seconds >= horizon:
        return 0
    per_unit = unit_construction_seconds(
        object_id, levels=levels, economy_speed=snapshot.economy_speed
    )
    if per_unit is None or not math.isfinite(per_unit) or per_unit <= 0:
        return 0
    capacity = max(0, (horizon - snapshot.unit_queue_seconds) // per_unit)
    return min(amount, capacity)


def cap_ship_amount(
    email: str,
    object_id: int,
    requested: int,
    *,
    levels: Mapping[int, int],
    horizon_seconds: int | None = None,
    planet_id: int | None = None,
) -> int:
    """Stable dynamic-batch API using the selected planet's live unit queue.

    Queue-read failure returns zero.  Tiny fixed bootstrap targets intentionally
    stay outside this function and are therefore unchanged by this fail-closed
    policy.
    """
    return cap_ship_amount_from_snapshot(
        production_queue_snapshot(email, planet_id=planet_id),
        object_id,
        requested,
        levels=levels,
        horizon_seconds=horizon_seconds,
    )


def pending_unit_amount(snapshot: ProductionQueueSnapshot, object_id: int) -> int:
    """Return unfinished quantity of one unit in an already-read snapshot."""
    wanted = int(object_id)
    return sum(
        item.remaining_amount for item in snapshot.unit_items
        if item.object_id == wanted
    )
