"""Opt-in client for OGameX's local bot economy batch endpoint.

The ordinary browser endpoints remain the compatibility path.  This module is
deliberately gated by both a process flag and an account allow-list so rollout can
move from one AI account to all ten and then to rule accounts without changing any
planner policy.

``None`` means the caller may safely use the legacy endpoint: batching was disabled
or the server returned its feature-hidden 404 before executing actions.  Ambiguous
transport/server failures raise instead; falling back after an unknown partial
execution could enqueue the same action twice.  Retries reuse the same request id,
which is what the server-side idempotency record is for.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import requests


ENDPOINT = "/ajax/bot/v1/economy-batch"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNSUPPORTED_TTL_SEC = 60.0
_unsupported_until: dict[tuple[str, str], float] = {}
_unsupported_lock = threading.Lock()


class EconomyBatchError(RuntimeError):
    """A batch request failed in a way where legacy replay is not known safe."""


def enabled_for_account(email: str) -> bool:
    """Return whether this process should batch ``email``.

    An explicit account allow-list is required even when the master flag is on.
    ``*`` opts every account in; entries are comma-separated and case-insensitive.
    """
    enabled = os.environ.get("OGAME_ECONOMY_BATCH_ENABLED", "").strip().lower()
    if enabled not in _TRUTHY:
        return False
    raw = os.environ.get("OGAME_ECONOMY_BATCH_ACCOUNTS", "")
    allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return "*" in allowed or email.lower() in allowed


def _server_known_unsupported(client, now: float) -> bool:
    key = (str(client.base_url), str(client.email).lower())
    with _unsupported_lock:
        until = _unsupported_until.get(key, 0.0)
        if until <= now:
            _unsupported_until.pop(key, None)
            return False
        return True


def _remember_unsupported(client, now: float) -> None:
    key = (str(client.base_url), str(client.email).lower())
    with _unsupported_lock:
        _unsupported_until[key] = now + _UNSUPPORTED_TTL_SEC


def clear_capability_cache() -> None:
    """Clear feature-hidden 404 memory (tests and controlled rollout flips)."""
    with _unsupported_lock:
        _unsupported_until.clear()


def try_economy_batch(
    client,
    actions: list[dict[str, Any]],
    *,
    max_success: int,
    stop_on_queue_full: bool = True,
    budget: dict[str, int] | None = None,
    request_id: str | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any] | None:
    """Execute ordered economy candidates in one authenticated request.

    Returns the decoded server response, or ``None`` when legacy execution is safe.
    A retry (auth repair, connection error, or 5xx) always reuses ``request_id``.
    """
    if not actions or not enabled_for_account(client.email):
        return None
    now = time.monotonic()
    if _server_known_unsupported(client, now):
        return None

    rid = request_id or uuid.uuid4().hex
    payload: dict[str, Any] = {
        "request_id": rid,
        "max_success": int(max_success),
        "stop_on_queue_full": bool(stop_on_queue_full),
        "actions": actions,
    }
    if budget is not None:
        payload["budget"] = {
            name: max(0, int(budget.get(name, 0)))
            for name in ("metal", "crystal", "deuterium")
        }

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            if not client._token:  # shared Laravel-session token cache
                client._token = client._csrf_token("/overview")
            body = dict(payload)
            body["_token"] = client._token
            response = client.session.post(
                f"{client.base_url}{ENDPOINT}",
                json=body,
                headers={
                    "Accept": "application/json",
                    "X-CSRF-TOKEN": client._token,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=timeout_sec,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                continue
            raise EconomyBatchError(
                f"economy batch transport failed ({type(exc).__name__})"
            ) from exc

        if response.status_code == 404:
            # Server contract hides OFF/non-local/non-bot before action execution.
            _remember_unsupported(client, time.monotonic())
            return None
        if response.status_code in (401, 419) and attempt == 0:
            # Laravel's auth middleware may return JSON 401 because this endpoint
            # explicitly requests JSON.  Like CSRF 419, it rejects before the
            # controller can execute an action, so retrying the same idempotency id
            # after bounded auth repair is safe.
            client._recover_auth(force_relogin=response.status_code == 401)
            continue
        if client._is_login_response(response) and attempt == 0:
            client._recover_auth(force_relogin=True)
            continue
        if response.status_code >= 500 and attempt == 0:
            last_error = EconomyBatchError(
                f"economy batch server error {response.status_code}"
            )
            continue
        if response.status_code == 422:
            # BotEconomyBatchController validation runs before the service and its
            # idempotency/action loop.  Only that explicit response shape is known
            # safe for the browser-compatible legacy path; an arbitrary 422 remains
            # ambiguous and must not be replayed.
            try:
                validation = response.json()
            except ValueError:
                validation = None
            if (
                isinstance(validation, dict)
                and validation.get("status") == "invalid_request"
                and validation.get("code") == "validation_failed"
            ):
                return None
        if response.status_code != 200:
            raise EconomyBatchError(
                f"economy batch rejected HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            decoded = response.json()
        except ValueError as exc:
            raise EconomyBatchError("economy batch returned non-JSON response") from exc
        if not isinstance(decoded, dict) or decoded.get("request_id") != rid:
            raise EconomyBatchError("economy batch response/request id mismatch")
        if not isinstance(decoded.get("results"), list):
            raise EconomyBatchError("economy batch response has no results list")
        return decoded

    if last_error is not None:
        raise EconomyBatchError(str(last_error)) from last_error
    raise EconomyBatchError("economy batch authentication recovery failed")
