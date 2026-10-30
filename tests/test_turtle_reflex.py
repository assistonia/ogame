"""터틀 피격 반사 상태/게이트 회귀 테스트."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import threat, turtle


def test_reflex_flag_defaults_on_after_safety_gate():
    assert turtle.TURTLE_REFLEX_ENABLED is True


def test_first_observation_is_baseline_then_loss_triggers_and_recovery_clears(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(threat, "STATE_DIR", tmp_path / "state")
    email = "turtle@test"

    first = threat.assess_turtle_threat(email, 100_000, now=1_000)
    assert not first.under_reflex and first.lost_value == 0

    hit = threat.assess_turtle_threat(email, 70_000, now=1_010)
    assert hit.under_reflex and hit.lost_value == 30_000
    assert hit.source == "state-peak"

    recovered = threat.assess_turtle_threat(email, 100_000, now=1_020)
    assert not recovered.under_reflex and recovered.lost_value == 0


def test_unrecovered_loss_expires_after_cooldown_and_resets_peak(tmp_path, monkeypatch):
    monkeypatch.setattr(threat, "STATE_DIR", tmp_path / "state")
    email = "turtle@test"
    threat.assess_turtle_threat(email, 100_000, now=1_000)
    assert threat.assess_turtle_threat(email, 50_000, now=1_010).under_reflex

    expired = threat.assess_turtle_threat(
        email, 50_000, now=1_010 + threat.REFLEX_COOLDOWN_SEC + 1
    )
    assert not expired.under_reflex
    assert threat._load(email)["peak_value"] == 50_000


def test_failed_count_read_uses_recent_defender_report_fallback(monkeypatch):
    monkeypatch.setattr(
        threat,
        "_fetch_defender_reports",
        lambda email, since: [{"id": 7, "loss": 12_345, "created_ts": since + 1}],
    )
    state = threat.assess_turtle_threat("turtle@test", None, now=10_000)
    assert state.under_reflex
    assert state.lost_value == 12_345
    assert state.source == "battle-report"


def test_reflex_raises_only_post_growth_budget_and_keeps_deut_reserve(monkeypatch):
    class State:
        under_reflex = True

    monkeypatch.setattr(threat, "assess_turtle_threat", lambda *a, **k: State())
    sm, sc, sd, active = turtle._apply_reflex(
        "turtle@test",
        {turtle.ROCKET_LAUNCHER: 10},
        {"metal": 90_000, "crystal": 40_000, "deuterium": 20_000},
        1_000,
        2_000,
        3_000,
    )
    assert active
    assert (sm, sc, sd) == (90_000, 40_000, 15_000)


def test_gate_off_never_calls_reflex_and_gate_on_does(monkeypatch):
    monkeypatch.setattr(turtle, "read_resources", lambda client: {"metal": 10_000})
    monkeypatch.setattr(turtle, "_surplus", lambda status, levels: (1_000, 0, 0))
    monkeypatch.setattr(
        turtle, "_read_defense_counts", lambda client: {turtle.ROCKET_LAUNCHER: 1}
    )
    monkeypatch.setattr(turtle, "plan_defense", lambda counts, levels: [])

    calls: list[str] = []

    def fake_reflex(email, counts, status, sm, sc, sd):
        calls.append(email)
        return sm, sc, sd, False

    monkeypatch.setattr(turtle, "_apply_reflex", fake_reflex)
    monkeypatch.setattr(turtle, "TURTLE_REFLEX_ENABLED", False)
    turtle._defense_tick_inner(object(), "turtle@test", {21: 1})
    assert calls == []

    monkeypatch.setattr(turtle, "TURTLE_REFLEX_ENABLED", True)
    turtle._defense_tick_inner(object(), "turtle@test", {21: 1})
    assert calls == ["turtle@test"]


def test_energy_deficit_does_not_block_active_reflex_rebuild(monkeypatch):
    counts = {tid: 0 for tid in turtle.DEF_COST}
    monkeypatch.setattr(
        turtle,
        "read_resources",
        lambda client: {"metal": 100_000, "crystal": 20_000, "deuterium": 10_000},
    )
    monkeypatch.setattr(turtle, "_surplus", lambda status, levels: (0, 0, 0))
    monkeypatch.setattr(turtle, "_read_defense_counts", lambda client: counts)
    monkeypatch.setattr(
        turtle,
        "_apply_reflex",
        lambda email, current, status, sm, sc, sd: (100_000, 20_000, 5_000, True),
    )
    builds = []
    monkeypatch.setattr(
        turtle,
        "build_defense",
        lambda client, tid, amount, **_kwargs: (
            builds.append((tid, amount)) or {"status": "success"}
        ),
    )

    note = turtle.defense_tick(
        object(), "turtle@test", {21: 1}, energy=-500,
    )

    assert builds and builds[0][0] == turtle.ROCKET_LAUNCHER
    assert note is not None and note.startswith("turtle REFLEX-DEF")
