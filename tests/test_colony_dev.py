"""식민지 개발 루틴(colony_dev)의 순수 로직 + 순회/복귀 계약 검증 (HTTP/DB 없이).

실제 전환/빌드는 부작용이라 여기서 실행하지 않고, 다음을 고정 입력·가짜로 검증한다:
  1. 라운드로빈 선택(select_colony)이 커서를 따라 모든 식민지를 균등 순회하는가.
  2. 빌드 오더(colony_build_order)가 초기 식민지에서 태양광 우선(에너지 규칙) 저비용
     오더를 내는가.
  3. colony_dev_tick 이 전환→빌드 후 **어떤 경우에도 홈월드로 복귀**하는가
     (정상 경로 + 빌드 중 예외 경로 둘 다).

실행: `python3 -m pytest tests/test_colony_dev.py` 또는 `python3 tests/test_colony_dev.py`.
"""
from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import colony_dev
from bots.rule_based import miner_bot
from bots.shared import db_reader
from bots.shared import planets as planets_mod

SOLAR_PLANT, METAL_MINE, CRYSTAL_MINE = 4, 1, 2

# 홈(현재)·식민지 2개. list_planets_db 반환 형식과 동일.
HOME = {"id": 98, "name": "Homeworld", "galaxy": 1, "system": 41, "position": 8, "is_current": True}
COL_A = {"id": 111, "name": "Colony", "galaxy": 1, "system": 38, "position": 8, "is_current": False}
COL_B = {"id": 113, "name": "Colony", "galaxy": 1, "system": 38, "position": 9, "is_current": False}

# 갓 시드된 식민지 상태: 광산 0, 태양광 0. 창고 기본 10000. energy 0(생산·소비 모두 0).
FRESH_LEVELS: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 14: 0, 21: 0, 31: 0}
FRESH_RS: dict = {
    "metal": 500, "crystal": 500, "deuterium": 0, "energy": 0,
    "metal_max": 10000, "crystal_max": 10000, "deuterium_max": 10000, "temp_max": 40,
}


# ---------------------------------------------------------------------------
# 1) 라운드로빈 선택
# ---------------------------------------------------------------------------
def test_select_colony_round_robin_covers_all():
    """커서 0..N 을 돌리면 모든 식민지가 한 번씩(순서대로) 뽑히고 순환한다."""
    cols = [COL_A, COL_B]
    picked = [colony_dev.select_colony(cols, c)["id"] for c in range(4)]
    assert picked == [111, 113, 111, 113], f"라운드로빈 순환 실패: {picked}"


def test_select_colony_empty_is_none():
    assert colony_dev.select_colony([], 0) is None
    assert colony_dev.select_colony([], 5) is None


def test_advance_cursor_increments_monotonically():
    """_advance_cursor 는 매 호출 현재값 반환 + 파일에 +1 저장(균등 순회 보장)."""
    with _isolated_state():
        seq = [colony_dev._advance_cursor("ai06@bots.local") for _ in range(4)]
        assert seq == [0, 1, 2, 3], f"커서 단조증가 실패: {seq}"
        # 다른 계정은 독립 커서.
        assert colony_dev._advance_cursor("ai07@bots.local") == 0


def _health(
    colony: dict,
    levels: dict,
    resources: dict,
    *,
    last: int = 0,
    now: int = 1,
) -> colony_dev.ColonyHealth:
    return colony_dev.assess_colony_health(
        colony,
        levels,
        resources,
        last_visited_seq=last,
        current_seq=now,
    )


def test_health_selection_prioritizes_energy_throttled_colony():
    """같은 방문 나이면 에너지 적자/생산률 저하 식민지가 정상 식민지보다 먼저다."""
    healthy_levels = {1: 12, 2: 10, 3: 8, 4: 12}
    healthy_rs = {**FRESH_RS, "energy": 250, "production_factor_pct": 100}
    critical_rs = {
        **FRESH_RS,
        "energy": -350,
        "energy_max": 650,
        "energy_used": 1000,
        "production_factor_pct": 65,
    }
    health = {
        111: _health(COL_A, healthy_levels, healthy_rs),
        113: _health(COL_B, healthy_levels, critical_rs),
    }
    picked = colony_dev.select_colony_by_health([COL_A, COL_B], health)
    assert picked and picked["id"] == 113
    assert health[113].critical is True
    assert health[111].critical is False


def test_zero_mine_bootstrap_colony_is_highest_normal_priority():
    """광산 3종이 모두 0인 새 식민지는 일반적인 소규모 적자보다 먼저 부트스트랩한다."""
    developed = {1: 8, 2: 7, 3: 5, 4: 8}
    modest_deficit = {**FRESH_RS, "energy": -100, "production_factor_pct": 90}
    fresh = {**FRESH_RS, "energy": 0, "production_factor_pct": 100}
    health = {
        111: _health(COL_A, developed, modest_deficit),
        113: _health(COL_B, FRESH_LEVELS, fresh),
    }
    picked = colony_dev.select_colony_by_health([COL_A, COL_B], health)
    assert picked and picked["id"] == 113
    assert health[113].zero_mines is True


def test_aging_eventually_visits_healthy_colony_despite_persistent_critical():
    """임계 노후도를 넘긴 정상 식민지는 지속 적자 식민지 때문에 영구 기아하지 않는다."""
    levels = {1: 12, 2: 10, 3: 8, 4: 12}
    critical = {**FRESH_RS, "energy": -2000, "production_factor_pct": 45}
    healthy = {**FRESH_RS, "energy": 200, "production_factor_pct": 100}
    now = 8
    health = {
        # 방금 관리한 심각한 식민지.
        111: _health(COL_A, levels, critical, last=7, now=now),
        # 2개 식민지 starvation threshold(4)를 넘긴 정상 식민지.
        113: _health(COL_B, levels, healthy, last=0, now=now),
    }
    picked = colony_dev.select_colony_by_health([COL_A, COL_B], health)
    assert picked and picked["id"] == 113


def test_account_visit_state_persists_and_rotates_equal_health():
    """동일 건강도면 저장된 마지막 방문 순서가 다음 호출의 선택을 바꾼다."""
    saved_levels = db_reader.get_levels_db
    saved_resources = db_reader.get_resource_state_db
    db_reader.get_levels_db = lambda *_a, **_k: dict(FRESH_LEVELS)
    db_reader.get_resource_state_db = lambda *_a, **_k: dict(FRESH_RS)
    try:
        with _isolated_state():
            first, _ = colony_dev._select_colony_for_account(
                "ai06@bots.local", [dict(COL_A), dict(COL_B)]
            )
            second, _ = colony_dev._select_colony_for_account(
                "ai06@bots.local", [dict(COL_A), dict(COL_B)]
            )
            state = colony_dev._read_visit_state("ai06@bots.local")
    finally:
        db_reader.get_levels_db = saved_levels
        db_reader.get_resource_state_db = saved_resources
    assert first and first["id"] == 111
    assert second and second["id"] == 113
    assert state["visit_seq"] == 2
    assert state["last_visited"] == {"111": 1, "113": 2}


# ---------------------------------------------------------------------------
# 2) 빌드 오더 (초기 커리큘럼 재사용)
# ---------------------------------------------------------------------------
def test_build_order_solar_first_on_fresh_colony():
    """갓 시드된 식민지: 광산 업글이 적자를 부르므로 태양광(4)이 첫 후보로 온다(선제)."""
    order = colony_dev.colony_build_order(
        "miner", dict(FRESH_LEVELS), tick_index=0,
        energy=FRESH_RS["energy"], resource_state=dict(FRESH_RS), email="ai06@bots.local",
    )
    assert order, "빌드 후보가 비었다"
    assert order[0] == ("build", SOLAR_PLANT), f"태양광 우선이 아님: {order[:3]}"
    # 저비용 광산이 뒤따른다(메탈/크리스탈 광산 포함).
    ids = [a[1] for a in order if a[0] == "build"]
    assert METAL_MINE in ids and CRYSTAL_MINE in ids, f"광산이 오더에 없음: {ids}"


def test_build_order_solar_first_when_energy_negative():
    """이미 에너지 적자면(반응) 성격 무관 태양광이 최우선."""
    lv = {1: 3, 2: 2, 3: 0, 4: 1, 14: 0, 21: 0, 31: 0}
    rs = {**FRESH_RS, "energy": -50}
    order = colony_dev.colony_build_order(
        "hybrid", lv, tick_index=1, energy=-50, resource_state=rs, email="x@bots.local",
    )
    assert order[0] == ("build", SOLAR_PLANT), f"적자인데 태양광 우선 아님: {order[:3]}"


# 조선소가 생긴 뒤 고레벨 식민지: 태양광 발전소 다음 레벨이 자원 100만+이라 위성 경로로 전환.
SHIPYARD, SOLAR_SATELLITE = 21, 212
COL_SAT = {"id": 115, "name": "Colony", "galaxy": 1, "system": 38, "position": 10, "is_current": False}
SAT_LEVELS: dict[int, int] = {1: 24, 2: 21, 3: 21, 4: 23, 14: 4, 21: 4, 31: 5}
SAT_RS: dict = {
    "metal": 90000, "crystal": 90000, "deuterium": 300000, "energy": -2000,
    "metal_max": 5_000_000, "crystal_max": 5_000_000, "deuterium_max": 5_000_000, "temp_max": 58,
}


def test_shipyard_flips_energy_from_solar_plant_to_satellite_path():
    """shipyard 유무로 에너지 개입이 갈린다: 조선소 없으면 적자 때 태양광 발전소를 **선두로
    강제**(비싼 고레벨이라 못 사고 적자 방치)하고, 조선소가 있으면 선두 강제를 풀어 위성
    경로에 맡긴다(태양광 발전소는 base 저순위로만 남고 선두가 아님)."""
    # 조선소 없음: 에너지 개입이 태양광 발전소를 최우선으로 끌어온다.
    no_yard = dict(SAT_LEVELS)
    no_yard[SHIPYARD] = 0
    order_no = colony_dev.colony_build_order(
        "miner", no_yard, tick_index=0,
        energy=SAT_RS["energy"], resource_state=dict(SAT_RS), email="ai07@bots.local",
    )
    assert order_no[0] == ("build", SOLAR_PLANT), f"조선소 없으면 태양광 발전소 선두여야: {order_no[:3]}"
    # 조선소 있음: 태양광 발전소를 선두로 강제하지 않는다(위성 경로가 에너지를 맡음).
    order_yes = colony_dev.colony_build_order(
        "miner", dict(SAT_LEVELS), tick_index=0,
        energy=SAT_RS["energy"], resource_state=dict(SAT_RS), email="ai07@bots.local",
    )
    assert order_yes[0] != ("build", SOLAR_PLANT), f"조선소 있으면 태양광 선두 강제가 아니어야: {order_yes[:3]}"


def test_colony_with_shipyard_builds_solar_satellite_for_energy():
    """조선소 있는 식민지가 에너지 적자면, 홈월드처럼 조선소 큐로 태양광 위성을 건조한다."""
    switch_log: list[int] = []
    ship_log: list[tuple] = []

    def _queue_none(client, candidates, max_items, **kw):
        return [], ""  # primary 는 이번 틱 아무것도 못 지음(고레벨 광산 자원부족 가정)

    def _fake_try_first(client, candidates):
        if candidates:
            ship_log.append(candidates[0])
            return candidates[0], ""
        return None, ""

    with _isolated_state(), _patched(switch_log, queue_impl=_queue_none):
        db_reader.list_planets_db = lambda email: [dict(HOME), dict(COL_SAT)]
        db_reader.get_levels_db = lambda email, *a, **k: dict(SAT_LEVELS)
        db_reader.get_resource_state_db = lambda email, *a, **k: dict(SAT_RS)
        saved_try = miner_bot._try_first
        miner_bot._try_first = _fake_try_first
        try:
            note = colony_dev.colony_dev_tick(
                FakeClient("ai07@bots.local"), "ai07@bots.local", "miner", 0
            )
        finally:
            miner_bot._try_first = saved_try

    assert ship_log, f"에너지 적자인데 위성 건조를 시도하지 않음: note={note!r}"
    assert ship_log[0][0] == "ship" and ship_log[0][1] == SOLAR_SATELLITE, f"위성이 아님: {ship_log}"
    assert ship_log[0][2] == 22, f"실제 부족량/위험예산 기반 배치가 아님: {ship_log}"
    assert note and "colonydev" in note, f"빌드 로그 없음: {note!r}"
    assert switch_log[0] == 115 and switch_log[-1] == 98, f"전환/복귀 계약 위반: {switch_log}"


def test_adaptive_satellite_batch_uses_actual_deficit_not_fixed_eight():
    """-2000, 위성당 33이면 61기가 필요하지만 자원 위험예산 50%가 22기로 안전 제한한다."""
    batch = colony_dev.adaptive_satellite_batch(
        SAT_RS,
        deficit=2000,
        risk_fraction=0.50,
    )
    assert batch == 22
    assert batch != 8


def test_adaptive_satellite_batch_applies_affordability_queue_and_hard_caps():
    resources = {
        **SAT_RS,
        "crystal": 20_000,
        "deuterium": 2_500,  # 위성 5기분
    }
    assert colony_dev.adaptive_satellite_batch(
        resources,
        deficit=10_000,
        risk_fraction=1.0,
    ) == 5
    assert colony_dev.adaptive_satellite_batch(
        SAT_RS,
        deficit=10_000,
        risk_fraction=1.0,
        queue_capacity=4,
    ) == 4
    assert colony_dev.adaptive_satellite_batch(
        {**SAT_RS, "crystal": 2_000_000, "deuterium": 2_000_000},
        deficit=100_000,
        risk_fraction=1.0,
    ) == colony_dev.SATELLITE_RECOVERY_MAX_BATCH


def test_adaptive_satellite_batch_obeys_resource_risk_budget():
    """파괴 가능한 위성에 유동자원을 전부 쓰지 않고 지정 비율만 노출한다."""
    resources = {
        **SAT_RS,
        "crystal": 40_000,
        "deuterium": 10_000,  # affordability 20기
    }
    assert colony_dev.adaptive_satellite_batch(
        resources,
        deficit=10_000,
        risk_fraction=0.25,
    ) == 5
    assert colony_dev.adaptive_satellite_batch(
        resources,
        deficit=10_000,
        risk_fraction=0.0,
    ) == 0


def test_policy_zero_fleet_risk_disables_destructible_satellite_budget():
    assert colony_dev._satellite_risk_fraction(
        {"limits": {"fleet_risk_percent": 0}}
    ) == 0


def test_new_unit_queue_items_detects_only_new_authoritative_rows():
    existing = SimpleNamespace(
        object_id=212,
        requested_amount=8,
        time_start=100,
        time_end=110,
    )
    defense = SimpleNamespace(
        object_id=401,
        requested_amount=20,
        time_start=110,
        time_end=130,
    )
    before = SimpleNamespace(
        available=True,
        planet_id=42,
        unit_items=(existing,),
    )
    after = SimpleNamespace(
        available=True,
        planet_id=42,
        unit_items=(existing, defense),
    )

    assert colony_dev._new_unit_queue_items(before, after) == [defense]
    assert colony_dev._new_unit_queue_items(
        before, SimpleNamespace(available=False, planet_id=42, unit_items=())
    ) is None


# ---------------------------------------------------------------------------
# 3) 순회/복귀 계약 — 전환→빌드→홈 복귀 (정상 + 예외)
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self, email: str) -> None:
        self.email = email


@contextlib.contextmanager
def _isolated_state():
    """커서 상태파일을 임시 디렉토리로 격리(레포 오염 방지)."""
    saved = colony_dev.STATE_DIR
    colony_dev.STATE_DIR = pathlib.Path(tempfile.mkdtemp()) / "cursor"
    try:
        yield
    finally:
        colony_dev.STATE_DIR = saved


@contextlib.contextmanager
def _patched(switch_log: list[int], *, queue_impl):
    """db_reader/planets_mod/_queue_actions 를 가짜로. switch_planet 호출 pid 를 기록."""
    saved = {}

    def _fake_switch(client, planet_id):
        switch_log.append(int(planet_id))
        return True

    patches = {
        (db_reader, "list_planets_db"): lambda email: [dict(HOME), dict(COL_A), dict(COL_B)],
        (db_reader, "get_levels_db"): lambda email, *a, **k: dict(FRESH_LEVELS),
        (db_reader, "get_resource_state_db"): lambda email, *a, **k: dict(FRESH_RS),
        (db_reader, "active_build_targets"): lambda email, *a, **k: set(),
        (planets_mod, "switch_planet"): _fake_switch,
        (miner_bot, "_queue_actions"): queue_impl,
    }
    try:
        for (mod, name), fn in patches.items():
            saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, fn)
        yield
    finally:
        for (mod, name), fn in saved.items():
            setattr(mod, name, fn)


def test_tick_switches_to_colony_and_returns_home_on_success():
    """정상 경로: 홈이 아닌 식민지로 전환→빌드→마지막에 홈(98)으로 복귀."""
    switch_log: list[int] = []

    def _queue_ok(client, candidates, max_items, **kw):
        return [("build", SOLAR_PLANT)], ""

    with _isolated_state(), _patched(switch_log, queue_impl=_queue_ok):
        note = colony_dev.colony_dev_tick(FakeClient("ai06@bots.local"), "ai06@bots.local", "miner", 0)

    assert note and "colonydev" in note, f"빌드 로그가 없음: {note!r}"
    assert switch_log[0] == 111, f"식민지로 먼저 전환해야: {switch_log}"
    assert switch_log[-1] == 98, f"마지막은 홈 복귀여야: {switch_log}"


def test_structured_tick_exposes_machine_readable_build_target():
    switch_log: list[int] = []

    def _queue_ok(client, candidates, max_items, **kw):
        return [("build", SOLAR_PLANT)], ""

    with _isolated_state(), _patched(switch_log, queue_impl=_queue_ok):
        outcome = colony_dev.colony_dev_tick(
            FakeClient("ai06@bots.local"),
            "ai06@bots.local",
            "miner",
            0,
            structured=True,
        )

    assert isinstance(outcome, dict)
    assert outcome["queued_actions"] == [{
        "object_type": "building",
        "object_id": SOLAR_PLANT,
        "planet_id": 111,
        "target_level": 1,
    }]
    assert outcome["untracked_action_count"] == 0


def test_tick_returns_home_even_when_build_raises():
    """빌드 중 예외가 나도 finally 로 홈(98)에 복귀하고, tick 은 예외를 삼켜 로그만 낸다."""
    switch_log: list[int] = []

    def _queue_boom(client, candidates, max_items, **kw):
        raise RuntimeError("boom in queue")

    with _isolated_state(), _patched(switch_log, queue_impl=_queue_boom):
        note = colony_dev.colony_dev_tick(FakeClient("ai06@bots.local"), "ai06@bots.local", "miner", 0)

    assert note is not None and "err" in note, f"예외가 로그로 잡히지 않음: {note!r}"
    assert switch_log[-1] == 98, f"예외에도 홈 복귀 보장 실패: {switch_log}"


def test_tick_no_colonies_is_noop():
    """식민지가 없으면(홈만) 전환 없이 즉시 None — 부하 없음."""
    switch_log: list[int] = []

    def _queue_ok(client, candidates, max_items, **kw):
        return [("build", SOLAR_PLANT)], ""

    with _isolated_state(), _patched(switch_log, queue_impl=_queue_ok):
        # list_planets_db 를 홈 단독으로 덮어쓴다.
        db_reader.list_planets_db = lambda email: [dict(HOME)]
        note = colony_dev.colony_dev_tick(FakeClient("ai06@bots.local"), "ai06@bots.local", "miner", 0)

    assert note is None, f"식민지 없는데 행동함: {note!r}"
    assert switch_log == [], f"식민지 없는데 전환함: {switch_log}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
