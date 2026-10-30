"""Bounded AI diplomacy over OGameX direct chat.

Only policy-engine AI accounts may initiate diplomacy. Their peers are other
policy-engine AIs or real player accounts; rule bots and system/admin-test
accounts are deliberately excluded. Free-form chat is carried as untrusted game
data, while non-aggression pacts use a small machine-readable contract ledger.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bots.shared import db_reader


LEDGER_VERSION = 1
LEDGER_PATH = Path(__file__).resolve().parent / "state" / "diplomacy_ledger.json"
AI_ACCOUNT_RE = re.compile(r"^ai\d+$", re.IGNORECASE)
NAMED_AI_ACCOUNTS = frozenset({"pirate01", "explorer01"})
SYSTEM_EMAILS = frozenset({
    "legor@ogamex.local",
    "simulation-admin@ogamex.local",
})
SYSTEM_USERNAMES = frozenset({"legor"})
NON_PLAYER_DOMAINS = frozenset({"example.com", "ogamex.dev", "ogamex.local"})
MAX_MESSAGE_CHARS = 400
MAX_PLANS = 8
MAX_OUTBOUND_PER_TICK = 1
GLOBAL_MESSAGE_COOLDOWN_SEC = 1800
TARGET_MESSAGE_COOLDOWN_SEC = 7200
MAX_CONTRACT_HOURS = 168
ALLOWED_KINDS = frozenset({
    "message",
    "propose_nap",
    "accept_nap",
    "reject_nap",
})


def _account_name(email: str) -> str:
    return str(email).strip().lower().split("@", 1)[0]


def is_autonomous_ai(email: str) -> bool:
    normalized = str(email).strip().lower()
    if not normalized.endswith("@bots.local"):
        return False
    name = _account_name(normalized)
    return name in NAMED_AI_ACCOUNTS or bool(AI_ACCOUNT_RE.fullmatch(name))


def participant_kind(email: str, username: str = "") -> str | None:
    normalized = str(email).strip().lower()
    if is_autonomous_ai(normalized):
        return "ai"
    domain = normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
    if (
        not normalized
        or normalized.endswith("@bots.local")
        or domain in NON_PLAYER_DOMAINS
        or normalized in SYSTEM_EMAILS
        or str(username).strip().lower() in SYSTEM_USERNAMES
    ):
        return None
    return "human"


def _empty_ledger() -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "sent_commands": {},
        "last_sent": {},
        "contracts": [],
    }


def _read_ledger(path: Path = LEDGER_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_ledger()
    if not isinstance(raw, dict) or raw.get("version") != LEDGER_VERSION:
        return _empty_ledger()
    return {
        "version": LEDGER_VERSION,
        "sent_commands": (
            dict(raw.get("sent_commands"))
            if isinstance(raw.get("sent_commands"), dict)
            else {}
        ),
        "last_sent": (
            dict(raw.get("last_sent"))
            if isinstance(raw.get("last_sent"), dict)
            else {}
        ),
        "contracts": [
            dict(item)
            for item in raw.get("contracts", [])
            if isinstance(item, dict)
        ],
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".diplomacy.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _ledger_lock(path: Path = LEDGER_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(descriptor, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:MAX_MESSAGE_CHARS]


def clean_diplomacy_policy(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    messages = source.get("messages")
    if not isinstance(messages, list):
        messages = []
    clean: list[dict[str, Any]] = []
    for item in messages[:MAX_PLANS]:
        if not isinstance(item, Mapping):
            continue
        command_id = item.get("command_id")
        recipient_id = item.get("recipient_id")
        kind = item.get("kind", "message")
        if (
            not isinstance(command_id, str)
            or not command_id.strip()
            or len(command_id.strip()) > 128
            or not isinstance(recipient_id, int)
            or isinstance(recipient_id, bool)
            or recipient_id <= 0
            or kind not in ALLOWED_KINDS
        ):
            continue
        plan: dict[str, Any] = {
            "command_id": command_id.strip(),
            "recipient_id": int(recipient_id),
            "kind": kind,
            "text": _clean_text(item.get("text")),
        }
        if kind != "message":
            contract_id = item.get("contract_id")
            if (
                not isinstance(contract_id, str)
                or not contract_id.strip()
                or len(contract_id.strip()) > 96
            ):
                continue
            plan["contract_id"] = contract_id.strip()
        if kind == "propose_nap":
            duration = item.get("duration_hours", 24)
            if not isinstance(duration, int) or isinstance(duration, bool):
                continue
            plan["duration_hours"] = max(1, min(MAX_CONTRACT_HOURS, duration))
        if plan["text"] or kind != "message":
            clean.append(plan)
    return {"version": 1, "messages": clean}


def _participant_rows() -> list[dict[str, Any]]:
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, username FROM users "
            "WHERE planet_current IS NOT NULL ORDER BY id"
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def eligible_participants(email: str) -> list[dict[str, Any]]:
    own = str(email).strip().lower()
    out: list[dict[str, Any]] = []
    for row in _participant_rows():
        peer_email = str(row.get("email") or "").strip().lower()
        kind = participant_kind(peer_email, str(row.get("username") or ""))
        if kind is None or peer_email == own:
            continue
        out.append({
            "player_id": int(row["id"]),
            "username": str(row.get("username") or ""),
            "email": peer_email,
            "kind": kind,
        })
    return out


def _contract_snapshot(email: str, ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized = str(email).strip().lower()
    now = time.time()
    out: list[dict[str, Any]] = []
    for item in ledger.get("contracts", []):
        if not isinstance(item, Mapping):
            continue
        if normalized not in {
            str(item.get("proposer_email") or "").lower(),
            str(item.get("recipient_email") or "").lower(),
        }:
            continue
        row = dict(item)
        if row.get("status") == "active":
            try:
                if float(row.get("expires_at_ts", 0)) <= now:
                    row["status"] = "expired"
            except (TypeError, ValueError):
                row["status"] = "expired"
        row.pop("expires_at_ts", None)
        out.append(row)
    return out[-20:]


def snapshot(email: str, *, limit: int = 20) -> dict[str, Any]:
    """Return eligible peers, bounded direct-chat history, and NAP contracts."""
    if not is_autonomous_ai(email):
        return {
            "status": "disabled",
            "reason": "only-autonomous-ai-has-diplomacy-context",
            "eligible_participants": [],
            "recent_messages": [],
            "contracts": [],
        }
    participants = eligible_participants(email)
    by_id = {int(row["player_id"]): row for row in participants}
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        own_row = cur.fetchone()
        if not own_row:
            raise RuntimeError("diplomacy account not found")
        own_id = int(own_row["id"])
        peer_ids = sorted(by_id)
        messages: list[dict[str, Any]] = []
        if peer_ids:
            placeholders = ",".join(["%s"] * len(peer_ids))
            cur.execute(
                "SELECT id, sender_id, recipient_id, message, created_at "
                "FROM chat_messages WHERE alliance_id IS NULL AND ("
                f"(recipient_id=%s AND sender_id IN ({placeholders})) OR "
                f"(sender_id=%s AND recipient_id IN ({placeholders}))) "
                "ORDER BY id DESC LIMIT %s",
                (own_id, *peer_ids, own_id, *peer_ids, max(1, min(50, int(limit)))),
            )
            for row in cur.fetchall():
                sender_id = int(row["sender_id"])
                peer_id = (
                    int(row["recipient_id"]) if sender_id == own_id else sender_id
                )
                peer = by_id.get(peer_id)
                if peer is None:
                    continue
                messages.append({
                    "message_id": int(row["id"]),
                    "direction": "outbound" if sender_id == own_id else "inbound",
                    "peer_id": peer_id,
                    "peer_username": peer["username"],
                    "peer_kind": peer["kind"],
                    "text": _clean_text(row.get("message")),
                    "created_at": str(row.get("created_at") or ""),
                    "untrusted": True,
                })
    ledger = _read_ledger()
    return {
        "status": "ready",
        "eligibility": "AI↔AI and AI↔human only; rule bots excluded",
        "eligible_participants": participants,
        "recent_messages": messages,
        "contracts": _contract_snapshot(email, ledger),
        "rate_limits": {
            "max_outbound_per_tick": MAX_OUTBOUND_PER_TICK,
            "global_cooldown_sec": GLOBAL_MESSAGE_COOLDOWN_SEC,
            "target_cooldown_sec": TARGET_MESSAGE_COOLDOWN_SEC,
        },
    }


def _find_contract(ledger: Mapping[str, Any], contract_id: str) -> dict | None:
    for item in ledger.get("contracts", []):
        if isinstance(item, dict) and item.get("contract_id") == contract_id:
            return item
    return None


def _render_message(plan: Mapping[str, Any]) -> str:
    kind = plan["kind"]
    text = _clean_text(plan.get("text"))
    if kind == "message":
        return text
    contract_id = str(plan["contract_id"])
    if kind == "propose_nap":
        prefix = f"[NAP 제안 {contract_id} / {plan['duration_hours']}시간]"
    elif kind == "accept_nap":
        prefix = f"[NAP 수락 {contract_id}]"
    else:
        prefix = f"[NAP 거절 {contract_id}]"
    return f"{prefix} {text}".strip()[:MAX_MESSAGE_CHARS]


def execute_policy(
    client,
    email: str,
    raw_policy: Any,
    *,
    now: float | None = None,
    ledger_path: Path = LEDGER_PATH,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate and send at most one diplomacy message for an AI account."""
    if not is_autonomous_ai(email):
        return ([{
            "action": "diplomacy",
            "status": "rejected",
            "reason": "sender-not-autonomous-ai",
            "command_id": "diplomacy-disabled",
        }], [])
    policy = clean_diplomacy_policy(raw_policy)
    plans = policy["messages"][:MAX_OUTBOUND_PER_TICK]
    if not plans:
        return [], []
    current = time.time() if now is None else float(now)
    peers = {row["player_id"]: row for row in eligible_participants(email)}
    outcomes: list[dict[str, Any]] = []
    notes: list[str] = []

    lock = _ledger_lock(ledger_path)
    try:
        ledger = _read_ledger(ledger_path)
        for plan in plans:
            command_id = plan["command_id"]
            recipient_id = int(plan["recipient_id"])
            peer = peers.get(recipient_id)
            outcome = {
                "action": "diplomacy",
                "kind": plan["kind"],
                "command_id": command_id,
                "target_player_id": recipient_id,
            }
            if command_id in ledger["sent_commands"]:
                outcomes.append({
                    **outcome,
                    "status": "completed",
                    "reason": "duplicate-command-already-sent",
                })
                continue
            if peer is None:
                outcomes.append({
                    **outcome,
                    "status": "rejected",
                    "reason": "recipient-not-ai-or-human",
                })
                continue

            own_key = str(email).strip().lower()
            target_key = f"{own_key}|{peer['email']}"
            last_global = float(ledger["last_sent"].get(own_key, 0) or 0)
            last_target = float(ledger["last_sent"].get(target_key, 0) or 0)
            if (
                last_global > 0
                and current - last_global < GLOBAL_MESSAGE_COOLDOWN_SEC
            ):
                outcomes.append({
                    **outcome,
                    "status": "deferred",
                    "reason": "global-message-cooldown",
                })
                continue
            if (
                last_target > 0
                and current - last_target < TARGET_MESSAGE_COOLDOWN_SEC
            ):
                outcomes.append({
                    **outcome,
                    "status": "deferred",
                    "reason": "target-message-cooldown",
                })
                continue

            contract = None
            contract_id = plan.get("contract_id")
            if plan["kind"] == "propose_nap":
                if _find_contract(ledger, str(contract_id)) is not None:
                    outcomes.append({
                        **outcome,
                        "status": "rejected",
                        "reason": "contract-id-already-exists",
                    })
                    continue
                contract = {
                    "contract_id": contract_id,
                    "type": "nap",
                    "status": "pending",
                    "proposer_email": own_key,
                    "recipient_email": peer["email"],
                    "duration_hours": int(plan["duration_hours"]),
                    "created_at": dt.datetime.fromtimestamp(
                        current, tz=dt.timezone.utc
                    ).isoformat(),
                }
            elif plan["kind"] in {"accept_nap", "reject_nap"}:
                contract = _find_contract(ledger, str(contract_id))
                if (
                    contract is None
                    or contract.get("status") != "pending"
                    or contract.get("recipient_email") != own_key
                    or contract.get("proposer_email") != peer["email"]
                ):
                    outcomes.append({
                        **outcome,
                        "status": "rejected",
                        "reason": "pending-contract-not-found-for-parties",
                    })
                    continue

            response = client.send_direct_message(
                recipient_id, _render_message(plan)
            )
            if not isinstance(response, Mapping) or response.get("status") != "OK":
                outcomes.append({
                    **outcome,
                    "status": "failed",
                    "reason": str(
                        response.get("status", "invalid-chat-response")
                        if isinstance(response, Mapping)
                        else "invalid-chat-response"
                    ),
                })
                continue

            if plan["kind"] == "propose_nap" and contract is not None:
                ledger["contracts"].append(contract)
            elif plan["kind"] == "accept_nap" and contract is not None:
                duration = int(contract.get("duration_hours", 24))
                contract["status"] = "active"
                contract["accepted_at"] = dt.datetime.fromtimestamp(
                    current, tz=dt.timezone.utc
                ).isoformat()
                contract["expires_at_ts"] = current + duration * 3600
                contract["expires_at"] = dt.datetime.fromtimestamp(
                    contract["expires_at_ts"], tz=dt.timezone.utc
                ).isoformat()
            elif plan["kind"] == "reject_nap" and contract is not None:
                contract["status"] = "rejected"
                contract["rejected_at"] = dt.datetime.fromtimestamp(
                    current, tz=dt.timezone.utc
                ).isoformat()

            ledger["sent_commands"][command_id] = {
                "sent_at": current,
                "message_id": response.get("id"),
                "recipient_email": peer["email"],
            }
            ledger["last_sent"][own_key] = current
            ledger["last_sent"][target_key] = current
            outcomes.append({
                **outcome,
                "status": "completed",
                "reason": "message-sent",
                "details": {
                    "message_id": response.get("id"),
                    "recipient_email": peer["email"],
                    "recipient_kind": peer["kind"],
                    "contract_id": contract_id,
                },
            })
            notes.append(
                f"diplomacy {plan['kind']} -> {peer['username']}"
            )
        _atomic_write(ledger_path, ledger)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return outcomes, notes


def blocked_player_ids(
    email: str,
    *,
    now: float | None = None,
    ledger_path: Path = LEDGER_PATH,
) -> set[int]:
    """Return other parties covered by an active NAP, resolved to game user IDs."""
    normalized = str(email).strip().lower()
    current = time.time() if now is None else float(now)
    peer_emails: set[str] = set()
    ledger = _read_ledger(ledger_path)
    for contract in ledger.get("contracts", []):
        if not isinstance(contract, Mapping) or contract.get("status") != "active":
            continue
        try:
            if float(contract.get("expires_at_ts", 0)) <= current:
                continue
        except (TypeError, ValueError):
            continue
        proposer = str(contract.get("proposer_email") or "").lower()
        recipient = str(contract.get("recipient_email") or "").lower()
        if normalized == proposer:
            peer_emails.add(recipient)
        elif normalized == recipient:
            peer_emails.add(proposer)
    if not peer_emails:
        return set()
    conn = db_reader._conn()
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(peer_emails))
        cur.execute(
            f"SELECT id FROM users WHERE email IN ({placeholders})",
            tuple(sorted(peer_emails)),
        )
        return {int(row["id"]) for row in cur.fetchall()}
