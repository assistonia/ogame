"""Exact, content-addressed cache for deterministic battle calculations.

The battle simulator is a pure function when every input, sample count and seed are
fixed.  Raider ticks repeat many identical scenarios, so recomputing them inside the
universe thread pool only burns CPU.  This module keeps a bounded in-memory LRU and
deduplicates concurrent misses without changing any battle formula or decision rule.

Results are deep-copied on both insert and return.  A caller therefore cannot mutate a
cached result that will later be used by another account tick.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from bots.rule_based import battle_sim


T = TypeVar("T")

# Bump this whenever battle_sim's formulas or result schema change.  The cache is only
# process-local today, but keeping the version in the content key makes invalidation an
# explicit contract and permits a persistent backend later without changing callers.
CACHE_ENGINE_VERSION = "battle-sim-v1"
DEFAULT_MAX_ENTRIES = 4096


def _canonical(value: Any) -> Any:
    """Return a JSON-safe representation with deterministic mapping/set ordering."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def content_hash(namespace: str, payload: Mapping[str, Any]) -> str:
    """Hash a complete calculation payload, including the engine-version namespace."""
    canonical = {
        "engine": CACHE_ENGINE_VERSION,
        "namespace": str(namespace),
        "payload": _canonical(payload),
    }
    raw = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def deterministic_sample(
    namespace: str,
    payload: Mapping[str, Any],
    *,
    denominator: int,
) -> bool:
    """Deterministically admit roughly ``1/denominator`` content keys.

    Unlike a shared counter or PRNG this is independent of ThreadPool completion order,
    so the same account/tick/target either always enters the shadow budget or never does.
    """
    denominator = max(1, int(denominator))
    digest = content_hash(f"sample:{namespace}", payload)
    return int(digest[:16], 16) % denominator == 0


class ExactContentCache:
    """Thread-safe bounded LRU with one-owner computation per content hash."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, Any] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._waits = 0
        self._evictions = 0

    def clear(self) -> None:
        """Clear stored values and counters; intended for tests/benchmarks."""
        with self._lock:
            self._entries.clear()
            # Do not strand waiters if a test clears while a calculation is active.
            for event in self._inflight.values():
                event.set()
            self._inflight.clear()
            self._hits = self._misses = self._waits = self._evictions = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "inflight": len(self._inflight),
                "hits": self._hits,
                "misses": self._misses,
                "waits": self._waits,
                "evictions": self._evictions,
            }

    def get_or_compute(
        self,
        namespace: str,
        payload: Mapping[str, Any],
        compute: Callable[[], T],
    ) -> T:
        key = content_hash(namespace, payload)
        while True:
            with self._lock:
                if key in self._entries:
                    self._hits += 1
                    value = self._entries.pop(key)
                    self._entries[key] = value
                    return copy.deepcopy(value)
                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    self._misses += 1
                    owner = True
                else:
                    self._waits += 1
                    owner = False
            if owner:
                break
            event.wait()

        try:
            value = compute()
        except BaseException:
            with self._lock:
                pending = self._inflight.pop(key, None)
                if pending is not None:
                    pending.set()
            raise

        with self._lock:
            stored = copy.deepcopy(value)
            self._entries[key] = stored
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
            pending = self._inflight.pop(key, None)
            if pending is not None:
                pending.set()
        # Preserve the cache-miss call's exact return-object contract for the owner.  The
        # stored copy is already isolated, so later mutation cannot poison cache hits.
        return value


BATTLE_CACHE = ExactContentCache()


def cached_simulate(
    attacker: dict[int, int],
    defender: dict[int, int],
    att_tech: dict | None = None,
    def_tech: dict | None = None,
    att_class: str | None = None,
    def_class: str | None = None,
    rounds: int = battle_sim.MAX_ROUNDS,
    samples: int = battle_sim.DEFAULT_SAMPLES,
    seed: int | None = None,
) -> dict:
    """Cached ``battle_sim.simulate`` with an otherwise identical call contract."""
    if seed is None:
        # random.Random(None) intentionally draws fresh entropy.  Reusing that result
        # would silently turn an unseeded Monte Carlo call deterministic, so only fixed-
        # seed calculations are content-cacheable.
        return battle_sim.simulate(
            attacker,
            defender,
            att_tech=att_tech,
            def_tech=def_tech,
            att_class=att_class,
            def_class=def_class,
            rounds=rounds,
            samples=samples,
            seed=seed,
        )
    payload = {
        # Function identity keeps monkeypatched/hot-reloaded implementations out of old
        # process-local entries; CACHE_ENGINE_VERSION handles normal deployments.
        "implementation": id(battle_sim.simulate),
        "attacker": attacker,
        "defender": defender,
        "att_tech": att_tech,
        "def_tech": def_tech,
        "att_class": att_class,
        "def_class": def_class,
        "rounds": rounds,
        "samples": samples,
        "seed": seed,
    }
    return BATTLE_CACHE.get_or_compute(
        "simulate",
        payload,
        lambda: battle_sim.simulate(
            attacker,
            defender,
            att_tech=att_tech,
            def_tech=def_tech,
            att_class=att_class,
            def_class=def_class,
            rounds=rounds,
            samples=samples,
            seed=seed,
        ),
    )


def cached_decide(
    attacker: dict[int, int],
    defender: dict[int, int],
    loot_estimate: float,
    has_recycler: bool,
    att_tech: dict | None = None,
    def_tech: dict | None = None,
    att_class: str | None = None,
    def_class: str | None = None,
    mode: str = "log",
    win_threshold: float = battle_sim.WIN_THRESHOLD,
    min_ev: float = battle_sim.MIN_EV,
    samples: int = battle_sim.DEFAULT_SAMPLES,
    seed: int | None = None,
) -> dict:
    """Cached ``battle_sim.decide`` preserving samples, seed and decision gates."""
    if seed is None:
        return battle_sim.decide(
            attacker=attacker,
            defender=defender,
            loot_estimate=loot_estimate,
            has_recycler=has_recycler,
            att_tech=att_tech,
            def_tech=def_tech,
            att_class=att_class,
            def_class=def_class,
            mode=mode,
            win_threshold=win_threshold,
            min_ev=min_ev,
            samples=samples,
            seed=seed,
        )
    payload = {
        "implementation": id(battle_sim.decide),
        "attacker": attacker,
        "defender": defender,
        "loot_estimate": loot_estimate,
        "has_recycler": bool(has_recycler),
        "att_tech": att_tech,
        "def_tech": def_tech,
        "att_class": att_class,
        "def_class": def_class,
        "mode": mode,
        "win_threshold": win_threshold,
        "min_ev": min_ev,
        "samples": samples,
        "seed": seed,
    }
    return BATTLE_CACHE.get_or_compute(
        "decide",
        payload,
        lambda: battle_sim.decide(
            attacker=attacker,
            defender=defender,
            loot_estimate=loot_estimate,
            has_recycler=has_recycler,
            att_tech=att_tech,
            def_tech=def_tech,
            att_class=att_class,
            def_class=def_class,
            mode=mode,
            win_threshold=win_threshold,
            min_ev=min_ev,
            samples=samples,
            seed=seed,
        ),
    )
