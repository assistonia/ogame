"""리사이클러 건조 + 데브리 수거 루틴의 순수 로직 단위검증 (HTTP/DB 없이).

raid_tick 전체는 HTTP/DB 부작용이 있어 여기서 실행하지 않고, 데브리 후보 선정(임계/거리/
제외 좌표)·리사이클러 용량 계산·선행조건 게이트 같은 결정 로직만 고정 입력으로 검증한다.
DB 를 타는 함수(find_debris_targets)는 read_debris_fields 를 monkeypatch 해 순수 로직만 본다.
실제 파견/건조 검증은 드라이런으로 별도 수행한다.

실행: `python3 -m pytest tests/test_recycler.py` 또는 `python3 tests/test_recycler.py`.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import raider as rd

RECYCLER, SHIPYARD, COMBUSTION, SHIELDING = 209, 21, 115, 110


# --- 선행조건 게이트 -------------------------------------------------------
def test_prereqs_met_exact_thresholds():
    """조선소4+연소6+실드2 를 정확히 충족하면 True."""
    lv = {SHIPYARD: 4, COMBUSTION: 6, SHIELDING: 2}
    assert rd._recycler_prereqs_met(lv) is True


def test_prereqs_unmet_when_combustion_low():
    """연소가 6 미만이면(레이더 커리큘럼 기본값 2) 게이트 미통과."""
    lv = {SHIPYARD: 4, COMBUSTION: 2, SHIELDING: 2}
    assert rd._recycler_prereqs_met(lv) is False


def test_prereqs_unmet_when_missing_key():
    """조회에 없는 선행조건은 0 으로 보고 미통과."""
    assert rd._recycler_prereqs_met({SHIPYARD: 4}) is False
    assert rd._recycler_prereqs_met({}) is False


# --- 용량(리사이클러 수) 계산 ---------------------------------------------
def test_recyclers_for_value_ceil():
    """필요 수 = ceil(가치/용량). 용량 20000 기준."""
    assert rd._recyclers_for_value(20000, 10) == 1     # 딱 1기
    assert rd._recyclers_for_value(20001, 10) == 2     # 넘치면 올림
    assert rd._recyclers_for_value(50000, 10) == 3     # ceil(2.5)=3
    assert rd._recyclers_for_value(1, 10) == 1         # 소량도 1기


def test_recyclers_for_value_clamped_by_owned():
    """보유량보다 많이 필요해도 보유량으로 클램프."""
    assert rd._recyclers_for_value(200000, 2) == 2     # ceil=10 이지만 보유 2
    assert rd._recyclers_for_value(50000, 0) == 0       # 보유 0 이면 0


# --- 데브리 후보 선정 (임계/거리/제외 좌표) -------------------------------
def _patch_debris(monkeypatch, rows):
    """read_debris_fields 를 고정 rows 로 대체(DB 우회)."""
    monkeypatch.setattr(rd, "read_debris_fields",
                        lambda g, s, span: [dict(r) for r in rows])


def _field(g, s, p, metal, crystal):
    return {"coord": (g, s, p), "metal": metal, "crystal": crystal,
            "value": metal + crystal}


def test_find_targets_threshold_filter(monkeypatch):
    """가치 < HARVEST_MIN_VALUE 데브리는 후보에서 제외."""
    _patch_debris(monkeypatch, [
        _field(1, 50, 8, 1000, 1000),   # 2000 < 5000 → 제외
        _field(1, 50, 9, 3000, 3000),   # 6000 ≥ 5000 → 포함
    ])
    out = rd.find_debris_targets(1, 50, own=(1, 50, 5))
    coords = [t["coord"] for t in out]
    assert coords == [(1, 50, 9)]


def test_find_targets_includes_former_user_system_but_excludes_expedition(monkeypatch):
    """사람 하드 제외는 제거됐고, 원정 데브리(position 16)만 계속 제외한다."""
    _patch_debris(monkeypatch, [
        _field(1, 44, 5, 100000, 100000),  # 구 사람 시스템도 이제 일반 후보
        _field(1, 50, 16, 100000, 100000),  # position 16 원정 데브리 → 제외
        _field(1, 50, 7, 10000, 10000),     # 정상 → 포함
    ])
    out = rd.find_debris_targets(1, 50, own=(1, 50, 5))
    assert [t["coord"] for t in out] == [(1, 44, 5), (1, 50, 7)]


def test_find_targets_includes_own_coord_for_counter_harvest(monkeypatch):
    """자기 좌표의 방어전 데브리도 카운터 수거 대상에 포함."""
    _patch_debris(monkeypatch, [
        _field(1, 50, 5, 50000, 50000),   # 자기 좌표 → 방어전 수거
        _field(1, 50, 6, 50000, 50000),   # 이웃 → 포함
    ])
    out = rd.find_debris_targets(1, 50, own=(1, 50, 5))
    assert [t["coord"] for t in out] == [(1, 50, 5), (1, 50, 6)]


def test_find_targets_sorted_value_then_distance(monkeypatch):
    """가치 내림차순, 동률이면 자기 시스템에서 가까운 순."""
    _patch_debris(monkeypatch, [
        _field(1, 48, 4, 10000, 10000),   # value 20000, dist 2
        _field(1, 51, 4, 25000, 25000),   # value 50000, dist 1  ← 최상위(가치)
        _field(1, 49, 4, 10000, 10000),   # value 20000, dist 1  ← 동률이면 가까운 쪽 먼저
    ])
    out = rd.find_debris_targets(1, 50, own=(1, 50, 5))
    assert [t["coord"] for t in out] == [(1, 51, 4), (1, 49, 4), (1, 48, 4)]


# --- 기댓값 연동: 리사이클러 보유 시 데브리 할인 해제 ----------------------
def test_debris_factor_linkage():
    """리사이클러 보유 레이더는 데브리 할인(0.3)이 1.0 이 되어야(EV 전액 반영).

    _raid_tick_inner 내부 로직과 동일한 판정식을 직접 검증한다(코드 상수 재사용).
    """
    # 미보유: 할인 factor
    have_rec = 0
    factor = 1.0 if have_rec > 0 else rd.DEBRIS_NO_RECYCLER_FACTOR
    assert factor == rd.DEBRIS_NO_RECYCLER_FACTOR < 1.0
    # 보유: 전액
    have_rec = 2
    factor = 1.0 if have_rec > 0 else rd.DEBRIS_NO_RECYCLER_FACTOR
    assert factor == 1.0


# --- 수거 단계: 슬롯 상한 / 쿨다운 / 파견 함선 수 --------------------------
class _FakeClient:
    pass


def test_pending_recycler_build_checks_all_owned_planets(monkeypatch):
    """현재 선택 행성이 아니라 user_id 소유권으로 모든 식민지 큐를 확인한다."""
    class Cursor:
        sql = ""
        params = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchone(self):
            return {"c": 1}

    cur = Cursor()

    class Conn:
        def cursor(self):
            return cur

    monkeypatch.setattr(rd.db_reader, "_conn", lambda: Conn())
    assert rd.pending_recycler_build("r@x") is True
    assert "u.id = p.user_id" in cur.sql
    assert "planet_current" not in cur.sql
    assert cur.params == ("r@x", rd.RECYCLER)


def test_harvest_step_sends_capacity_matched(monkeypatch):
    """수거 파견은 용량 맞춤 리사이클러와 예약분 밖 화물을 함께 보낸다."""
    sent = {}

    def fake_harvest(client, target, recyclers=1, **kw):
        sent["target"] = target
        sent["recyclers"] = recyclers
        sent["support_ships"] = kw.get("support_ships")
        return {"ok": True}

    monkeypatch.setattr(rd.fleet, "harvest", fake_harvest)
    monkeypatch.setattr(rd, "_save_state", lambda *a, **k: None)

    targets = [_field(1, 50, 9, 30000, 30000)]  # value 60000 → ceil(60000/20000)=3
    # now 는 쿨다운(HARVEST_COOLDOWN_SEC)보다 커야 첫 파견이 막히지 않는다(기록 없음=0 기준).
    note = rd._harvest_step(_FakeClient(), "e@x", targets, have_rec=5,
                            state={"targets": {}}, now=1e9, have_sc=8, have_lc=2)
    assert sent["target"] == (1, 50, 9)
    assert sent["recyclers"] == 3
    # 리사이클러만으로 60k가 맞아도 가용 소형화물 1대는 항상 동행한다.
    assert sent["support_ships"] == {rd.SMALL_CARGO: 1}
    assert note and "HARVEST" in note and "1:50:9" in note and "소형화물1" in note


def test_harvest_support_cargo_fills_shortfall_after_reserves():
    """리사이클러 부족 용량부터 채우되 SC3/LC1은 약탈·대피용으로 남긴다."""
    cargo = rd._harvest_support_cargo(
        value=100000,
        recyclers=2,       # 40k, 부족 60k
        have_sc=10,        # 가용 7
        have_lc=3,         # 가용 2
    )
    assert cargo == {rd.SMALL_CARGO: 2, rd.LARGE_CARGO: 2}
    assert rd._sizing_cargo_capacity(cargo) == 60000


def test_harvest_support_uses_one_ship_when_only_reserved_cargo_is_parked():
    assert rd._harvest_support_cargo(
        value=100000, recyclers=1,
        have_sc=rd.HARVEST_RESERVED_SMALL_CARGO,
        have_lc=rd.HARVEST_RESERVED_LARGE_CARGO,
    ) == {rd.SMALL_CARGO: 1}


def test_harvest_support_is_empty_only_when_no_cargo_is_parked():
    assert rd._harvest_support_cargo(
        value=100000, recyclers=1, have_sc=0, have_lc=0,
    ) == {}


def test_harvest_step_waits_instead_of_sending_recycler_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rd.fleet, "harvest",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    note = rd._harvest_step(
        _FakeClient(),
        "e@x",
        [_field(1, 50, 9, 30000, 30000)],
        have_rec=3,
        state={"targets": {}},
        now=1e9,
        have_sc=0,
        have_lc=0,
    )

    assert note == "raid HARVEST WAIT 1:50:9 (동행 화물선 0, 귀환/건조 대기)"
    assert calls == []


def test_harvest_wrapper_preserves_recycler_gate_and_adds_support(monkeypatch):
    sent = {}

    def fake_send(_client, mission, target, ships, **kw):
        sent.update(mission=mission, target=target, ships=ships, kw=kw)
        return {"ok": True}

    monkeypatch.setattr(rd.fleet, "send_fleet", fake_send)
    rd.fleet.harvest(
        _FakeClient(), (1, 50, 9), recyclers=2,
        support_ships={rd.SMALL_CARGO: 4, rd.LARGE_CARGO: 1},
    )

    assert sent["mission"] == rd.fleet.MISSION_RECYCLE
    assert sent["ships"] == {
        rd.SMALL_CARGO: 4,
        rd.LARGE_CARGO: 1,
        rd.RECYCLER: 2,
    }
    assert sent["kw"]["target_type"] == rd.fleet.PLANET_TYPE_DEBRIS


def test_parked_harvest_inventory_uses_live_availability(monkeypatch):
    monkeypatch.setattr(
        rd.db_reader, "get_levels_db",
        lambda _email: {rd.RECYCLER: 2, rd.SMALL_CARGO: 9, rd.LARGE_CARGO: 4},
    )
    assert rd._parked_harvest_inventory("e@x", {}) == {
        rd.RECYCLER: 2,
        rd.SMALL_CARGO: 9,
        rd.LARGE_CARGO: 4,
    }


def test_incoming_attack_to_coord_returns_eta_and_filters_sql(monkeypatch):
    class Cursor:
        sql = ""
        params = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchone(self):
            return {
                "mission_id": 77,
                "arrival_ts": 1061,
                "g": 1,
                "s": 50,
                "p": 5,
            }

    cur = Cursor()

    class Conn:
        def cursor(self):
            return cur

    monkeypatch.setattr(rd.db_reader, "_conn", lambda: Conn())
    attack = rd.incoming_attack_to_coord("e@x", (1, 50, 5), now=1000.2)

    assert attack == {
        "mission_id": 77,
        "coord": (1, 50, 5),
        "arrival_ts": 1061,
        "eta_sec": 61,
    }
    assert "fm.processed=0" in cur.sql and "fm.canceled=0" in cur.sql
    assert "fm.mission_type=1" in cur.sql
    assert cur.params == ("e@x", 1, 50, 5)


def test_counter_harvest_reservation_round_trip():
    state = {"targets": {}}
    attack = {
        "mission_id": 77,
        "coord": (1, 50, 5),
        "arrival_ts": 1061,
    }
    rd._set_counter_harvest_reservation(state, attack)
    assert rd._counter_harvest_reservation(state) == attack
    rd._clear_counter_harvest_reservation(state)
    assert rd._counter_harvest_reservation(state) is None


def test_raid_tick_holds_all_outgoing_actions_until_incoming_eta(monkeypatch):
    state = {"targets": {}}
    saved = []
    monkeypatch.setattr(rd.time, "time", lambda: 1000.0)
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: (0, False, 0))
    monkeypatch.setattr(rd, "_load_state", lambda _email: state)
    monkeypatch.setattr(rd, "_save_state", lambda _email, value: saved.append(dict(value)))
    monkeypatch.setattr(rd, "own_coords", lambda _client: (1, 50, 5))
    monkeypatch.setattr(rd, "find_debris_targets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        rd,
        "incoming_attack_to_coord",
        lambda *_args, **_kwargs: {
            "mission_id": 77,
            "coord": (1, 50, 5),
            "arrival_ts": 1060,
            "eta_sec": 60,
        },
    )
    monkeypatch.setattr(
        rd, "scan_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incoming 중에는 공격/정찰 스캔 금지")
        ),
    )

    note = rd._raid_tick_inner(_FakeClient(), "e@x", {rd.RECYCLER: 1})

    assert note and "COUNTER HOLD 1:50:5" in note and "ETA60s" in note
    assert rd._counter_harvest_reservation(state) == {
        "mission_id": 77,
        "coord": (1, 50, 5),
        "arrival_ts": 1060,
    }
    assert saved


def test_raid_tick_launches_reserved_counter_harvest_before_normal_raid(monkeypatch):
    state = {
        "targets": {},
        "counter_harvest": {
            "mission_id": 77,
            "coord": [1, 50, 5],
            "arrival_ts": 1000,
        },
    }
    sent = {}
    monkeypatch.setattr(rd.time, "time", lambda: 1010.0)
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: (0, False, 0))
    monkeypatch.setattr(rd, "_load_state", lambda _email: state)
    monkeypatch.setattr(rd, "_save_state", lambda *_args: None)
    monkeypatch.setattr(rd, "own_coords", lambda _client: (1, 50, 5))
    monkeypatch.setattr(
        rd, "find_debris_targets",
        lambda *_args, **_kwargs: [_field(1, 50, 5, 30000, 30000)],
    )
    monkeypatch.setattr(rd, "incoming_attack_to_coord", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rd,
        "_parked_harvest_inventory",
        lambda *_args: {rd.RECYCLER: 2, rd.SMALL_CARGO: 5, rd.LARGE_CARGO: 1},
    )

    def harvest(_client, target, recyclers=1, **kw):
        sent.update(target=target, recyclers=recyclers, support=kw["support_ships"])
        return {"ok": True}

    monkeypatch.setattr(rd.fleet, "harvest", harvest)
    monkeypatch.setattr(
        rd, "scan_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("counter harvest는 일반 raid보다 먼저여야 함")
        ),
    )

    note = rd._raid_tick_inner(
        _FakeClient(), "e@x", {rd.RECYCLER: 2, rd.SMALL_CARGO: 5, rd.LARGE_CARGO: 1}
    )

    assert note and "COUNTER HARVEST 1:50:5" in note
    assert sent == {
        "target": (1, 50, 5),
        "recyclers": 2,
        "support": {rd.SMALL_CARGO: 2},
    }
    assert rd._counter_harvest_reservation(state) is None


def test_counter_reservation_survives_when_no_cargo_is_parked(monkeypatch):
    state = {
        "targets": {},
        "counter_harvest": {
            "mission_id": 77,
            "coord": [1, 50, 5],
            "arrival_ts": 1000,
        },
    }
    monkeypatch.setattr(rd.time, "time", lambda: 1010.0)
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: (0, False, 0))
    monkeypatch.setattr(rd, "_load_state", lambda _email: state)
    monkeypatch.setattr(rd, "_save_state", lambda *_args: None)
    monkeypatch.setattr(rd, "own_coords", lambda _client: (1, 50, 5))
    monkeypatch.setattr(
        rd, "find_debris_targets",
        lambda *_args, **_kwargs: [_field(1, 50, 5, 30000, 30000)],
    )
    monkeypatch.setattr(rd, "incoming_attack_to_coord", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rd,
        "_parked_harvest_inventory",
        lambda *_args: {rd.RECYCLER: 2, rd.SMALL_CARGO: 0, rd.LARGE_CARGO: 0},
    )
    monkeypatch.setattr(
        rd.fleet, "harvest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("화물선 0이면 리사이클러 단독 파견 금지")
        ),
    )

    note = rd._raid_tick_inner(_FakeClient(), "e@x", {rd.RECYCLER: 2})

    assert note and "COUNTER HARVEST WAIT 1:50:5" in note
    assert rd._counter_harvest_reservation(state) == {
        "mission_id": 77,
        "coord": (1, 50, 5),
        "arrival_ts": 1000,
    }


def test_harvest_step_respects_cooldown(monkeypatch):
    """쿨다운(HARVEST_COOLDOWN_SEC) 안이면 그 데브리로는 파견하지 않는다."""
    calls = []
    monkeypatch.setattr(rd.fleet, "harvest",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    monkeypatch.setattr(rd, "_save_state", lambda *a, **k: None)

    now = 10000.0
    # 최근(쿨다운 내) 수거 기록이 있는 좌표
    state = {"targets": {"1:50:9": {"harvest": now - 100}}}
    targets = [_field(1, 50, 9, 60000, 0)]
    note = rd._harvest_step(_FakeClient(), "e@x", targets, have_rec=5,
                            state=state, now=now)
    assert note is None and calls == [], "쿨다운 내면 파견 없어야"


def test_harvest_max_concurrent_is_one():
    """동시 수거 상한은 1건(공격 슬롯과 경합 방지)."""
    assert rd.HARVEST_MAX_CONCURRENT == 1


def test_harvest_keeps_one_slot_for_raid():
    """max_active=3에서 active=2면 수거로 마지막 슬롯을 채우지 않는다."""
    assert rd._harvest_slot_safe(active=1, max_active=3) is True
    assert rd._harvest_slot_safe(active=2, max_active=3) is False
    assert rd.HARVEST_RESERVED_RAID_SLOTS == 1


def test_harvest_reserved_slot_is_policy_controlled():
    assert rd._harvest_slot_safe(active=2, max_active=3, reserved_raid_slots=0) is True
    assert rd._harvest_slot_safe(active=1, max_active=3, reserved_raid_slots=2) is False


def test_zero_harvest_concurrency_removes_artificial_cap_instead_of_disabling():
    assert rd._harvest_concurrency_limit(0, max_active=7) == 7
    assert rd._harvest_concurrency_limit(3, max_active=7) == 3


if __name__ == "__main__":
    import traceback

    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]

    class _MP:
        """pytest 없이 실행할 때 쓰는 최소 monkeypatch (setattr 만, 종료 시 복원)."""

        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    failed = 0
    for name, fn in tests:
        mp = _MP()
        try:
            if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                fn(mp)
            else:
                fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}:\n{traceback.format_exc()}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
