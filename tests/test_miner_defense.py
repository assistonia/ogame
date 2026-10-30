"""채굴형 최소 방어선(miner_defense) + post_actions 훅 단위검증.

핵심 계약:
  - MINER_BASE_DEFENSE_ENABLED 기본 True → miner/explorer/hybrid에 라이브 적용.
  - 플래그 False kill switch면 공용 post_actions가 훅을 건너뛴다.
  - 최소선 로직: 부족하면 fodder 건조, 충족하면 None, CAP 초과 금지, 적응 시 목표 상향, 무예외.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import miner_defense, post_actions, turtle


# ---------------------------------------------------------------------------
# 플래그 기본값 & post_actions 게이팅 (양쪽 경로 = 공용 run_post_actions)
# ---------------------------------------------------------------------------
def test_flag_defaults_on_after_ready_trend_gate():
    assert miner_defense.MINER_BASE_DEFENSE_ENABLED is True


def _silence_post_actions(monkeypatch):
    """post_actions 의 정체성/세이브/개발/섀도우 훅을 전부 무력화(방어 훅만 관찰)."""
    monkeypatch.setattr(post_actions.colony, "colony_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.explorer, "expedition_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.turtle, "defense_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.raider, "raid_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.metal_sink_observer, "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)


def test_flag_off_is_noop_for_miner(monkeypatch):
    _silence_post_actions(monkeypatch)
    called: list = []
    monkeypatch.setattr(
        post_actions.miner_defense, "base_defense_tick",
        lambda *a, **k: called.append(a) or "SHOULD-NOT-RUN",
    )
    monkeypatch.setattr(
        post_actions.miner_defense, "MINER_BASE_DEFENSE_ENABLED", False
    )
    # 긴급 kill switch OFF: 훅이 아예 호출되지 않아야 한다.
    notes = post_actions.run_post_actions(object(), "bot@test", {}, "miner", 3, energy=1)
    assert called == []
    assert notes == []


def test_flag_on_runs_for_every_non_turtle_personality(monkeypatch):
    _silence_post_actions(monkeypatch)
    seen: list[tuple[str, str]] = []

    def _fake(client, email, levels, pkey, *, energy=None):
        seen.append((email, pkey))
        return f"BASE {pkey}"

    monkeypatch.setattr(post_actions.miner_defense, "base_defense_tick", _fake)
    monkeypatch.setattr(
        post_actions.miner_defense, "MINER_BASE_DEFENSE_ENABLED", True
    )

    got = {}
    for pkey in ("miner", "explorer", "hybrid", "turtle", "raider"):
        notes = post_actions.run_post_actions(
            object(), f"{pkey}@test", {}, pkey, 1, energy=5
        )
        got[pkey] = [n for n in notes if n.startswith("BASE")]

    assert got["miner"] == ["BASE miner"]
    assert got["explorer"] == ["BASE explorer"]
    assert got["hybrid"] == ["BASE hybrid"]
    assert got["turtle"] == []      # 터틀은 별도 정체성(turtle.defense_tick) — 제외
    assert got["raider"] == ["BASE raider"]
    assert {p for _, p in seen} == {"miner", "explorer", "hybrid", "raider"}


# ---------------------------------------------------------------------------
# 최소선 로직 (base_defense_tick)
# ---------------------------------------------------------------------------
_LEVELS_WITH_SHIPYARD = {1: 10, 2: 8, 3: 4, 4: 8, 14: 4, 31: 3, 21: 4}


def _patch_reads(monkeypatch, *, counts, status, under_reflex=False):
    """base_defense_tick 이 부르는 조회 3종을 대체(자원/방어수량/피격상태).

    앵커가 '저장 자원(status)'이라 생산 조회는 더 이상 타지 않는다.
    """
    monkeypatch.setattr(miner_defense.turtle, "_read_defense_counts", lambda *a, **k: counts)
    monkeypatch.setattr(miner_defense.turtle, "read_resources", lambda *a, **k: status)

    class _State:
        def __init__(self, u):
            self.under_reflex = u
            self.lost_value = 0
            self.source = "state-peak"

    monkeypatch.setattr(
        miner_defense.threat, "assess_turtle_threat",
        lambda *a, **k: _State(under_reflex),
    )


def _capture_builds(monkeypatch):
    builds: list[tuple[int, int]] = []
    monkeypatch.setattr(
        miner_defense.turtle, "build_defense",
        lambda client, tid, amount, **_kwargs: (
            builds.append((tid, amount)) or {"status": "success"}
        ),
    )
    return builds


def test_no_shipyard_is_silent_noop(monkeypatch):
    _patch_reads(monkeypatch, counts={401: 0}, status={"metal": 10**9})
    assert miner_defense.base_defense_tick(object(), "e", {21: 0}, "miner") is None


def test_energy_deficit_holds(monkeypatch):
    _patch_reads(monkeypatch, counts={401: 0}, status={"metal": 10**9})
    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=-5
    )
    assert note == "basedef hold (에너지 우선)"


def test_energy_deficit_does_not_block_rebuild_while_under_attack(monkeypatch):
    counts = {tid: 0 for tid in turtle.DEF_COST}
    status = {"metal": 100_000, "crystal": 20_000, "deuterium": 10_000}
    _patch_reads(monkeypatch, counts=counts, status=status, under_reflex=True)
    builds = _capture_builds(monkeypatch)

    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=-5
    )

    assert builds and builds[0][0] == turtle.ROCKET_LAUNCHER
    assert note is not None and note.startswith("basedef ADAPT-DEF")


def test_builds_rocket_launcher_when_below_minimum(monkeypatch):
    # 약탈 가치가 크고(창고 가득) 방어=0 → 최소선 미달 → 로켓런처(401) 건조.
    counts = {tid: 0 for tid in turtle.DEF_COST}
    status = {"metal": 10**9, "crystal": 10**9, "deuterium": 10**9}
    _patch_reads(monkeypatch, counts=counts, status=status)
    builds = _capture_builds(monkeypatch)

    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    )
    assert builds and builds[0][0] == turtle.ROCKET_LAUNCHER
    assert note is not None and note.startswith("basedef BASE-DEF")


def test_returns_none_when_minimum_already_met(monkeypatch):
    # 고정 상한이 아니라 현재 약탈가치 목표를 이미 넘으면 추가 건조하지 않는다.
    counts = {turtle.ROCKET_LAUNCHER: 500}
    status = {"metal": 100_000, "crystal": 100_000, "deuterium": 100_000}
    _patch_reads(monkeypatch, counts=counts, status=status)
    builds = _capture_builds(monkeypatch)

    assert miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    ) is None
    assert builds == []


def test_no_fixed_total_cap_blocks_defense_growth(monkeypatch):
    # 부유해지면 과거 코호트 상한과 무관하게 현재 약탈가치에 맞춰 계속 성장한다.
    counts = {turtle.ROCKET_LAUNCHER: 290}
    status = {"metal": 10**9, "crystal": 10**9, "deuterium": 10**9}
    _patch_reads(monkeypatch, counts=counts, status=status)
    builds = _capture_builds(monkeypatch)

    miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    )
    assert builds == [(turtle.ROCKET_LAUNCHER, turtle.FODDER_BATCH)]


def test_adaptation_raises_target_and_tags_note(monkeypatch):
    # 방어 가치가 baseline 목표는 넘지만 ADAPT(피격) 목표엔 못 미치는 구간: 평상시엔 None,
    # 피격(under_reflex) 시엔 목표가 약탈 가치 전액까지 올라 추가 건조(ADAPT-DEF)해야 한다.
    # 저장 자원 meq=32,000 → 약탈 가치(loot)=16,000. baseline=0.5×loot=8,000,
    # ADAPT=1.0×loot=16,000. def=8,000(로켓런처 4문). 잉여는 별도로 넉넉히 고정(목표 헤드룸만 검증).
    counts = {turtle.ROCKET_LAUNCHER: 4}  # 4*2000 = 8000
    status = {"metal": 32000, "crystal": 0, "deuterium": 0}
    monkeypatch.setattr(miner_defense, "_surplus", lambda *a, **k: (10**9, 10**9, 10**9))

    # 평상시(under_reflex=False): 8000 ≥ baseline 8000 → None.
    _patch_reads(monkeypatch, counts=counts, status=status, under_reflex=False)
    _capture_builds(monkeypatch)
    assert miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    ) is None

    # 피격(under_reflex=True): 목표=16000 > 8000 → 건조, note 는 ADAPT-DEF 태그.
    _patch_reads(monkeypatch, counts=counts, status=status, under_reflex=True)
    builds = _capture_builds(monkeypatch)
    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    )
    assert builds and builds[0][0] == turtle.ROCKET_LAUNCHER
    assert note is not None and note.startswith("basedef ADAPT-DEF")


def test_resource_read_failure_is_silent_and_never_raises(monkeypatch):
    # 자원 조회 실패 → 헛건조 없이 idle. 어떤 조회 실패도 예외로 새지 않는다.
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(miner_defense.turtle, "read_resources", _boom)
    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    )
    assert note == "basedef idle (자원 조회 실패)"


def test_defense_count_failure_does_not_build_blindly(monkeypatch):
    # 방어 수량 조회 실패({}) → def_value=None → idle(0 을 '방어 전무'로 오인 금지).
    _patch_reads(monkeypatch, counts={}, status={"metal": 10**9})
    builds = _capture_builds(monkeypatch)
    note = miner_defense.base_defense_tick(
        object(), "e", _LEVELS_WITH_SHIPYARD, "miner", energy=10
    )
    assert builds == []
    assert note == "basedef idle (방어 조회 실패)"
