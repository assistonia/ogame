"""공격 직전 2단계 재정찰+24h peak envelope 안전 preflight."""
from __future__ import annotations

from bots.rule_based import raider as rd


COORD = (1, 50, 7)
OWN = (1, 50, 5)
EMAIL = "r@x"


def test_preflight_is_live_by_default():
    assert rd.RAID_ATTACK_PREFLIGHT_ENABLED is True


def _report(
    *, report_id: int = 10, created_at_ts: float = 9500.0,
    ships: dict[int, int] | None = None, defense: dict[int, int] | None = None,
    research: dict[int, int] | None = None, res_sum: int = 30_000,
) -> dict:
    ships = {} if ships is None else ships
    defense = {} if defense is None else defense
    research = {rd.ESPIONAGE_TECH: 3} if research is None else research
    return {
        "report_id": report_id,
        "created_at_ts": created_at_ts,
        "created_at": "fake",
        "fresh": True,
        "age_sec": max(0, int(10_000 - created_at_ts)),
        "resources": {"metal": res_sum, "crystal": 0, "deuterium": 0},
        "res_sum": res_sum,
        "ships": ships,
        "defense_units": defense,
        "defense": sum(defense.values()),
        "research": research,
    }


def _pending_state(*, requested_at: float = 9900.0, before: int = 10) -> dict:
    return {
        "targets": {
            "1:50:7": {
                "attack_preflight": {
                    "requested_at": requested_at,
                    "report_id_before": before,
                    "probes": 7,
                }
            }
        }
    }


def _levels(*, small_cargo: int = 4) -> dict[int, int]:
    return {
        rd.LIGHT_FIGHTER: 8,
        rd.SMALL_CARGO: small_cargo,
        rd.ESPIONAGE_PROBE: 12,
        rd.ESPIONAGE_TECH: 3,
        109: 3,
        110: 3,
        111: 3,
        115: 6,
        117: 4,
    }


def _patch_tick(monkeypatch, report: dict, state: dict):
    attacks: list[tuple] = []
    spies: list[tuple] = []
    saved: list[dict] = []
    monkeypatch.setattr(rd, "RAID_ATTACK_PREFLIGHT_ENABLED", True)
    monkeypatch.setattr(rd, "raid_loot_percent", lambda _email: 50)
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: (0, False, 0))
    monkeypatch.setattr(rd, "own_coords", lambda _client: OWN)
    monkeypatch.setattr(
        rd, "scan_targets",
        lambda *_args, **_kwargs: [{"coord": COORD, "inactive": True}],
    )
    monkeypatch.setattr(rd, "read_spy_reports", lambda _email: {COORD: report})
    monkeypatch.setattr(rd, "_load_state", lambda _email: state)
    monkeypatch.setattr(rd, "_save_state", lambda _email, value: saved.append(value))
    monkeypatch.setattr(rd, "_battle_sim_shadow", lambda *_args, **_kwargs: None)
    # 계정 함대가치 1M → 동적 preflight 한도 150k. 행성/비행/큐 합산 함수 자체는
    # test_account_ship_totals.py에서 별도로 검증한다.
    monkeypatch.setattr(
        rd.db_reader, "get_account_ship_totals_db",
        lambda _email: {rd.LIGHT_FIGHTER: 250},
    )
    monkeypatch.setattr(rd.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(
        rd.fleet, "spy",
        lambda _client, coord, probes: spies.append((coord, probes)) or {"ok": True},
    )
    monkeypatch.setattr(
        rd.fleet, "attack",
        lambda _client, coord, ships: attacks.append((coord, dict(ships))) or {"ok": True},
    )
    return attacks, spies, saved


def test_old_report_cannot_attack_immediately_and_requests_respy(monkeypatch):
    state = {"targets": {}}
    attacks, spies, _saved = _patch_tick(monkeypatch, _report(), state)

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert spies == [(COORD, 7)]
    request = state["targets"]["1:50:7"]["attack_preflight"]
    assert request["requested_at"] == 10_000.0
    assert request["report_id_before"] == 10
    assert note and "PREFLIGHT SPY" in note and "다음 틱" in note


def test_report_created_before_requested_at_is_rejected(monkeypatch):
    state = _pending_state(requested_at=9900.0)
    # id만 새로워도 생성 시각이 requested_at 이전이면 소비 금지.
    attacks, spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9899.0), state
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == [] and spies == []
    assert "attack_preflight" in state["targets"]["1:50:7"]
    assert note and "PREFLIGHT WAIT" in note


def test_new_full_intel_showing_returned_fleet_blocks_attack(monkeypatch):
    state = _pending_state()
    attacks, spies, _saved = _patch_tick(
        monkeypatch,
        _report(report_id=11, created_at_ts=9950.0, ships={rd.LIGHT_FIGHTER: 12}),
        state,
    )
    monkeypatch.setattr(
        rd, "read_spy_combat_peak",
        lambda *_args: (_ for _ in ()).throw(AssertionError("current report must block first")),
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == [] and spies == []
    assert "attack_preflight" not in state["targets"]["1:50:7"]
    assert note and "복귀함대 탐지" in note


def test_dynamic_fleet_value_budget_has_50k_floor(monkeypatch):
    monkeypatch.setattr(
        rd.db_reader, "get_account_ship_totals_db",
        lambda _email: {rd.LIGHT_FIGHTER: 10},
    )

    assert rd._preflight_fleet_value_budget(EMAIL) == (50_000, 40_000)


def test_dynamic_fleet_value_budget_scales_to_15_percent(monkeypatch):
    monkeypatch.setattr(
        rd.db_reader, "get_account_ship_totals_db",
        lambda _email: {rd.LIGHT_FIGHTER: 250},
    )

    assert rd._preflight_fleet_value_budget(EMAIL) == (150_000, 1_000_000)


def test_dynamic_fleet_value_budget_fails_closed_on_read_error(monkeypatch):
    def fail(_email):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(rd.db_reader, "get_account_ship_totals_db", fail)

    assert rd._preflight_fleet_value_budget(EMAIL) is None


def test_preflight_budget_read_error_blocks_and_consumes_pending(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )

    def fail(_email):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(rd.db_reader, "get_account_ship_totals_db", fail)

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    target = state["targets"]["1:50:7"]
    assert attacks == []
    assert "attack_preflight" not in target
    assert target["preflight_blocked"] == 10_000.0
    assert note == "raid PREFLIGHT HOLD (계정 함대가치 조회 실패)"


def test_preflight_blocks_fleet_value_over_dynamic_budget(monkeypatch):
    state = _pending_state()
    report = _report(report_id=11, created_at_ts=9950.0, res_sum=300_000)
    attacks, _spies, _saved = _patch_tick(monkeypatch, report, state)
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    # 계정 함대가치 800k → 동적 한도 120k, 편대는 136k.
    monkeypatch.setattr(
        rd.db_reader, "get_account_ship_totals_db",
        lambda _email: {rd.LIGHT_FIGHTER: 200},
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels(small_cargo=100))

    assert attacks == []
    assert note and "함대가치 136000>동적한도120000" in note


def test_preflight_allows_fleet_above_old_100k_under_dynamic_budget(monkeypatch):
    state = _pending_state()
    report = _report(report_id=11, created_at_ts=9950.0, res_sum=300_000)
    attacks, _spies, _saved = _patch_tick(monkeypatch, report, state)
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **_kwargs: {
            "recommend": True,
            "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0},
            "ev": 10_000,
        },
    )
    monkeypatch.setattr(rd.battle_guard, "record_prediction", lambda *_a, **_kw: None)

    note = rd._raid_tick_inner(object(), EMAIL, _levels(small_cargo=100))

    assert attacks and rd.battle_sim.fleet_cost_value(attacks[0][1]) == 136_000
    assert note and "함대가치136000/동적한도150000" in note


def test_preflight_dynamic_budget_boundary_is_inclusive(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )
    exact_limit = {rd.SMALL_CARGO: 35, rd.HEAVY_FIGHTER: 1}  # 150k
    monkeypatch.setattr(rd, "_attack_force", lambda *_args: exact_limit)
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **_kwargs: {
            "recommend": True, "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0}, "ev": 1,
        },
    )
    monkeypatch.setattr(rd.battle_guard, "record_prediction", lambda *_a, **_kw: None)

    note = rd._raid_tick_inner(
        object(), EMAIL,
        {**_levels(small_cargo=35), rd.HEAVY_FIGHTER: 1},
    )

    assert attacks and rd.battle_sim.fleet_cost_value(attacks[0][1]) == 150_000
    assert note and "함대가치150000/동적한도150000" in note


def test_preflight_dynamic_budget_limit_plus_one_is_blocked(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )
    monkeypatch.setattr(
        rd, "_attack_force",
        lambda *_args: {rd.SMALL_CARGO: 35, rd.HEAVY_FIGHTER: 1},
    )
    monkeypatch.setattr(
        rd, "_preflight_fleet_value_budget", lambda _email: (149_999, 999_999),
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert note and "함대가치 150000>동적한도149999" in note


def test_account_fleet_valuation_supports_every_registered_ship():
    assert set(rd.SHIPS) <= rd.battle_sim.supported_unit_ids()


def test_preflight_blocks_eta_over_75_seconds(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 76)

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert note and "ETA 76>75s" in note


def test_eta_replicates_server_formula_conservatively():
    # 동일 시스템 2칸, 연소6 LF+기본 SC, war speed x100.
    # SC가 최저 8,000이고 서버 round 대신 ceil이라 보수적 40초.
    assert rd._attack_eta_seconds(
        OWN,
        COORD,
        {rd.LIGHT_FIGHTER: 4, rd.SMALL_CARGO: 3},
        {115: 6, 117: 4},
        fleet_speed_war=100,
    ) == 40


def test_history_peak_envelope_blocks_lazy_settlement_return_risk(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(
        rd, "read_spy_combat_peak", lambda _email, _coord: {rd.LIGHT_FIGHTER: 522}
    )
    defenders: list[dict[int, int]] = []

    def reject_peak(**kwargs):
        defenders.append(dict(kwargs["defender"]))
        return {"recommend": False, "reason": "peak fleet", "sim": {}, "ev": -1}

    monkeypatch.setattr(rd.battle_sim, "decide", reject_peak)

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert defenders == [{rd.LIGHT_FIGHTER: 522}]
    assert note and "24h peak stress" in note


def test_safe_new_report_under_caps_attacks_and_records_new_prediction(monkeypatch):
    state = _pending_state()
    attacks, _spies, _saved = _patch_tick(
        monkeypatch, _report(report_id=11, created_at_ts=9950.0), state
    )
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    decision = {
        "recommend": True,
        "reason": "safe",
        "sim": {"win_prob": 1.0, "attacker_loss_value": 0},
        "ev": 10_000,
    }
    decide_calls: list[dict] = []
    monkeypatch.setattr(
        rd.battle_sim, "decide",
        lambda **kwargs: decide_calls.append(kwargs) or decision,
    )
    predictions: list[tuple] = []
    monkeypatch.setattr(
        rd.battle_guard, "record_prediction",
        lambda *args, **kwargs: predictions.append((args, kwargs)),
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert len(attacks) == 1 and attacks[0][0] == COORD
    assert decide_calls and decide_calls[0]["defender"] == {}
    assert predictions and predictions[0][0][3] is decision
    assert predictions[0][1]["mode"] == "log"
    assert predictions[0][1]["cohort"] == rd.PREFLIGHT_BATTLE_GUARD_COHORT
    assert "attack_preflight" not in state["targets"]["1:50:7"]
    assert note and "ATTACK[PREFLIGHT]" in note and "ETA30s" in note


def test_dispatch_revalidation_rejects_newer_report_snapshot(monkeypatch):
    state = _pending_state()
    evaluated = _report(report_id=11, created_at_ts=9950.0)
    newer = _report(report_id=12, created_at_ts=9960.0)
    attacks, _spies, _saved = _patch_tick(monkeypatch, evaluated, state)
    reports = iter(({COORD: evaluated}, {COORD: newer}))
    monkeypatch.setattr(rd, "read_spy_reports", lambda _email: next(reports))
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(
        rd.battle_sim,
        "decide",
        lambda **_kwargs: {
            "recommend": True, "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0}, "ev": 1,
        },
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert note and "발사 직전 report 변경" in note
    assert "attack_preflight" not in state["targets"]["1:50:7"]


def test_dispatch_revalidation_rejects_changed_24h_peak(monkeypatch):
    state = _pending_state()
    report = _report(report_id=11, created_at_ts=9950.0)
    attacks, _spies, _saved = _patch_tick(monkeypatch, report, state)
    peaks = iter(({}, {rd.LIGHT_FIGHTER: 1}))
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: next(peaks))
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(
        rd.battle_sim,
        "decide",
        lambda **_kwargs: {
            "recommend": True, "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0}, "ev": 1,
        },
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert note and "발사 직전 24h envelope 변경" in note


def test_dispatch_revalidation_rejects_newly_occupied_slot(monkeypatch):
    state = _pending_state()
    report = _report(report_id=11, created_at_ts=9950.0)
    attacks, _spies, _saved = _patch_tick(monkeypatch, report, state)
    fleet_states = iter(((0, False, 0), (3, True, 0)))
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: next(fleet_states))
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(
        rd.battle_sim,
        "decide",
        lambda **_kwargs: {
            "recommend": True, "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0}, "ev": 1,
        },
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == []
    assert note and "발사 직전 함대슬롯 부족" in note


def test_existing_attack_does_not_block_another_when_reserved_cap_has_room(monkeypatch):
    state = _pending_state()
    report = _report(report_id=11, created_at_ts=9950.0)
    attacks, _spies, _saved = _patch_tick(monkeypatch, report, state)
    monkeypatch.setattr(rd, "_active_fleet_info", lambda _client: (1, True, 0))
    monkeypatch.setattr(rd, "read_spy_combat_peak", lambda _email, _coord: {})
    monkeypatch.setattr(rd, "_attack_eta_seconds", lambda *_args, **_kwargs: 30)
    monkeypatch.setattr(
        rd.battle_sim,
        "decide",
        lambda **_kwargs: {
            "recommend": True,
            "reason": "safe",
            "sim": {"win_prob": 1.0, "attacker_loss_value": 0},
            "ev": 1,
        },
    )

    note = rd._raid_tick_inner(
        object(), EMAIL, _levels(), max_active_missions=3
    )

    assert len(attacks) == 1
    assert note and "ATTACK[PREFLIGHT]" in note


def test_dispatch_fleet_availability_is_exact():
    ships = {rd.LIGHT_FIGHTER: 4, rd.SMALL_CARGO: 3}
    assert rd._fleet_available(ships, {rd.LIGHT_FIGHTER: 4, rd.SMALL_CARGO: 3})
    assert not rd._fleet_available(ships, {rd.LIGHT_FIGHTER: 4, rd.SMALL_CARGO: 2})


def test_shadow_budget_skip_does_not_block_real_preflight(monkeypatch):
    state = {"targets": {}}
    attacks, spies, _saved = _patch_tick(monkeypatch, _report(), state)
    monkeypatch.setattr(rd, "_shadow_budget_allows", lambda *_args: False)
    monkeypatch.setattr(
        rd,
        "_battle_sim_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("budgeted-out shadow must not run")
        ),
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert attacks == [] and spies == [(COORD, 7)]
    assert note and "PREFLIGHT SPY" in note


def test_shadow_budget_admits_observation_without_changing_preflight(monkeypatch):
    state = {"targets": {}}
    attacks, spies, _saved = _patch_tick(monkeypatch, _report(), state)
    calls = []
    monkeypatch.setattr(rd, "_shadow_budget_allows", lambda *_args: True)
    monkeypatch.setattr(
        rd,
        "_battle_sim_shadow",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"log": "shadow-sample"},
    )

    note = rd._raid_tick_inner(object(), EMAIL, _levels())

    assert len(calls) == 1
    assert attacks == [] and spies == [(COORD, 7)]
    assert note and "shadow-sample" in note and "PREFLIGHT SPY" in note


def test_spy_only_policy_never_spends_shadow_cpu(monkeypatch):
    state = {"targets": {}}
    attacks, _spies, _saved = _patch_tick(monkeypatch, _report(), state)
    monkeypatch.setattr(rd, "_shadow_budget_allows", lambda *_args: True)
    monkeypatch.setattr(
        rd,
        "_battle_sim_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("allow_attack=False must skip battle shadow")
        ),
    )

    rd._raid_tick_inner(object(), EMAIL, _levels(), allow_attack=False)

    assert attacks == []


def test_preflight_attack_without_spy_permission_never_spends_shadow_cpu(monkeypatch):
    state = {"targets": {}}
    attacks, _spies, _saved = _patch_tick(monkeypatch, _report(), state)
    monkeypatch.setattr(rd, "_shadow_budget_allows", lambda *_args: True)
    monkeypatch.setattr(
        rd,
        "_battle_sim_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preflight cannot advance when allow_spy=False")
        ),
    )

    rd._raid_tick_inner(object(), EMAIL, _levels(), allow_spy=False)

    assert attacks == []


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_history_peak_reads_only_attackers_legal_spy_messages(monkeypatch):
    cursor = _Cursor([
        {"ships": '{"light_fighter":12,"small_cargo":20}'},
        {"ships": '{"light_fighter":522,"heavy_fighter":5}'},
        {"ships": None},
    ])
    monkeypatch.setattr(rd.db_reader, "_conn", lambda: _Conn(cursor))

    peak = rd.read_spy_combat_peak(EMAIL, COORD)

    assert peak == {rd.LIGHT_FIGHTER: 522, rd.HEAVY_FIGHTER: 5}
    assert "messages" in cursor.sql and "espionage_reports" in cursor.sql
    assert "planets" not in cursor.sql and "fleet_missions" not in cursor.sql
    assert cursor.params == (EMAIL, *COORD)
