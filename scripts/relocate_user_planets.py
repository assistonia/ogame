#!/usr/bin/env python3
"""사용자 01/02 행성을 같은 슬롯로 1:39에 원자적으로 재배치한다.

기본은 dry-run. ``--apply``는 유니버스 정지, 소유권·현재좌표·목적지·식민예약을
재검증하고 DB 백업 뒤 행성 ID를 유지한 채 좌표와 활성 미션 스냅샷을 함께 갱신한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from dataclasses import asdict


PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts import rebalance_ai_positions as relocation


USER_EMAIL = "123123123@naver.com"
ROUTES = {
    "01": ((1, 44, 5), (1, 39, 5)),
    "02": ((1, 44, 8), (1, 39, 8)),
}


def plan_moves(
    planets: list[relocation.PlanetRow],
    *,
    reservations: set[tuple[int, int, int]] | None = None,
) -> list[relocation.Move]:
    account = [row for row in planets if row.email == USER_EMAIL]
    by_name: dict[str, relocation.PlanetRow] = {}
    for row in account:
        if row.name in ROUTES:
            if row.name in by_name:
                raise RuntimeError(f"duplicate planet name: {row.name}")
            by_name[row.name] = row
    missing = set(ROUTES) - set(by_name)
    if missing:
        raise RuntimeError(f"user planets missing: {sorted(missing)}")
    if any(row.planet_type != 1 for row in by_name.values()):
        raise RuntimeError("moon/non-planet relocation is not supported")

    moving_ids = {row.planet_id for row in by_name.values()}
    occupied = {
        (row.galaxy, row.system, row.position)
        for row in planets if row.planet_id not in moving_ids
    }
    occupied.update(reservations or set())
    targets = [target for _source, target in ROUTES.values()]
    if len(set(targets)) != len(targets):
        raise RuntimeError("duplicate relocation target")

    moves: list[relocation.Move] = []
    for name, (expected_source, target) in ROUTES.items():
        row = by_name[name]
        current = (row.galaxy, row.system, row.position)
        if current not in {expected_source, target}:
            raise RuntimeError(
                f"unexpected current coordinate for {name}: {current}"
            )
        if current != target and target in occupied:
            raise RuntimeError(f"destination occupied or reserved: {target}")
        moves.append(relocation.Move(
            planet_id=row.planet_id,
            email=row.email,
            name=row.name,
            old_galaxy=row.galaxy,
            old_system=row.system,
            old_position=row.position,
            new_galaxy=target[0],
            new_system=target[1],
            new_position=target[2],
        ))
    return moves


def build_report(conn) -> tuple[list[relocation.Move], dict[str, object]]:
    with conn.cursor() as cur:
        moves = plan_moves(
            relocation._rows(cur),
            reservations=relocation._colonisation_reservations(cur),
        )
        changed = [move for move in moves if move.old_coord != move.new_coord]
        active = relocation._active_mission_count(
            cur, [move.planet_id for move in changed]
        )
    return moves, {
        "changed_planets": len(changed),
        "active_missions_touched": active,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.apply and relocation._universe_running():
        print("REFUSED: run_universe.py is still running")
        return 2

    conn = relocation._connect(apply=args.apply)
    try:
        moves, summary = build_report(conn)
        result: dict[str, object] = {
            "mode": "apply" if args.apply else "dry-run",
            **summary,
            "moves": [asdict(move) for move in moves if move.old_coord != move.new_coord],
        }
        if args.apply and summary["changed_planets"]:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            result.update(relocation._apply_db(
                conn, moves, stamp, prefix_base="ops_user_relocation",
            ))
            result["local_state"] = relocation._migrate_local_state(
                moves, stamp, backup_namespace="user_planet_relocation",
            )
        elif args.apply:
            result.update({"backup_prefix": None, "local_state": {}, "no_op": True})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if args.apply:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
