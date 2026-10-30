"""금속 sink shadow 통합: HTTP/쓰기 없음, 저빈도, 예약비·전계정 수량 검증."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import metal_sink_observer as obs


def _snapshot(**overrides) -> obs.RuntimeSnapshot:
    data = {
        "resource_state": {
            "metal": 500_000,
            "crystal": 150_000,
            "deuterium": 80_000,
            "energy": 100,
            "metal_max": 500_000,
            "crystal_max": 500_000,
            "deuterium_max": 500_000,
            "temp_max": 50,
        },
        "on_homeworld": True,
        "colony_action_pending": False,
        "building_queue_depth": 0,
        "research_queue_depth": 0,
        "unit_queue_depth": 0,
        "fleet_slots_used": 0,
        "safe_fleetsave_target": True,
        "planet_count": 3,
        "total_large_cargo": 8,
        "total_colony_ships": 0,
        "parked_units": {202: 8, 203: 8, 401: 0, 208: 0},
        "building_queue": (),
        "research_queue": (),
    }
    data.update(overrides)
    return obs.RuntimeSnapshot(**data)


READY_MINER_LEVELS = {
    1: 10,
    2: 10,
    3: 10,
    4: 10,
    14: 4,
    21: 4,
    31: 5,
    113: 3,
    115: 6,
    108: 3,
    106: 2,
    202: 8,
    203: 8,
    401: 0,
}


def test_sampling_is_once_per_interval_and_spread_by_account():
    assert obs.LIVE_EXECUTION_ENABLED is False
    assert set(obs.LIVE_BLOCKERS) == {
        "ai-policy-extra-ship-commitment-unavailable",
        "future-fleetsave-slot-not-reserved",
        "exact-fleetsave-fuel-unavailable",
        "unit-queue-not-atomically-reserved",
    }
    email = "bot011@bots.local"
    hits = [i for i in range(obs.SHADOW_SAMPLE_INTERVAL * 3) if obs.should_sample(email, i)]
    assert len(hits) == 3
    assert hits[1] - hits[0] == obs.SHADOW_SAMPLE_INTERVAL
    assert hits[2] - hits[1] == obs.SHADOW_SAMPLE_INTERVAL

    buckets = {
        next(tick for tick in range(obs.SHADOW_SAMPLE_INTERVAL) if obs.should_sample(
            f"bot{account:03d}@bots.local", tick
        ))
        for account in range(1, 30)
    }
    assert len(buckets) > 1


def test_reserve_includes_waiting_queues_next_growth_ship_and_colony():
    snapshot = _snapshot(
        planet_count=1,
        safe_fleetsave_target=False,
        building_queue_depth=2,
        research_queue_depth=2,
        building_queue=(
            obs.QueueItem(22, 2, False),  # 2,000M, 아직 미차감
            obs.QueueItem(14, 4, True),   # 진행 중 비용은 이미 차감
        ),
        research_queue=(obs.QueueItem(113, 2, False),),  # 1,600C/800D
    )
    reserve = obs.estimate_growth_reserve(
        {**READY_MINER_LEVELS, 22: 1, 113: 1, 202: 0}, "miner", 2, snapshot
    )
    assert reserve is not None
    # 대기 큐와 아직 없는 식민선 비용은 최소한 전부 보호돼야 한다. 다음 성장 3+2건과
    # 소형화물 배치는 여기에 더해지므로 >= 로 검증한다.
    assert reserve["metal"] >= 2_000 + obs.COLONY_SHIP_COST["metal"]
    assert reserve["crystal"] >= 1_600 + obs.COLONY_SHIP_COST["crystal"]
    assert reserve["deuterium"] >= 800 + obs.COLONY_SHIP_COST["deuterium"]


def test_observation_is_explicit_shadow_plan_and_never_has_execution_client(monkeypatch):
    monkeypatch.setattr(obs, "should_sample", lambda *_args: True)
    monkeypatch.setattr(obs, "read_runtime_snapshot", lambda _email: _snapshot())

    note = obs.observe_metal_sink_shadow(
        "bot011@bots.local", dict(READY_MINER_LEVELS), "miner", 9
    )
    assert note is not None
    assert note.startswith("metal-sink SHADOW PLAN defense rocket_launcher(401)x")
    # observer API 자체가 client/build callback을 받지 않아 PLAN은 실행 권한이 될 수 없다.
    assert "client" not in obs.observe_metal_sink_shadow.__annotations__


def test_busy_unit_queue_is_shadow_hold_not_ship_or_defense(monkeypatch):
    monkeypatch.setattr(obs, "should_sample", lambda *_args: True)
    monkeypatch.setattr(
        obs, "read_runtime_snapshot", lambda _email: _snapshot(unit_queue_depth=1)
    )
    note = obs.observe_metal_sink_shadow(
        "bot011@bots.local", dict(READY_MINER_LEVELS), "miner", 9
    )
    assert note is not None
    assert "SHADOW HOLD unit-queue-busy" in note


def test_db_failure_is_silent_and_cannot_break_growth_loop(monkeypatch):
    monkeypatch.setattr(obs, "should_sample", lambda *_args: True)
    monkeypatch.setattr(
        obs,
        "read_runtime_snapshot",
        lambda _email: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert obs.observe_metal_sink_shadow(
        "bot011@bots.local", {}, "miner", 9
    ) is None


def test_runtime_reader_uses_all_planets_flights_and_unfinished_unit_queue(monkeypatch):
    main_sql: list[str] = []
    main_row = {
        "current_planet_id": 10,
        "homeworld_id": 10,
        "planet_count": 2,
        "building_depth": 1,
        "research_depth": 2,
        "unit_depth": 0,
        "fleet_used": 1,
        "colony_pending": 0,
        "total_large_cargo": 13,
        "total_colony_ships": 1,
    }
    for tid in dict.fromkeys((*obs.fleetsave.SAVABLE_SHIPS, 401, 208)):
        main_row[f"parked_{tid}"] = 0
    main_row["parked_203"] = 3

    class Cursor:
        def __init__(self):
            self.call = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params):
            assert params == ("bot011@bots.local",)
            if self.call == 0:
                main_sql.append(sql)
            self.call += 1
        def fetchone(self): return main_row
        def fetchall(self):
            if self.call == 2:
                return [{"object_id": 22, "object_level_target": 2, "building": 0}]
            return [{"object_id": 113, "object_level_target": 2, "building": 1}]
    class Conn:
        def cursor(self): return Cursor()

    monkeypatch.setattr(
        obs.db_reader,
        "get_resource_state_db",
        lambda _email: {
            "metal": 1,
            "crystal": 2,
            "deuterium": 3,
            "energy": 4,
            "metal_max": 10,
            "crystal_max": 10,
            "deuterium_max": 10,
            "temp_max": 20,
        },
    )
    monkeypatch.setattr(obs.db_reader, "_conn", lambda: Conn())

    snapshot = obs.read_runtime_snapshot("bot011@bots.local")
    assert snapshot.total_large_cargo == 13
    assert snapshot.parked_units[203] == 3
    assert snapshot.building_queue == (obs.QueueItem(22, 2, False),)
    assert snapshot.research_queue == (obs.QueueItem(113, 2, True),)
    sql = main_sql[0]
    assert "FROM fleet_missions lf" in sql
    assert "object_amount-lq.object_amount_progress" in sql
    assert "FROM planets ap" in sql
