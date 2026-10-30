"""Deterministic due-time schedule for the universe account loop."""
from __future__ import annotations

import hashlib
import heapq
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountOffset:
    email: str
    offset_sec: float


def stable_spread_offsets(emails: list[str], spread_sec: float) -> list[AccountOffset]:
    """Assign exactly even slots after a stable SHA-256 account shuffle.

    The result is independent of the input order.  Hashing decides which account
    owns each slot; rank/N spacing prevents the random clusters produced by taking
    ``hash % spread`` directly.
    """
    if spread_sec <= 0:
        return [AccountOffset(email, 0.0) for email in emails]
    if len(set(emails)) != len(emails):
        raise ValueError("spread scheduling requires unique account emails")
    ranked = sorted(
        emails,
        key=lambda email: (hashlib.sha256(email.encode("utf-8")).digest(), email),
    )
    width = float(spread_sec) / len(ranked) if ranked else 0.0
    return [
        AccountOffset(email=email, offset_sec=index * width)
        for index, email in enumerate(ranked)
    ]


@dataclass(order=True, frozen=True)
class DueTick:
    due_at: float
    stable_order: int
    email: str = field(compare=False)
    cycle: int = field(compare=False)
    tick_index: int = field(compare=False)


@dataclass(frozen=True)
class DueBatch:
    ready: tuple[DueTick, ...]
    overrun: tuple[DueTick, ...]


class SpreadSchedule:
    """Recurring fixed-epoch schedule with per-account overrun coalescing.

    ``take_due`` advances every popped account immediately to its next fixed epoch
    slot.  If its previous task is still active, that occurrence is returned as an
    overrun instead of being queued for catch-up.
    """

    def __init__(
        self,
        emails: list[str],
        *,
        period_sec: float,
        spread_sec: float,
        now: float,
        base_tick_index: int,
    ) -> None:
        if period_sec <= 0:
            raise ValueError("period_sec must be positive")
        if spread_sec <= 0:
            raise ValueError("spread_sec must be positive")
        if spread_sec > period_sec:
            raise ValueError("spread_sec cannot exceed period_sec")
        self.period_sec = float(period_sec)
        self.spread_sec = float(spread_sec)
        self.account_count = len(emails)
        self.base_cycle = math.floor(float(now) / self.period_sec)
        self.base_tick_index = int(base_tick_index)
        # The current epoch can be partial at process startup.  From the next epoch
        # onward every account has exactly one due occurrence and can form a full
        # operational summary.
        self.first_full_cycle = self.base_cycle + 1
        self._active: set[str] = set()
        self._heap: list[DueTick] = []

        offsets = stable_spread_offsets(emails, spread_sec)
        boundary = self.base_cycle * self.period_sec
        for order, item in enumerate(offsets):
            cycle = self.base_cycle
            due_at = boundary + item.offset_sec
            # Do not create a startup catch-up burst for slots already in the past.
            if due_at < float(now):
                cycle += 1
                due_at += self.period_sec
            heapq.heappush(
                self._heap,
                DueTick(
                    due_at=due_at,
                    stable_order=order,
                    email=item.email,
                    cycle=cycle,
                    tick_index=self.base_tick_index + (cycle - self.base_cycle),
                ),
            )

    @property
    def next_due_at(self) -> float | None:
        return self._heap[0].due_at if self._heap else None

    @property
    def active_emails(self) -> frozenset[str]:
        return frozenset(self._active)

    def take_due(self, now: float) -> DueBatch:
        ready: list[DueTick] = []
        overrun: list[DueTick] = []
        # Small epsilon avoids losing a mathematically exact slot to float division.
        deadline = float(now) + 1e-9
        while self._heap and self._heap[0].due_at <= deadline:
            item = heapq.heappop(self._heap)
            heapq.heappush(
                self._heap,
                DueTick(
                    due_at=item.due_at + self.period_sec,
                    stable_order=item.stable_order,
                    email=item.email,
                    cycle=item.cycle + 1,
                    tick_index=item.tick_index + 1,
                ),
            )
            if item.email in self._active:
                overrun.append(item)
                continue
            self._active.add(item.email)
            ready.append(item)
        return DueBatch(tuple(ready), tuple(overrun))

    def complete(self, email: str) -> None:
        self._active.discard(email)
