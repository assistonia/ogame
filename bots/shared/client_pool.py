"""Process-local, per-account :class:`OGameClient` reuse.

The universe scheduler runs account ticks in a thread pool.  A requests Session is
not safe to use concurrently, and OGameX stores the selected planet on the user,
so two ticks for the same account must never overlap.  ``OGameClientPool`` keeps
one client per email and holds that account's lock for the whole tick.

The first lease validates or creates the login.  Later leases call
``client.ensure_login()`` and therefore issue no validation GET while the Session
is known healthy.  OGameClient marks itself stale on a 419/login redirect, so an
expired Session still falls back to login on the next lease.
"""
from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from bots.shared.client import OGameClient


ClientFactory = Callable[[str, str], OGameClient]


@dataclass
class _ClientEntry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    client: OGameClient | None = None
    password: str | None = None


class OGameClientPool:
    """Reuse one client per email and serialize that account's complete tick."""

    def __init__(self, factory: ClientFactory | None = None) -> None:
        self._factory = factory or OGameClient
        self._entries: dict[str, _ClientEntry] = {}
        self._entries_lock = threading.Lock()

    def _entry(self, email: str) -> _ClientEntry:
        with self._entries_lock:
            entry = self._entries.get(email)
            if entry is None:
                entry = _ClientEntry()
                self._entries[email] = entry
            return entry

    @contextlib.contextmanager
    def lease(self, email: str, password: str) -> Iterator[OGameClient]:
        """Yield a logged-in client while holding the account single-flight lock.

        A password change replaces the cached client only after the previous lease
        has released the account lock.  Login failures propagate to the scheduler,
        which already converts account-level exceptions into ``SKIP`` results.
        """
        entry = self._entry(email)
        with entry.lock:
            if entry.client is None or entry.password != password:
                old = entry.client
                entry.client = self._factory(email, password)
                entry.password = password
                if old is not None:
                    try:
                        old.session.close()
                    except Exception:  # noqa: BLE001 - replacement must still proceed
                        pass
            client = entry.client
            client.ensure_login()
            yield client

    def clear(self) -> None:
        """Close and forget cached clients.

        Intended for orderly shutdown and tests; callers must not invoke this while
        leases are active.
        """
        with self._entries_lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.client is None:
                continue
            try:
                entry.client.session.close()
            except Exception:  # noqa: BLE001 - best-effort connection cleanup
                pass
