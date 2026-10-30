#!/usr/bin/env python3
"""battle_reports 실전 표본으로 battle_sim 손실/승률 분포를 검증하는 배포 게이트.

게이트(사용자 지정):
  - 유효 실전 보고서가 기본 57건 이상
  - 전체 공격측 예상손실 합계가 실제손실 합계 대비 ±15% 이내

개별 전투는 서버/로컬 모두 난수라 한 건의 상대오차로 판정하면 잘못 탈락한다. 따라서 게이트는
독립 실전 표본 전체의 손실 합계를 사용하고, 개별 오차 분포(p50/p90), 함대가치 정규화 MAE,
승리확률 Brier score를 함께 출력한다. 이 스크립트는 botread 읽기 전용 계정만 사용한다.

주의: OGameX battle_reports는 General +2를 표시하지만 현재 Rust 전투 입력에는 그 보너스가
연결돼 있지 않다. 보고서 레벨에서 +2를 빼 원래 연구 레벨을 복원한 뒤 시뮬레이션한다.
counter-espionage/원정/달 파괴 보고서는 현재 클래스 필드를 저장하지 않아 +2 복원이
불가능하므로 게이트 표본에서 제외한다. Hamill Manoeuvre 발동 보고서도 이 순수 전투
시뮬레이터가 특수 전처리를 모델링하지 않으므로 제외한다.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
from typing import Any

PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from bots.rule_based import battle_sim  # noqa: E402
from bots.shared import db_reader  # noqa: E402
from bots.shared.game_objects import NAME_TO_ID  # noqa: E402


TECH_KEYS = {
    battle_sim.WEAPON_TECH: "weapon_technology",
    battle_sim.SHIELD_TECH: "shielding_technology",
    battle_sim.ARMOR_TECH: "armor_technology",
}


def _json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def _units(raw: Any) -> tuple[dict[int, int], list[str]]:
    obj = _json(raw, {})
    if not isinstance(obj, dict):
        return {}, []
    out: dict[int, int] = {}
    unknown: list[str] = []
    supported = battle_sim.supported_unit_ids()
    for name, amount in obj.items():
        uid = NAME_TO_ID.get(str(name))
        if uid is None or uid not in supported:
            # 요격/행성간 미사일(502/503)은 일반 전투에 참여하지 않으므로 무시한다.
            if uid not in (502, 503):
                unknown.append(str(name))
            continue
        try:
            n = int(amount)
        except (TypeError, ValueError):
            unknown.append(str(name))
            continue
        if n > 0:
            out[uid] = n
    return out, unknown


def _engine_tech(side: dict[str, Any]) -> dict[int, int]:
    """보고서 표시 레벨을 현재 Rust 입력에 실제 사용된 raw 연구 레벨로 복원."""
    shown_bonus = (
        battle_sim.GENERAL_REPORTED_COMBAT_BONUS
        if side.get("character_class") == "General"
        else 0
    )
    return {
        tid: max(0, int(side.get(name, 0) or 0) - shown_bonus)
        for tid, name in TECH_KEYS.items()
    }


def _unit_total(units: Any) -> int | None:
    """보고서 유닛 맵의 총 수량. 현재 저장 형식(name -> int)과 Rust 원형식
    (id -> {amount: int}) 둘 다를 받아, 형식이 깨지면 None을 반환한다.
    """
    if not isinstance(units, dict):
        return None
    total = 0
    for value in units.values():
        if isinstance(value, dict):
            value = value.get("amount", 0)
        try:
            amount = int(value or 0)
        except (TypeError, ValueError):
            return None
        if amount < 0:
            return None
        total += amount
    return total


def _actual_attacker_win(rounds_raw: Any) -> int | None:
    rounds = _json(rounds_raw, [])
    if not isinstance(rounds, list) or not rounds:
        return None
    if not isinstance(rounds[-1], dict):
        return None
    last = rounds[-1]
    attackers = _unit_total(last.get("attacker_ships"))
    defenders = _unit_total(last.get("defender_ships"))
    if attackers is None or defenders is None:
        return None
    att_alive = attackers > 0
    def_alive = defenders > 0
    return int(att_alive and not def_alive)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def load_reports() -> list[dict[str, Any]]:
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, general, attacker, defender, rounds FROM battle_reports "
            "WHERE attacker IS NOT NULL AND defender IS NOT NULL ORDER BY id"
        )
        return list(cur.fetchall())


def _aggregate_loss_bias(predicted: float, actual: float) -> float:
    """실제 손실 합계 대비 signed bias. 양수는 과대, 음수는 과소 예측.

    실제·예측이 모두 0이면 오차 0, 실제만 0인데 예측이 양수면 게이트가
    반드시 탈락하도록 +inf로 다룬다.
    """
    if actual > 0:
        return (predicted - actual) / actual
    return 0.0 if predicted == 0 else math.inf


def gate_passes(result: dict[str, Any], min_reports: int, max_loss_error: float) -> bool:
    """배포 게이트 단일 판정점. CLI와 단위 테스트가 같은 규칙을 쓴다."""
    error = float(result["aggregate_loss_error"])
    return (
        int(result["eligible_reports"]) >= max(0, int(min_reports))
        and math.isfinite(error)
        and error <= max_loss_error
    )


def run_backtest(samples: int) -> dict[str, Any]:
    rows = load_reports()
    errors_norm: list[float] = []
    signed_norm: list[float] = []
    brier: list[float] = []
    pred_total = actual_total = fleet_total = abs_error_total = 0.0
    skipped_empty = skipped_unknown = 0
    skipped_missing_class_meta = skipped_special = skipped_invalid = 0
    unknown_names: set[str] = set()

    for row in rows:
        attacker_meta = _json(row["attacker"], {})
        defender_meta = _json(row["defender"], {})
        if not isinstance(attacker_meta, dict) or not isinstance(defender_meta, dict):
            skipped_invalid += 1
            continue

        general = _json(row.get("general"), {})
        if isinstance(general, dict) and (
            general.get("expedition_battle")
            or general.get("hamill_manoeuvre_triggered")
        ):
            skipped_special += 1
            continue

        attacker, au = _units(attacker_meta.get("units"))
        defender, du = _units(defender_meta.get("units"))
        if au or du:
            skipped_unknown += 1
            unknown_names.update(au + du)
            continue
        if not attacker or not defender:
            skipped_empty += 1
            continue

        # General +2는 보고서 표시에만 있으므로 양쪽 클래스 키가 있어야
        # raw 연구 레벨을 역산할 수 있다. 값 None은 "클래스 미선택"이라 유효하다.
        if "character_class" not in attacker_meta or "character_class" not in defender_meta:
            skipped_missing_class_meta += 1
            continue

        actual_win = _actual_attacker_win(row["rounds"])
        try:
            actual = float(attacker_meta.get("resource_loss", 0) or 0)
        except (TypeError, ValueError):
            skipped_invalid += 1
            continue
        if actual_win is None or not math.isfinite(actual) or actual < 0:
            skipped_invalid += 1
            continue

        try:
            result = battle_sim.simulate(
                attacker,
                defender,
                att_tech=_engine_tech(attacker_meta),
                def_tech=_engine_tech(defender_meta),
                samples=samples,
                seed=int(row["id"]) * 104729,
            )
        except (TypeError, ValueError):
            skipped_invalid += 1
            continue
        predicted = float(result["attacker_loss_value"])
        fleet_value = max(1.0, battle_sim.fleet_cost_value(attacker))
        err = predicted - actual

        pred_total += predicted
        actual_total += actual
        fleet_total += fleet_value
        abs_error_total += abs(err)
        errors_norm.append(abs(err) / fleet_value)
        signed_norm.append(err / fleet_value)
        brier.append((float(result["win_prob"]) - actual_win) ** 2)

    eligible = len(errors_norm)
    aggregate_bias = _aggregate_loss_bias(pred_total, actual_total)
    return {
        "total_reports": len(rows),
        "eligible_reports": eligible,
        "skipped_empty": skipped_empty,
        "skipped_unknown": skipped_unknown,
        "skipped_missing_class_meta": skipped_missing_class_meta,
        "skipped_special": skipped_special,
        "skipped_invalid": skipped_invalid,
        "unknown_units": sorted(unknown_names),
        "samples_per_report": samples,
        "predicted_loss_total": pred_total,
        "actual_loss_total": actual_total,
        "aggregate_loss_bias": aggregate_bias,
        "aggregate_loss_error": abs(aggregate_bias),
        "normalized_mae": abs_error_total / max(1.0, fleet_total),
        "per_report_abs_error_p50": _percentile(errors_norm, 0.50),
        "per_report_abs_error_p90": _percentile(errors_norm, 0.90),
        "per_report_signed_error_mean": statistics.fmean(signed_norm) if signed_norm else math.nan,
        "win_brier": statistics.fmean(brier) if brier else math.nan,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--min-reports", type=int, default=57)
    parser.add_argument("--max-loss-error", type=float, default=0.15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_backtest(max(1, args.samples))
    passed = gate_passes(result, args.min_reports, args.max_loss_error)
    result["gate_passed"] = passed
    result["gate_min_reports"] = args.min_reports
    result["gate_max_loss_error"] = args.max_loss_error

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"표본 {result['eligible_reports']}/{result['total_reports']}건 "
            f"(빈 구성 {result['skipped_empty']}, 미지원 {result['skipped_unknown']}, "
            f"클래스 메타 누락 {result['skipped_missing_class_meta']}, "
            f"특수전 {result['skipped_special']}, 잘못된 보고서 {result['skipped_invalid']} 제외)"
        )
        print(
            f"공격측 손실 합계: 예측 {result['predicted_loss_total']:.0f} / "
            f"실제 {result['actual_loss_total']:.0f} / "
            f"bias {result['aggregate_loss_bias']:+.1%} / "
            f"절대오차 {result['aggregate_loss_error']:.1%}"
        )
        print(
            "개별 오차(함대가치 정규화): "
            f"MAE {result['normalized_mae']:.1%}, "
            f"p50 {result['per_report_abs_error_p50']:.1%}, "
            f"p90 {result['per_report_abs_error_p90']:.1%}"
        )
        print(f"승리확률 Brier: {result['win_brier']:.4f}")
        if result["unknown_units"]:
            print("미지원 유닛: " + ", ".join(result["unknown_units"]))
        print(
            "GATE " + ("PASS" if passed else "FAIL")
            + f" (표본≥{args.min_reports}, 손실합계 오차≤{args.max_loss_error:.0%})"
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
