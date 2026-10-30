"""계정 단위 함선 상한(행성+비행+건조대기) 회귀 테스트."""
from __future__ import annotations

import pytest

from bots.ai_agents import policy_engine as pe
from bots.rule_based import personality
from bots.shared import db_reader
from bots.shared.game_objects import LIGHT_FIGHTER, SMALL_CARGO, SOLAR_SATELLITE


EMAIL = "ai10@bots.local"


class _Cursor:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> dict:
        return self.row


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def test_db_reader_uses_one_snapshot_for_all_planets_flights_and_queue(monkeypatch):
    cursor = _Cursor({
        "planet_count": 3,
        "total_202": 12,
        "total_204": 209,
        "total_212": 87,
    })
    monkeypatch.setattr(db_reader, "_conn", lambda: _Conn(cursor))

    totals = db_reader.get_account_ship_totals_db(EMAIL)

    assert totals[SMALL_CARGO] == 12
    assert totals[LIGHT_FIGHTER] == 209
    assert totals[SOLAR_SATELLITE] == 87
    assert len(cursor.calls) == 1, "세 원천은 귀환 경계 누락을 피하도록 한 SELECT여야 함"
    sql, params = cursor.calls[0]
    assert params == (EMAIL, EMAIL, EMAIL)
    assert "SUM(p.small_cargo)" in sql
    assert "JOIN users u ON u.id=p.user_id" in sql  # 현재 행성 아닌 식민지/달도 포함
    assert "SUM(fm.small_cargo)" in sql
    assert "fm.processed=0 AND fm.canceled=0" in sql
    assert "object_amount-uq.object_amount_progress" in sql
    assert "uq.processed=0" in sql
    assert "fm.solar_satellite" not in sql  # 비행 불가능해 fleet 컬럼 자체가 없음


def test_db_reader_missing_account_is_not_silently_treated_as_zero(monkeypatch):
    cursor = _Cursor({"planet_count": 0})
    monkeypatch.setattr(db_reader, "_conn", lambda: _Conn(cursor))

    with pytest.raises(RuntimeError, match="account ships not found"):
        db_reader.get_account_ship_totals_db("missing@bots.local")


def _raider_shipyard_levels() -> dict[int, int]:
    # 소형화물/전투기 공용 커리큘럼 선행조건.
    return {21: 8, 115: 6, SMALL_CARGO: 0, LIGHT_FIGHTER: 0}


def test_shared_curriculum_uses_account_total_not_current_planet_levels():
    levels = _raider_shipyard_levels()

    local_only = personality.plan_secondary("raider", levels, tick_index=1)
    assert any(a[1] == LIGHT_FIGHTER for a in local_only)

    account = {SMALL_CARGO: 8, LIGHT_FIGHTER: 12}
    account_wide = personality.plan_secondary(
        "raider", levels, tick_index=1, account_ship_totals=account,
        require_account_ship_totals=True,
    )
    assert not any(a[0] == "ship" and a[1] == LIGHT_FIGHTER for a in account_wide)
    assert not any(a[0] == "ship" and a[1] == SMALL_CARGO for a in account_wide)


def test_shared_curriculum_fails_closed_only_when_live_total_is_required():
    levels = _raider_shipyard_levels()

    failed_read = personality.plan_secondary(
        "raider", levels, tick_index=1, account_ship_totals=None,
        require_account_ship_totals=True,
    )
    assert not any(a[0] == "ship" for a in failed_read)

    # 직접 호출하는 옛 코드에는 현재 행성 levels 기반 동작을 유지한다.
    legacy = personality.plan_secondary("raider", levels, tick_index=1)
    assert any(a[0] == "ship" for a in legacy)


def test_policy_max_total_blocks_overlapping_shared_candidate_by_account_total():
    levels = _raider_shipyard_levels()
    shared = [("ship", LIGHT_FIGHTER, 3)]
    policy = [{"id": LIGHT_FIGHTER, "batch": 5, "max_total": 3}]

    out = pe.apply_ship_priority(
        shared, policy, levels, account_ship_totals={LIGHT_FIGHTER: 209}
    )
    assert out == []


def test_policy_max_total_clamps_batch_to_exact_remaining_capacity():
    levels = _raider_shipyard_levels()
    shared = [("ship", LIGHT_FIGHTER, 5)]
    policy = [{"id": LIGHT_FIGHTER, "batch": 5, "max_total": 50}]

    out = pe.apply_ship_priority(
        shared, policy, levels, account_ship_totals={LIGHT_FIGHTER: 48}
    )
    assert out == [("ship", LIGHT_FIGHTER, 2)]


def test_full_max_total_only_guards_manual_extra_not_dynamic_shared_growth():
    levels = _raider_shipyard_levels()
    dynamic = [("ship", LIGHT_FIGHTER, 17)]
    policy = [{"id": LIGHT_FIGHTER, "batch": 5, "max_total": 50}]

    out = pe.apply_ship_priority(
        dynamic,
        policy,
        levels,
        account_ship_totals={LIGHT_FIGHTER: 209},
        enforce_max_total_on_shared=False,
    )

    # The stale manual batch is stopped at max_total, but full autonomy's
    # resource/composition-driven strategic candidate keeps growing.
    assert out == dynamic


def test_policy_guarded_ship_fails_closed_when_account_total_read_failed():
    levels = _raider_shipyard_levels()
    shared = [("ship", LIGHT_FIGHTER, 3)]
    policy = [{"id": LIGHT_FIGHTER, "batch": 5, "max_total": 50}]

    out = pe.apply_ship_priority(
        shared, policy, levels, account_ship_totals=None
    )
    assert out == []

    # 가드 없는 옛 정책은 합계가 필요 없으므로 하위호환상 계속 집행된다.
    guardless = pe.apply_ship_priority(
        [], [{"id": LIGHT_FIGHTER, "batch": 2}], levels,
        account_ship_totals=None,
    )
    assert guardless == [("ship", LIGHT_FIGHTER, 2)]
