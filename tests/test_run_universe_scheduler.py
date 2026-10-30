from __future__ import annotations

import argparse
import contextlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import run_universe
from scripts.universe_schedule import SpreadSchedule, stable_spread_offsets


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = float(now)

    def time(self) -> float:
        return self.now

    def advance_to(self, value: float) -> None:
        assert value >= self.now
        self.now = float(value)


def _emails(count: int = 102) -> list[str]:
    return [f"bot{i:03d}@bots.local" for i in range(1, count + 1)]


def test_stable_hash_rank_produces_exact_even_240_second_slots():
    emails = _emails()
    first = stable_spread_offsets(emails, 240)
    second = stable_spread_offsets(list(reversed(emails)), 240)

    assert {row.email: row.offset_sec for row in first} == {
        row.email: row.offset_sec for row in second
    }
    offsets = sorted(row.offset_sec for row in first)
    assert offsets == pytest.approx([index * 240 / 102 for index in range(102)])
    assert offsets[0] == 0
    assert offsets[-1] < 240


def test_fake_clock_submits_each_account_only_at_its_fixed_epoch_due_time():
    clock = FakeClock(2400.0)
    schedule = SpreadSchedule(
        _emails(), period_sec=240, spread_sec=240,
        now=clock.time(), base_tick_index=777,
    )
    seen = []

    while len(seen) < 102:
        clock.advance_to(schedule.next_due_at)
        batch = schedule.take_due(clock.time())
        assert not batch.overrun
        for item in batch.ready:
            seen.append(item)
            schedule.complete(item.email)

    assert {item.email for item in seen} == set(_emails())
    assert all(item.cycle == 10 for item in seen)
    assert all(item.tick_index == 777 for item in seen)
    assert [item.due_at for item in seen] == pytest.approx(
        [2400 + index * 240 / 102 for index in range(102)]
    )


def test_fake_clock_coalesces_same_account_overrun_without_catchup():
    clock = FakeClock(0)
    schedule = SpreadSchedule(
        ["bot001@bots.local"], period_sec=240, spread_sec=240,
        now=clock.time(), base_tick_index=10,
    )

    first = schedule.take_due(clock.time())
    assert [item.tick_index for item in first.ready] == [10]

    clock.advance_to(240)
    second = schedule.take_due(clock.time())
    assert not second.ready
    assert [item.tick_index for item in second.overrun] == [11]

    schedule.complete("bot001@bots.local")
    clock.advance_to(480)
    third = schedule.take_due(clock.time())
    assert [item.tick_index for item in third.ready] == [12]
    assert not third.overrun


def test_startup_does_not_submit_slots_already_in_the_past():
    schedule = SpreadSchedule(
        ["a@bots.local", "b@bots.local"],
        period_sec=240,
        spread_sec=240,
        now=100,
        base_tick_index=20,
    )

    assert not schedule.take_due(100).ready
    assert schedule.next_due_at > 100


def test_once_and_zero_spread_keep_original_pass_scheduler():
    once = run_universe.parse_args(["--once", "--spread-sec", "240"])
    loop_zero = run_universe.parse_args(["--loop", "--spread-sec", "0"])
    loop_spread = run_universe.parse_args(["--loop", "--spread-sec", "240"])

    assert run_universe.use_spread_scheduler(once) is False
    assert run_universe.use_spread_scheduler(loop_zero) is False
    assert run_universe.use_spread_scheduler(loop_spread) is True


def test_zero_spread_sequential_pass_preserves_account_call_order(monkeypatch):
    calls = []

    def fake_tick(email, _args, tick_index):
        calls.append((email, tick_index))
        return run_universe.TickOutcome(
            kind="built",
            line=email,
            duration_sec=0.1,
            engine="miner_bot",
            cycle=tick_index,
        )

    monkeypatch.setattr(run_universe, "_tick_outcome", fake_tick)
    monkeypatch.setattr(run_universe, "log_line", lambda _line: None)
    args = argparse.Namespace(parallel=1, delay_sec=0)
    emails = ["bot002@bots.local", "bot001@bots.local"]

    stats = run_universe.run_pass(emails, args, tick_index=55)

    assert calls == [(emails[0], 55), (emails[1], 55)]
    assert stats == {"built": 2, "idle": 0, "skipped": 0}


def test_tick_one_injects_one_leased_client_without_changing_engine_call(monkeypatch):
    sentinel = object()
    leases = []
    engine_calls = []

    class Pool:
        @contextlib.contextmanager
        def lease(self, email, password):
            leases.append((email, password))
            yield sentinel

    def tick(email, **kwargs):
        engine_calls.append((email, kwargs))
        return f"{email} built"

    monkeypatch.setattr(run_universe, "CLIENT_POOL", Pool())
    monkeypatch.setattr(run_universe.miner_bot, "tick", tick)
    monkeypatch.setattr(
        run_universe.db_reader, "has_due_fleet_missions_db", lambda _email: False
    )
    args = argparse.Namespace(password="pw")

    result = run_universe._tick_one("bot001@bots.local", args, tick_index=99)

    assert result == ("built", "bot001@bots.local built")
    assert leases == [("bot001@bots.local", "pw")]
    assert engine_calls == [("bot001@bots.local", {
        "password": "pw", "tick_index": 99, "client": sentinel,
    })]
