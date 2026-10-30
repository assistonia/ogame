from __future__ import annotations

import concurrent.futures
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared.client_pool import OGameClientPool


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.session = _Session()
        self.ensure_calls = 0

    def ensure_login(self) -> None:
        self.ensure_calls += 1


def test_pool_reuses_one_client_without_revalidating_itself():
    created: list[_Client] = []

    def factory(email: str, password: str) -> _Client:
        client = _Client(email, password)
        created.append(client)
        return client

    pool = OGameClientPool(factory=factory)
    with pool.lease("bot001@bots.local", "pw") as first:
        pass
    with pool.lease("bot001@bots.local", "pw") as second:
        pass

    assert first is second
    assert created == [first]
    # ensure_login is a cheap state check on a healthy real OGameClient; the pool
    # does not call the validating login() method on every lease.
    assert first.ensure_calls == 2


def test_same_account_lease_is_single_flight():
    pool = OGameClientPool(factory=_Client)
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    def first_tick() -> None:
        with pool.lease("bot001@bots.local", "pw"):
            first_inside.set()
            assert release_first.wait(timeout=2)

    def second_tick() -> None:
        assert first_inside.wait(timeout=2)
        with pool.lease("bot001@bots.local", "pw"):
            second_inside.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_tick)
        assert first_inside.wait(timeout=2)
        second = executor.submit(second_tick)
        assert not second_inside.wait(timeout=0.05)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_inside.is_set()


def test_password_change_replaces_and_closes_cached_client():
    pool = OGameClientPool(factory=_Client)
    with pool.lease("bot001@bots.local", "old") as old:
        pass
    with pool.lease("bot001@bots.local", "new") as new:
        pass

    assert new is not old
    assert old.session.closed is True
    assert new.password == "new"
