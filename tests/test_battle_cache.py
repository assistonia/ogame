"""Exact battle-cache golden, invalidation and concurrent-dedupe contracts."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bots.rule_based import battle_cache
from bots.rule_based import battle_sim
from bots.rule_based import raider


LF = raider.LIGHT_FIGHTER
SC = raider.SMALL_CARGO
RL = 401


def test_cached_simulate_miss_is_bit_exact_to_uncached_golden():
    battle_cache.BATTLE_CACHE.clear()
    kwargs = {
        "attacker": {LF: 18, SC: 4},
        "defender": {RL: 12},
        "att_tech": {109: 3, 110: 2, 111: 4},
        "def_tech": {109: 1, 110: 1, 111: 2},
        "samples": 24,
        "seed": 1234,
    }

    golden = battle_sim.simulate(**kwargs)
    cached = battle_cache.cached_simulate(**kwargs)

    assert cached == golden
    assert battle_cache.BATTLE_CACHE.stats()["misses"] == 1


def test_autonomous_sizing_cache_miss_matches_exhaustive_v1_golden():
    """The cache wrapper must not alter the 16×24 fleet choice or decision."""
    levels = {LF: 80, SC: 20, 109: 4, 110: 4, 111: 4}
    defender = {RL: 10}
    research = {109: 1, 110: 1, 111: 1}
    loot = 80_000
    cap = raider._sizing_max_combat_budget(
        levels[LF], 0, levels, max_combat_percent=65
    )
    choices = []
    for step in range(1, raider.SIZING_SCAN_STEPS + 1):
        fleet = raider._sizing_fleet(
            max(1, cap * step // raider.SIZING_SCAN_STEPS),
            loot,
            levels[LF],
            0,
            levels[SC],
            combat_inventory=levels,
            max_combat_percent=65,
        )
        if not fleet:
            continue
        realized = min(loot, raider._sizing_cargo_capacity(fleet))
        decision = battle_sim.decide(
            attacker=fleet,
            defender=defender,
            loot_estimate=realized,
            has_recycler=False,
            att_tech=levels,
            def_tech=research,
            mode="log",
            samples=raider.SIZING_BAND_SAMPLES,
            seed=raider.SIZING_SEED,
        )
        if decision.get("recommend") is True:
            choices.append((
                int(battle_sim.fleet_cost_value(fleet)),
                -float(decision.get("ev", 0.0)),
                fleet,
                decision,
            ))
    assert choices
    _value, _neg_ev, golden_fleet, golden_decision = min(
        choices, key=lambda item: (item[0], item[1])
    )

    battle_cache.BATTLE_CACHE.clear()
    actual = raider._autonomous_sized_attack(
        defender,
        research,
        loot,
        levels,
        have_rec=0,
        exposure_percent=65,
    )

    assert actual == (golden_fleet, golden_decision)


def test_exact_hit_dedupes_and_returns_isolated_values(monkeypatch):
    battle_cache.BATTLE_CACHE.clear()
    calls = []

    def fake_decide(**kwargs):
        calls.append(kwargs)
        return {"recommend": True, "ev": kwargs["loot_estimate"], "sim": {}}

    monkeypatch.setattr(battle_sim, "decide", fake_decide)
    kwargs = {
        "attacker": {LF: 4, SC: 2},
        "defender": {RL: 1},
        "loot_estimate": 10_000,
        "has_recycler": False,
        "att_tech": {109: 2},
        "def_tech": {109: 1},
        "samples": 24,
        "seed": 1234,
    }
    first = battle_cache.cached_decide(**kwargs)
    first["ev"] = -999
    second = battle_cache.cached_decide(**kwargs)

    assert len(calls) == 1
    assert second["ev"] == 10_000
    assert battle_cache.BATTLE_CACHE.stats()["hits"] == 1


def test_changed_combat_content_samples_or_seed_is_a_cache_miss(monkeypatch):
    battle_cache.BATTLE_CACHE.clear()
    calls = []

    def fake_decide(**kwargs):
        calls.append(kwargs)
        return {"recommend": True, "ev": 1, "sim": {}}

    monkeypatch.setattr(battle_sim, "decide", fake_decide)
    base = {
        "attacker": {LF: 4},
        "defender": {RL: 1},
        "loot_estimate": 10_000,
        "has_recycler": False,
        "att_tech": {109: 2},
        "def_tech": {109: 1},
        "samples": 24,
        "seed": 1234,
    }
    battle_cache.cached_decide(**base)
    battle_cache.cached_decide(**{**base, "defender": {RL: 2}})
    battle_cache.cached_decide(**{**base, "def_tech": {109: 2}})
    battle_cache.cached_decide(**{**base, "samples": 25})
    battle_cache.cached_decide(**{**base, "seed": 1235})
    battle_cache.cached_decide(**base)

    assert len(calls) == 5
    assert battle_cache.BATTLE_CACHE.stats() == {
        "entries": 5,
        "inflight": 0,
        "hits": 1,
        "misses": 5,
        "waits": 0,
        "evictions": 0,
    }


def test_unseeded_monte_carlo_is_never_frozen_in_cache(monkeypatch):
    battle_cache.BATTLE_CACHE.clear()
    calls = []

    def fake_decide(**_kwargs):
        calls.append(len(calls) + 1)
        return {"recommend": True, "ev": calls[-1], "sim": {}}

    monkeypatch.setattr(battle_sim, "decide", fake_decide)
    kwargs = {
        "attacker": {LF: 4},
        "defender": {RL: 1},
        "loot_estimate": 10_000,
        "has_recycler": False,
        "seed": None,
    }

    assert battle_cache.cached_decide(**kwargs)["ev"] == 1
    assert battle_cache.cached_decide(**kwargs)["ev"] == 2
    assert battle_cache.BATTLE_CACHE.stats()["entries"] == 0


def test_concurrent_identical_misses_compute_once():
    cache = battle_cache.ExactContentCache(max_entries=8)
    calls = 0
    lock = threading.Lock()

    def compute():
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.03)
        return {"value": 7}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(cache.get_or_compute, "same", {"fleet": {LF: 4}}, compute)
            for _ in range(8)
        ]
        results = [future.result() for future in futures]

    assert calls == 1
    assert results == [{"value": 7}] * 8
    stats = cache.stats()
    assert stats["misses"] == 1
    assert stats["waits"] >= 1
    assert stats["hits"] == 7


def test_deterministic_shadow_budget_is_order_independent():
    payload_a = {"email": "a", "bucket": 10, "coord": (1, 2, 3)}
    payload_b = {"coord": (1, 2, 3), "bucket": 10, "email": "a"}
    assert battle_cache.content_hash("x", payload_a) == battle_cache.content_hash(
        "x", payload_b
    )
    assert battle_cache.deterministic_sample(
        "shadow", payload_a, denominator=64
    ) == battle_cache.deterministic_sample("shadow", payload_b, denominator=64)
