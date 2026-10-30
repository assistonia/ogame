"""Manual cold/warm microbenchmark for the exact raider battle cache.

Run from the repository root:

    python3 tests/bench_battle_cache.py

This does not touch HTTP, MariaDB or game state.
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import battle_cache, raider  # noqa: E402


def main() -> int:
    levels = {
        raider.LIGHT_FIGHTER: 120,
        raider.SMALL_CARGO: 30,
        109: 5,
        110: 5,
        111: 5,
    }
    args = (
        {401: 18, 402: 6},
        {109: 2, 110: 2, 111: 2},
        120_000,
        levels,
    )
    kwargs = {"have_rec": 0, "exposure_percent": 65}

    battle_cache.BATTLE_CACHE.clear()
    started = time.perf_counter()
    cold = raider._autonomous_sized_attack(*args, **kwargs)
    cold_seconds = time.perf_counter() - started
    cold_stats = battle_cache.BATTLE_CACHE.stats()

    repeats = 20
    started = time.perf_counter()
    warm = [raider._autonomous_sized_attack(*args, **kwargs) for _ in range(repeats)]
    warm_seconds = time.perf_counter() - started
    final_stats = battle_cache.BATTLE_CACHE.stats()

    assert cold is not None and all(result == cold for result in warm)
    assert final_stats["misses"] == cold_stats["misses"]
    print(
        "battle-cache "
        f"cold={cold_seconds:.4f}s "
        f"warm_avg={warm_seconds / repeats:.6f}s "
        f"misses={final_stats['misses']} hits={final_stats['hits']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
