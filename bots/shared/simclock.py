"""Game clock for bot code.

In the turn-based simulator the server's clock is virtual (frozen between
turns), so bot-side cooldowns, deadlines and resource estimates must use the
same virtual time. The simulator calls ``set_now()`` before each turn.
Outside the simulator (realtime server) ``now()`` is plain ``time.time()``.
"""
from __future__ import annotations

import os
import time as _time

_sim_now: float | None = None


def set_now(ts: float | None) -> None:
    """Pin the game clock (``None`` returns to wall-clock time)."""
    global _sim_now
    _sim_now = None if ts is None else float(ts)


def now() -> float:
    if _sim_now is not None:
        return _sim_now
    env = os.environ.get("OGAME_SIM_NOW")
    if env:
        return float(env)
    return _time.time()


def utcnow():
    """Timezone-aware UTC ``datetime`` for the game clock."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(now(), _dt.timezone.utc)
