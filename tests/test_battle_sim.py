"""전투 시뮬레이터(battle_sim) 검증.

서버 전투 공식 복제의 정확성(스탯 손계산 대조), 상식적 승패(약체 방어 격파 / 압도적
방어에 자살), 대칭성, 재현성을 확인한다. HTTP/DB 없이 순수 함수만 호출한다.

실행: `python3 -m pytest tests/test_battle_sim.py` 또는 `python3 tests/test_battle_sim.py`.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import battle_sim as bs
from scripts import backtest_battle_sim as backtest

LIGHT_FIGHTER, HEAVY_FIGHTER, SMALL_CARGO = 204, 205, 202
ROCKET_LAUNCHER, LIGHT_LASER = 401, 402
WEAPON, SHIELD, ARMOR = 109, 110, 111


# (d) 서버 스탯 손계산 대조 — 경전투기(204): structure 4000, shield 10, attack 50.
def test_combat_stats_matches_server():
    base = bs.combat_stats(LIGHT_FIGHTER)
    assert base == {"attack": 50, "shield": 10, "hull": 400, "structure": 4000}, base
    # 연구 보정(정수절삭): 무기10 → 공격 100, 실드6 → 실드 16, 장갑5 → 선체 600.
    boosted = bs.combat_stats(LIGHT_FIGHTER, weapon=10, shield=6, armor=5)
    assert boosted["attack"] == 100, boosted
    assert boosted["shield"] == 16, boosted
    assert boosted["hull"] == 600 and boosted["structure"] == 6000, boosted


# (a) 전투기 10 vs 로켓런처 5 → 승률 높고 손실 소액.
def test_fighters_beat_weak_defense():
    r = bs.simulate({LIGHT_FIGHTER: 10}, {ROCKET_LAUNCHER: 5}, samples=60, seed=1)
    assert r["win_prob"] > 0.6, r
    # 손실이 공격 함대 전체 가치(10×4000=40000)의 절반 미만이면 "소액"으로 본다.
    assert r["attacker_loss_value"] < 20000, r


# (b) 전투기 4 vs 로켓런처 100 → 승률 ~0 (겁쟁이 게이트가 옳았던 케이스).
def test_fighters_suicide_into_wall():
    r = bs.simulate({LIGHT_FIGHTER: 4}, {ROCKET_LAUNCHER: 100}, samples=40, seed=2)
    assert r["win_prob"] < 0.05, r
    assert r["defender_win_prob"] > 0.9, r


# (c) 대칭 전력 → 공격/방어 승률이 대칭(둘 다 비슷).
def test_symmetric_is_balanced():
    r = bs.simulate({LIGHT_FIGHTER: 20}, {LIGHT_FIGHTER: 20},
                    att_tech={WEAPON: 3, SHIELD: 3, ARMOR: 3},
                    def_tech={WEAPON: 3, SHIELD: 3, ARMOR: 3},
                    samples=80, seed=3)
    assert abs(r["win_prob"] - r["defender_win_prob"]) < 0.25, r


# (e) 재현성 — 같은 시드면 결과가 완전히 동일.
def test_reproducible_with_seed():
    kw = dict(samples=30, seed=42)
    r1 = bs.simulate({LIGHT_FIGHTER: 12, SMALL_CARGO: 3}, {ROCKET_LAUNCHER: 8, LIGHT_LASER: 4}, **kw)
    r2 = bs.simulate({LIGHT_FIGHTER: 12, SMALL_CARGO: 3}, {ROCKET_LAUNCHER: 8, LIGHT_LASER: 4}, **kw)
    assert r1 == r2, (r1, r2)
    # 다른 시드는 (거의 확실히) 다른 표본 → 승률이 완전히 같을 필요 없음(분포만 동일).
    r3 = bs.simulate({LIGHT_FIGHTER: 12, SMALL_CARGO: 3}, {ROCKET_LAUNCHER: 8, LIGHT_LASER: 4},
                     samples=30, seed=99)
    assert r3["samples"] == 30


def test_general_report_bonus_is_not_applied_to_actual_rust_stats():
    """General +2는 보고서 표시용이며 simulate의 raw tech에 더하지 않는다."""
    raw = {WEAPON: 3, SHIELD: 2, ARMOR: 1}
    kwargs = dict(
        attacker={LIGHT_FIGHTER: 16, SMALL_CARGO: 2},
        defender={ROCKET_LAUNCHER: 12, LIGHT_LASER: 4},
        att_tech=raw,
        def_tech=raw,
        samples=40,
        seed=12345,
    )
    no_class = bs.simulate(**kwargs)
    general = bs.simulate(**kwargs, att_class="General", def_class="General")
    assert general == no_class

    # BattleEngine 보고서에는 +2가 저장되므로 백테스트 경로는 반드시 빼야 한다.
    report_side = {
        "character_class": "General",
        "weapon_technology": 5,
        "shielding_technology": 4,
        "armor_technology": 3,
    }
    assert backtest._engine_tech(report_side) == raw
    # 표시값이 2보다 작은 깨진/오래된 보고서도 음수 연구로 만들지 않는다.
    assert backtest._engine_tech({"character_class": "General"}) == {
        WEAPON: 0,
        SHIELD: 0,
        ARMOR: 0,
    }


def test_attack_ev_signs():
    """EV: 약탈은 승률 가중, 리사이클러 없으면 데브리 할인, 손실 차감."""
    sim = {"win_prob": 1.0, "attacker_loss_value": 5000.0, "expected_debris": (10000.0, 5000.0)}
    ev_rec = bs.attack_ev(20000, sim, has_recycler=True)
    ev_norec = bs.attack_ev(20000, sim, has_recycler=False)
    # 리사이클러 보유: 20000 + 15000 - 5000 = 30000
    assert abs(ev_rec - 30000.0) < 1e-6, ev_rec
    # 미보유: 데브리 15000×0.3=4500 → 20000 + 4500 - 5000 = 19500
    assert abs(ev_norec - 19500.0) < 1e-6, ev_norec
    # 승률 0이면 약탈은 0 가중 → EV = 데브리 - 손실 (음수 가능)
    sim_lose = {"win_prob": 0.0, "attacker_loss_value": 40000.0, "expected_debris": (0.0, 0.0)}
    assert bs.attack_ev(20000, sim_lose, has_recycler=True) < 0


def test_defense_repair_reduces_defender_loss():
    """방어시설 손실은 복구율(70%) 반영 → 격파분의 30%만 영구손실로 집계."""
    # 공격측을 압도적으로 줘 방어를 확실히 격파시키고 방어측 손실가치를 본다.
    r = bs.simulate({LIGHT_FIGHTER: 200}, {ROCKET_LAUNCHER: 10}, samples=20, seed=7)
    assert r["win_prob"] > 0.95, r
    # 로켓런처 10기 전액가치 = 10×2000=20000. 복구 반영 시 30%인 6000 근처(전멸 기준).
    assert r["defender_loss_value"] <= 20000 * 0.30 + 1, r


def test_performance_under_100ms():
    """타겟당 1회 호출 예산(100ms) — 중간 규모 전투 20표본."""
    t0 = time.perf_counter()
    bs.simulate({LIGHT_FIGHTER: 30, HEAVY_FIGHTER: 10, SMALL_CARGO: 10},
                {ROCKET_LAUNCHER: 40, LIGHT_LASER: 20}, samples=20, seed=5)
    dt = time.perf_counter() - t0
    assert dt < 0.10, f"too slow: {dt*1000:.1f}ms"


def test_backtest_filters_unreconstructable_reports_and_computes_gate_metrics():
    """클래스 메타 누락/특수전은 빼고, 유효 표본 합계 bias로 게이트를 판정한다."""

    def side(
        units: dict[str, int],
        loss: object = 0,
        character_class: object = None,
        include_class: bool = True,
    ) -> dict:
        value = {
            "units": units,
            "resource_loss": loss,
            "weapon_technology": 5,
            "shielding_technology": 4,
            "armor_technology": 3,
        }
        if include_class:
            value["character_class"] = character_class
        return value

    def row(
        report_id: int,
        attacker: dict,
        defender: dict,
        general: dict | None = None,
        rounds: list | None = None,
    ) -> dict:
        if rounds is None:
            rounds = [{"attacker_ships": {"light_fighter": 1}, "defender_ships": {}}]
        return {
            "id": report_id,
            "general": json.dumps(general or {}),
            "attacker": json.dumps(attacker),
            "defender": json.dumps(defender),
            "rounds": json.dumps(rounds),
        }

    valid_one = row(
        1,
        side({"light_fighter": 1}, loss=100, character_class="General"),
        side({"rocket_launcher": 1}),
    )
    valid_two = row(
        2,
        side({"light_fighter": 2}, loss=200, character_class="General"),
        side({"rocket_launcher": 2}),
    )
    missing_class = row(
        3,
        side({"light_fighter": 1}, include_class=False),
        side({"rocket_launcher": 1}, include_class=False),
    )
    expedition = row(
        4,
        side({"light_fighter": 1}),
        side({"rocket_launcher": 1}),
        general={"expedition_battle": True},
    )
    hamill = row(
        5,
        side({"light_fighter": 1}, character_class="General"),
        side({"deathstar": 1}),
        general={"hamill_manoeuvre_triggered": True},
    )
    empty = row(6, side({"light_fighter": 1}), side({}))
    unknown = row(7, side({"future_ship": 1}), side({"rocket_launcher": 1}))
    invalid_rounds = row(
        8,
        side({"light_fighter": 1}),
        side({"rocket_launcher": 1}),
        rounds=[],
    )

    seen_tech: list[dict[int, int]] = []

    def fake_simulate(attacker: dict[int, int], _defender: dict[int, int], **kwargs) -> dict:
        seen_tech.append(kwargs["att_tech"])
        predicted = 110.0 * attacker[LIGHT_FIGHTER]
        return {"attacker_loss_value": predicted, "win_prob": 0.8}

    reports = [
        valid_one,
        valid_two,
        missing_class,
        expedition,
        hamill,
        empty,
        unknown,
        invalid_rounds,
    ]
    with mock.patch.object(backtest, "load_reports", return_value=reports), mock.patch.object(
        backtest.battle_sim, "simulate", side_effect=fake_simulate
    ):
        result = backtest.run_backtest(samples=7)

    assert result["total_reports"] == 8
    assert result["eligible_reports"] == 2
    assert result["skipped_missing_class_meta"] == 1
    assert result["skipped_special"] == 2
    assert result["skipped_empty"] == 1
    assert result["skipped_unknown"] == 1
    assert result["skipped_invalid"] == 1
    assert result["samples_per_report"] == 7
    assert seen_tech == [
        {WEAPON: 3, SHIELD: 2, ARMOR: 1},
        {WEAPON: 3, SHIELD: 2, ARMOR: 1},
    ]
    assert result["predicted_loss_total"] == 330.0
    assert result["actual_loss_total"] == 300.0
    assert math.isclose(result["aggregate_loss_bias"], 0.10)
    assert math.isclose(result["aggregate_loss_error"], 0.10)
    assert math.isclose(result["win_brier"], 0.04)
    assert backtest.gate_passes(result, min_reports=2, max_loss_error=0.11)
    assert not backtest.gate_passes(result, min_reports=3, max_loss_error=0.11)
    assert not backtest.gate_passes(result, min_reports=2, max_loss_error=0.09)


def test_gate_rejects_nonfinite_loss_error():
    """실제 손실 0인데 손실을 예측하면 0 나누기로 우회하지 않고 탈락한다."""
    assert backtest._aggregate_loss_bias(0.0, 0.0) == 0.0
    error = abs(backtest._aggregate_loss_bias(1.0, 0.0))
    assert math.isinf(error)
    assert not backtest.gate_passes(
        {"eligible_reports": 100, "aggregate_loss_error": error},
        min_reports=57,
        max_loss_error=0.15,
    )


def test_safety_trips_on_three_consecutive_overruns_and_reset_clears_state():
    """gate 3: 3회 연속 초과 시 트립하고 reset은 즉시 해제한다."""
    original_state_dir = bs.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        bs.STATE_DIR = pathlib.Path(tmp)
        try:
            key = "raider@example.com"
            predicted = 6000.0
            threshold = max(bs.OVERRUN_FACTOR * predicted, bs.OVERRUN_ABS_FLOOR)

            first = bs.record_outcome(key, predicted, threshold + 1)
            second = bs.record_outcome(key, predicted, threshold + 1)
            third = bs.record_outcome(key, predicted, threshold + 1)
            fourth = bs.record_outcome(key, predicted, threshold + 1)

            assert first == {
                "tripped": False,
                "just_tripped": False,
                "streak": 1,
                "overrun": True,
            }
            assert second["streak"] == 2 and not second["tripped"]
            assert third["tripped"] and third["just_tripped"] and third["streak"] == 3
            assert fourth["tripped"] and not fourth["just_tripped"] and fourth["streak"] == 4
            assert bs.is_tripped(key)

            bs.reset_safety(key)
            assert not bs.is_tripped(key)
            saved = json.loads(bs._safety_path(key).read_text(encoding="utf-8"))
            assert saved == {"recent": [], "streak": 0, "tripped": False, "tripped_at": 0.0}
        finally:
            bs.STATE_DIR = original_state_dir


def test_safety_normal_outcome_breaks_overrun_streak():
    """gate 3: 중간의 정상 1건이 연속 초과 스트릭을 0으로 끊는다."""
    original_state_dir = bs.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        bs.STATE_DIR = pathlib.Path(tmp)
        try:
            key = "normal-resets-streak"
            predicted = 6000.0
            threshold = max(bs.OVERRUN_FACTOR * predicted, bs.OVERRUN_ABS_FLOOR)
            bs.record_outcome(key, predicted, threshold + 1)
            bs.record_outcome(key, predicted, threshold + 1)

            # 경계값과 같으면 초과('>')가 아니다.
            normal = bs.record_outcome(key, predicted, threshold)
            assert normal["streak"] == 0 and not normal["overrun"] and not normal["tripped"]

            one = bs.record_outcome(key, predicted, threshold + 1)
            two = bs.record_outcome(key, predicted, threshold + 1)
            assert one["streak"] == 1 and two["streak"] == 2
            assert not bs.is_tripped(key)
        finally:
            bs.STATE_DIR = original_state_dir


def test_safety_save_uses_atomic_replace_without_temp_residue():
    """gate 3 상태는 최종 파일에 직접 덮어쓰지 않고 temp -> replace로 저장한다."""
    original_state_dir = bs.STATE_DIR
    real_replace = pathlib.Path.replace
    replace_calls: list[tuple[pathlib.Path, pathlib.Path]] = []

    def tracking_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        replace_calls.append((source, pathlib.Path(target)))
        return real_replace(source, target)

    with tempfile.TemporaryDirectory() as tmp:
        bs.STATE_DIR = pathlib.Path(tmp)
        try:
            with mock.patch.object(pathlib.Path, "replace", new=tracking_replace):
                bs.reset_safety("atomic/account")

            final_path = bs._safety_path("atomic/account")
            assert len(replace_calls) == 1
            source, target = replace_calls[0]
            assert target == final_path
            assert source.parent == final_path.parent
            assert source.name.startswith(final_path.stem + ".tmp.")
            assert final_path.exists()
            assert not source.exists()
            assert list(final_path.parent.glob("*.tmp.*")) == []
            json.loads(final_path.read_text(encoding="utf-8"))
        finally:
            bs.STATE_DIR = original_state_dir


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
