from __future__ import annotations

import pytest

from scripts import rebalance_ai_positions as rebalance
from scripts import relocate_user_planets as relocate


def _planet(pid: int, email: str, name: str, system: int, position: int):
    return rebalance.PlanetRow(pid, email, name, 1, 1, system, position)


def _fixture():
    return [
        _planet(103, relocate.USER_EMAIL, "01", 44, 5),
        _planet(110, relocate.USER_EMAIL, "02", 44, 8),
        _planet(237, relocate.USER_EMAIL, "03", 42, 8),
        _planet(1, "bot001@bots.local", "Homeworld", 38, 5),
    ]


def test_plan_keeps_planet_ids_and_slots_while_changing_only_system():
    moves = relocate.plan_moves(_fixture())
    assert [(move.planet_id, move.old_coord, move.new_coord) for move in moves] == [
        (103, (1, 44, 5), (1, 39, 5)),
        (110, (1, 44, 8), (1, 39, 8)),
    ]


def test_plan_refuses_occupied_or_colonisation_reserved_destination():
    occupied = _fixture() + [_planet(999, "other@local", "X", 39, 5)]
    with pytest.raises(RuntimeError, match="destination occupied"):
        relocate.plan_moves(occupied)
    with pytest.raises(RuntimeError, match="destination occupied"):
        relocate.plan_moves(_fixture(), reservations={(1, 39, 8)})


def test_plan_is_idempotent_and_refuses_unexpected_source():
    moved = [
        _planet(103, relocate.USER_EMAIL, "01", 39, 5),
        _planet(110, relocate.USER_EMAIL, "02", 39, 8),
    ]
    assert all(move.old_coord == move.new_coord for move in relocate.plan_moves(moved))

    wrong = _fixture()
    wrong[0] = _planet(103, relocate.USER_EMAIL, "01", 40, 5)
    with pytest.raises(RuntimeError, match="unexpected current coordinate"):
        relocate.plan_moves(wrong)
