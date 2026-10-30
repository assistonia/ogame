"""Atomic per-action execution feedback for AI policy ticks.

The store is intentionally small and action-scoped: callers append one command
outcome at a time, and policy generation reads only a bounded recent window.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORE_VERSION = 1
DEFAULT_RECENT_LIMIT = 24
MAX_RECENT_LIMIT = 200
MAX_STORED_OUTCOMES = 200
MAX_RECORD_BYTES = 32 * 1024
DEFAULT_STATE_ROOT = Path(__file__).resolve().parent / "state"

LIFECYCLE_STATUSES = frozenset({
    "planned",
    "accepted",
    "queued",
    "completed",
    "stalled",
    "failed",
    "skipped",
})
_STATUS_LIFECYCLE = {
    "ready": "planned",
    "waiting": "planned",
    "deferred": "planned",
    "reserved": "accepted",
    "sending": "accepted",
    "executed": "queued",
    "dispatched": "queued",
    "sent": "queued",
    "success": "queued",
    "confirmed": "completed",
    "ambiguous": "stalled",
    "unknown": "stalled",
    "error": "failed",
    "lost": "failed",
    "rejected": "failed",
    "idle": "skipped",
    "disabled": "skipped",
}

_AGENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ACTION_FIELDS = (
    "occurred_at",
    "policy_activation_id",
    "plan",
    "action",
    "kind",
    "status",
    "lifecycle",
    "reason",
    "planet_id",
    "coordinates",
    "start_planet_id",
    "home_planet_id",
    "restored_planet_id",
    "restore_ok",
    "source_planet_id",
    "source_coordinates",
    "destination_planet_id",
    "destination_coordinates",
    "target_planet_id",
    "target_coordinates",
    "object_type",
    "object_id",
    "target_level",
    "target_total",
    "role",
    "resources",
    "reserves",
    "rationale",
    "expected_payoff",
    "attempt",
    "ambiguous",
    "executor",
    "details",
)


def _agent_name(agent: str) -> str:
    name = str(agent).strip().split("@", 1)[0]
    if not _AGENT_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(f"invalid agent name: {agent!r}")
    return name


def _store_path(agent: str, state_root: str | os.PathLike[str] | None) -> Path:
    root = Path(state_root) if state_root is not None else DEFAULT_STATE_ROOT
    return root / _agent_name(agent) / "action_feedback.json"


def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "next_sequence": 1, "outcomes": []}


def _read_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        raise ValueError("unsupported or malformed action feedback store")
    outcomes = data.get("outcomes")
    if not isinstance(outcomes, list) or not all(isinstance(row, dict) for row in outcomes):
        raise ValueError("malformed action feedback outcomes")
    next_sequence = data.get("next_sequence", 1)
    if not isinstance(next_sequence, int) or isinstance(next_sequence, bool) or next_sequence < 1:
        raise ValueError("malformed action feedback sequence")
    return {
        "version": STORE_VERSION,
        "next_sequence": next_sequence,
        "outcomes": outcomes,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=".action_feedback.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _lock(path: Path, exclusive: bool):
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(descriptor, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return handle


def lifecycle_status(status: Any) -> str:
    """Map legacy executor statuses onto the stable action lifecycle."""
    normalized = str(status or "").strip().lower()
    if normalized in LIFECYCLE_STATUSES:
        return normalized
    return _STATUS_LIFECYCLE.get(normalized, "stalled")


def _normalize_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(outcome, Mapping):
        raise TypeError("outcome must be a mapping for one action command")
    command_id = outcome.get("command_id")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("action outcome requires a non-empty command_id")
    status = outcome.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("action outcome requires a non-empty status")
    action = outcome.get("action") or outcome.get("plan") or outcome.get("kind")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action outcome requires action, plan, or kind")

    record: dict[str, Any] = {
        "feedback_version": STORE_VERSION,
        "recorded_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "command_id": command_id.strip(),
    }
    for field in _ACTION_FIELDS:
        if field in outcome:
            record[field] = outcome[field]
    record["action"] = action.strip()
    record["status"] = status.strip().lower()
    requested_lifecycle = outcome.get("lifecycle")
    if requested_lifecycle is None:
        record["lifecycle"] = lifecycle_status(record["status"])
    else:
        lifecycle = str(requested_lifecycle).strip().lower()
        if lifecycle not in LIFECYCLE_STATUSES:
            raise ValueError(
                "action outcome lifecycle must be planned, accepted, queued, "
                "completed, stalled, failed, or skipped"
            )
        record["lifecycle"] = lifecycle

    try:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("action outcome must contain only JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError(f"action outcome exceeds {MAX_RECORD_BYTES} bytes")
    return json.loads(encoded)


def _transition_identity(record: Mapping[str, Any]) -> str:
    """Stable identity for one lifecycle transition, excluding wall-clock metadata."""
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"feedback_version", "recorded_at", "sequence", "occurred_at"}
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def append_action_outcome(
    agent: str,
    outcome: Mapping[str, Any],
    *,
    state_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Atomically append one action result and return the stored record."""
    record = _normalize_outcome(outcome)
    path = _store_path(agent, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock(path, exclusive=True)
    try:
        store = _read_store(path)
        transition = _transition_identity(record)
        duplicate = next(
            (
                existing
                for existing in reversed(store["outcomes"])
                if _transition_identity(existing) == transition
            ),
            None,
        )
        if duplicate is not None:
            return json.loads(json.dumps(duplicate, ensure_ascii=False))
        sequence = store["next_sequence"]
        record["sequence"] = sequence
        outcomes = [*store["outcomes"], record][-MAX_STORED_OUTCOMES:]
        _atomic_write(
            path,
            {
                "version": STORE_VERSION,
                "next_sequence": sequence + 1,
                "outcomes": outcomes,
            },
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return dict(record)


def append_action_transition_if_current(
    agent: str,
    outcome: Mapping[str, Any],
    *,
    expected_lifecycles: set[str] | frozenset[str],
    state_root: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append a transition only while the command is still in an expected state.

    Reconciliation can race with another policy or snapshot process.  The
    compare-and-append therefore shares the action store's exclusive lock rather
    than relying on a prior unlocked read.
    """
    expected = {
        str(value).strip().lower() for value in expected_lifecycles
    }
    if not expected or not expected.issubset(LIFECYCLE_STATUSES):
        raise ValueError("expected_lifecycles must contain valid lifecycle states")
    record = _normalize_outcome(outcome)
    path = _store_path(agent, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock(path, exclusive=True)
    try:
        store = _read_store(path)
        latest = next(
            (
                existing
                for existing in reversed(store["outcomes"])
                if existing.get("command_id") == record["command_id"]
            ),
            None,
        )
        if latest is None or lifecycle_status(
            latest.get("lifecycle", latest.get("status"))
        ) not in expected:
            return (
                json.loads(json.dumps(latest, ensure_ascii=False))
                if latest is not None
                else {},
                False,
            )

        transition = _transition_identity(record)
        duplicate = next(
            (
                existing
                for existing in reversed(store["outcomes"])
                if _transition_identity(existing) == transition
            ),
            None,
        )
        if duplicate is not None:
            return json.loads(json.dumps(duplicate, ensure_ascii=False)), False

        sequence = store["next_sequence"]
        record["sequence"] = sequence
        outcomes = [*store["outcomes"], record][-MAX_STORED_OUTCOMES:]
        _atomic_write(
            path,
            {
                "version": STORE_VERSION,
                "next_sequence": sequence + 1,
                "outcomes": outcomes,
            },
        )
        return dict(record), True
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def load_recent_action_outcomes(
    agent: str,
    limit: int = DEFAULT_RECENT_LIMIT,
    *,
    state_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Load at most ``limit`` action results, newest first."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_RECENT_LIMIT:
        raise ValueError(f"limit must be an integer from 0 to {MAX_RECENT_LIMIT}")
    if limit == 0:
        return []
    path = _store_path(agent, state_root)
    if not path.exists():
        return []
    lock = _lock(path, exclusive=False)
    try:
        outcomes = _read_store(path)["outcomes"]
        recent = list(reversed(outcomes[-limit:]))
        return json.loads(json.dumps(recent, ensure_ascii=False))
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
