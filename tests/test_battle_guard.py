"""battle_guard의 지연 예측-실전 매칭과 live 안전핀 배선 검증."""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import battle_guard as guard
from bots.rule_based import battle_sim


def _decision(loss: float = 1000.0) -> dict:
    return {
        "sim": {"attacker_loss_value": loss, "win_prob": 0.9},
        "ev": 5000.0,
        "recommend": True,
    }


def _report(report_id: int, loss: int, *, created: int = 120) -> dict:
    return {
        "id": report_id,
        "g": 1,
        "s": 2,
        "p": 3,
        "created_ts": created,
        "attacker": json.dumps({"units": {"light_fighter": 4}, "resource_loss": loss}),
    }


def test_log_prediction_reconciles_without_touching_safety(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "guard")
    monkeypatch.setattr(battle_sim, "STATE_DIR", tmp_path / "sim")
    monkeypatch.setattr(guard, "_fetch_reports", lambda email, since: [_report(7, 3000)])
    guard.record_prediction(
        "bot@test", (1, 2, 3), {204: 4}, _decision(2000), mode="log", sent_at=100
    )

    out = guard.reconcile("bot@test", now=200)
    assert len(out) == 1
    assert out[0]["actual_loss"] == 3000
    assert "safety" not in out[0]
    assert not battle_sim.is_tripped("bot@test")
    assert guard.recent_observations("bot@test")[0]["report_id"] == 7


def test_cohort_and_exact_match_inputs_survive_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "guard")
    monkeypatch.setattr(guard, "_fetch_reports", lambda email, since: [_report(9, 0)])
    guard.record_prediction(
        "bot@test", (1, 2, 3), {204: 4}, _decision(0),
        mode="log", sent_at=100, cohort="preflight_v1",
    )

    [outcome] = guard.reconcile("bot@test", now=200)

    assert outcome["cohort"] == "preflight_v1"
    assert outcome["email"] == "bot@test"
    assert outcome["sent_at"] == 100
    assert outcome["target"] == [1, 2, 3]
    assert outcome["attacker"] == {"204": 4}


def test_wrong_coordinate_or_units_do_not_match(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "guard")
    bad = _report(8, 3000)
    bad["p"] = 9
    monkeypatch.setattr(guard, "_fetch_reports", lambda email, since: [bad])
    guard.record_prediction(
        "bot@test", (1, 2, 3), {204: 4}, _decision(), mode="log", sent_at=100
    )
    assert guard.reconcile("bot@test", now=200) == []


def test_live_three_overruns_trip_and_emit_once(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "guard")
    monkeypatch.setattr(battle_sim, "STATE_DIR", tmp_path / "sim")
    trips: list[tuple] = []
    monkeypatch.setattr(guard, "_record_trip_event", lambda *args: trips.append(args))

    for idx in range(3):
        rid = 20 + idx
        monkeypatch.setattr(
            guard, "_fetch_reports", lambda email, since, rid=rid: [_report(rid, 25001)]
        )
        guard.record_prediction(
            "bot@test", (1, 2, 3), {204: 4}, _decision(10000),
            mode="live", sent_at=100 + idx,
        )
        out = guard.reconcile("bot@test", now=200 + idx)
        assert len(out) == 1

    assert battle_sim.is_tripped("bot@test")
    assert len(trips) == 1


def test_expired_prediction_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path / "guard")
    monkeypatch.setattr(guard, "_fetch_reports", lambda email, since: [])
    guard.record_prediction(
        "bot@test", (1, 2, 3), {204: 4}, _decision(), mode="log", sent_at=1
    )
    assert guard.reconcile("bot@test", now=guard.PENDING_TTL_SEC + 2) == []
    assert guard._load("bot@test")["pending"] == []
