from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import rebalance_ai_positions as rebalance


def _planet(pid: int, email: str, system: int, position: int, name: str = "Homeworld"):
    return rebalance.PlanetRow(pid, email, name, 1, 1, system, position)


def test_plan_spreads_ai_homeworlds_and_preserves_same_system_planet_cluster():
    rows = []
    pid = 1
    for email, target in rebalance.TARGET_SYSTEMS.items():
        # ai01만 이미 목표 시스템, 나머지는 끝 구간에 몰려 있다고 가정.
        source = target if email == "ai01@bots.local" else 40 + pid
        rows.append(_planet(pid, email, source, 8))
        if email != "ai02@bots.local":
            rows.append(_planet(100 + pid, email, source, 9, "Colony"))
        pid += 1
    # 모든 목표 시스템 중앙 슬롯을 규칙봇이 일부 점유.
    rows.extend(
        _planet(500 + n, f"bot{n:03d}@bots.local", target, 8)
        for n, target in enumerate(rebalance.TARGET_SYSTEMS.values(), start=1)
        if target != 2
    )

    moves = rebalance.plan_moves(rows)

    for email, target in rebalance.TARGET_SYSTEMS.items():
        account = [move for move in moves if move.email == email]
        assert account
        assert {move.new_system for move in account} == {target}
    assert len({move.new_coord for move in moves}) == len(moves)


def test_plan_never_uses_rule_planet_or_inflight_colonisation_slot():
    rows = []
    for n, (email, target) in enumerate(rebalance.TARGET_SYSTEMS.items(), start=1):
        rows.append(_planet(n, email, 40 + n, 8))
        rows.append(_planet(100 + n, f"bot{n:03d}@bots.local", target, 8))
    reserved = {(1, target, 7) for target in rebalance.TARGET_SYSTEMS.values()}

    moves = rebalance.plan_moves(rows, reservations=reserved)

    assert all(move.new_position not in {7, 8} for move in moves if move.new_system != 2)


def test_plan_is_idempotent_after_first_layout():
    rows = []
    for n, (email, target) in enumerate(rebalance.TARGET_SYSTEMS.items(), start=1):
        rows.append(_planet(n, email, target, 6))

    moves = rebalance.plan_moves(rows)

    assert all(move.old_coord == move.new_coord for move in moves)
