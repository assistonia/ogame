"""Import settled OGameX mission messages into the AI action feedback store."""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bots.ai_agents import action_feedback
from bots.shared import db_reader


STATE_VERSION = 1
DEFAULT_STATE_ROOT = Path(__file__).resolve().parent / "state"
MAX_IMPORT_PER_TICK = 200

EXPEDITION_KEYS = frozenset({
    "expedition_battle",
    "expedition_battle_pirates",
    "expedition_battle_aliens",
    "expedition_gain_dark_matter",
    "expedition_failed",
    "expedition_failed_and_delay",
    "expedition_loss_of_fleet",
    "expedition_failed_and_speedup",
    "expedition_gain_item",
    "expedition_gain_resources",
    "expedition_gain_ships",
    "expedition_merchant_found",
})
ACTION_BY_KEY = {
    "espionage_report": "spy",
    "fleet_lost_contact": "fleet",
    "debris_field_harvest": "harvest",
    "colony_established": "colonize",
    "colony_establish_fail_astrophysics": "colonize",
}


def _agent_name(email: str) -> str:
    return str(email).strip().split("@", 1)[0]


def _state_path(email: str, state_root: Path = DEFAULT_STATE_ROOT) -> Path:
    return state_root / _agent_name(email) / "game_outcome_cursor.json"


def _read_cursor(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(value, Mapping) or value.get("version") != STATE_VERSION:
        return 0
    cursor = value.get("last_message_id", 0)
    return int(cursor) if isinstance(cursor, int) and cursor >= 0 else 0


def _write_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".game_outcome.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": STATE_VERSION, "last_message_id": int(cursor)},
                handle,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _classify(row: Mapping[str, Any]) -> tuple[str, str, bool] | None:
    key = str(row.get("key") or "")
    if key in EXPEDITION_KEYS:
        status = "lost" if key == "expedition_loss_of_fleet" else "completed"
        return "expedition", status, False
    action = ACTION_BY_KEY.get(key)
    if action is None:
        return None
    if key == "fleet_lost_contact":
        # The server message lacks mission type. Raider's state correlator handles
        # probe quarantine; feedback keeps this explicitly ambiguous.
        return action, "lost", True
    if key == "colony_establish_fail_astrophysics":
        return action, "failed", False
    return action, "completed", False


def _coordinate_label(value: Mapping[str, Any]) -> str | None:
    text = str(value.get("coordinates") or "")
    match = re.search(r"(\d+):(\d+):(\d+)", text)
    return ":".join(match.groups()) if match else None


def _pending_dispatch_command(
    email: str,
    action: str,
    message_params: Mapping[str, Any],
    *,
    state_root: Path,
) -> str | None:
    """Find the oldest still-unsettled legacy dispatch for this result."""
    try:
        recent = action_feedback.load_recent_action_outcomes(
            email, limit=100, state_root=state_root
        )
    except Exception:  # noqa: BLE001 - correlation is optional, import is not
        return None
    terminal = {
        str(row.get("command_id"))
        for row in recent
        if row.get("status") in {"completed", "lost", "failed"}
    }
    target = _coordinate_label(message_params)
    candidates: list[dict[str, Any]] = []
    for row in recent:
        command_id = row.get("command_id")
        if (
            row.get("action") != action
            or row.get("status") != "dispatched"
            or not isinstance(command_id, str)
            or command_id in terminal
        ):
            continue
        if target is not None:
            details = row.get("details")
            note = str(details.get("note") or "") if isinstance(details, Mapping) else ""
            if target not in note:
                continue
        candidates.append(row)
    if not candidates:
        return None
    # load_recent is newest-first; fleet results settle in dispatch order.
    return str(candidates[-1]["command_id"])


def sync_game_outcomes(
    email: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    """Append newly settled mission outcomes and advance an account-local cursor."""
    path = _state_path(email, state_root)
    cursor = _read_cursor(path)
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.key, m.params, m.espionage_report_id, "
            "m.battle_report_id, m.created_at "
            "FROM messages m JOIN users u ON u.id=m.user_id "
            "WHERE u.email=%s AND m.id>%s ORDER BY m.id LIMIT %s",
            (email, cursor, MAX_IMPORT_PER_TICK),
        )
        rows = cur.fetchall()

    imported = 0
    last_seen = cursor
    for row in rows:
        try:
            message_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        classified = _classify(row)
        if classified is None:
            last_seen = max(last_seen, message_id)
            continue
        action, status, ambiguous = classified
        parsed_params = _params(row.get("params"))
        dispatch_command = (
            _pending_dispatch_command(
                email, action, parsed_params, state_root=state_root
            )
            if action != "fleet"
            else None
        )
        details = {
            "message_id": message_id,
            "message_key": str(row.get("key") or ""),
            "params": parsed_params,
            "espionage_report_id": row.get("espionage_report_id"),
            "battle_report_id": row.get("battle_report_id"),
            "settled_at": str(row.get("created_at") or ""),
            "dispatch_command_id": dispatch_command,
        }
        try:
            action_feedback.append_action_outcome(email, {
                "command_id": dispatch_command or f"game-message:{message_id}",
                "action": action,
                "status": status,
                "ambiguous": ambiguous,
                "details": details,
            }, state_root=state_root)
        except Exception:  # noqa: BLE001 - one malformed message must not stop cursor
            break
        imported += 1
        last_seen = max(last_seen, message_id)
    if last_seen > cursor:
        _write_cursor(path, last_seen)
    return {
        "status": "ready",
        "imported": imported,
        "last_message_id": last_seen,
    }
