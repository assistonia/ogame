"""규칙봇/AI 공용 후행 파이프라인의 순서·인자 단위검증."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import post_actions


def test_raider_identity_then_two_planet_colony_then_fleetsave_then_colonydev(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        post_actions.raider,
        "raid_tick",
        lambda *a, **kw: calls.append(("raid", kw)) or "RAID",
    )
    monkeypatch.setattr(
        post_actions.colony,
        "colony_tick",
        lambda *a, **kw: calls.append(("colony", kw)) or "COLONY",
    )
    monkeypatch.setattr(
        post_actions.fleetsave,
        "fleetsave_tick",
        lambda *a, **kw: calls.append(("save", a[3], a[4])) or "SAVE",
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **kw: calls.append(("colonydev", a[2], a[3])) or "COLONYDEV",
    )

    notes = post_actions.run_post_actions(
        object(), "bot@test", {204: 5}, "raider", 77, energy=123,
        raid_options={"min_loot": 9000, "max_active_missions": 2},
    )
    assert notes == ["RAID", "COLONY", "SAVE", "COLONYDEV"]
    assert [c[0] for c in calls] == ["raid", "colony", "save", "colonydev"]
    assert calls[0][1]["min_loot"] == 9000
    assert calls[0][1]["max_active_missions"] == 1
    assert calls[0][1]["controls"]["limits"]["fleet_risk_percent"] == 100
    assert calls[0][1]["controls"]["targeting"]["allow_defended_targets"] is True
    assert calls[1][1] == {
        "max_planets": 99,
        "reserve_fleet_slots": 0,
        "enforce_strategy_cap": False,
    }


def test_rule_path_dispatches_expedition_then_raids_only_with_surplus(monkeypatch):
    calls: list[tuple] = []

    def expedition(_client, _email, _levels, **kwargs):
        kwargs["dispatch_state"].update({"sent": True, "ships": {203: 20}})
        calls.append(("expedition", dict(_levels)))
        return "EXP"

    monkeypatch.setattr(post_actions.explorer, "expedition_tick", expedition)
    monkeypatch.setattr(
        post_actions.explorer,
        "expedition_raid_reservation",
        lambda *a, **k: (2, {203: 30}),
    )
    monkeypatch.setattr(
        post_actions.raider,
        "raid_tick",
        lambda _c, _e, lv, **kw: calls.append(("raid", dict(lv), kw)) or "RAID",
    )
    monkeypatch.setattr(post_actions.colony, "colony_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.miner_defense, "base_defense_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )

    notes = post_actions.run_post_actions(
        object(), "bot004@bots.local", {108: 5, 203: 100}, "explorer", 1,
        energy=0,
    )

    assert notes == ["EXP", "RAID"]
    assert [call[0] for call in calls] == ["expedition", "raid"]
    assert calls[1][1][203] == 50  # 100 - 방금 출발20 - 남은 원정 예약30
    assert calls[1][2]["max_active_missions"] == 4  # 일반6 - 원정예약2


def test_policy_order_is_kept_but_future_expedition_fleet_is_reserved(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        post_actions.explorer,
        "expedition_raid_reservation",
        lambda *a, **k: (1, {203: 20}),
    )
    monkeypatch.setattr(
        post_actions.raider,
        "raid_tick",
        lambda _c, _e, lv, **kw: calls.append(("raid", dict(lv), kw)) or "RAID",
    )
    monkeypatch.setattr(
        post_actions.explorer,
        "expedition_tick",
        lambda _c, _e, lv, **kw: calls.append(("expedition", dict(lv))) or "EXP",
    )
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )

    notes = post_actions._run_policy_actions(
        object(), "ai04@bots.local", {108: 3, 203: 100}, "explorer", 1,
        energy=0,
        raid_options={},
        defense_ratio=None,
        policy={
            "autonomy": "full",
            "limits": {"use_server_expedition_slots": True},
            "targeting": {"allow_defended_targets": True},
        },
        action_plan=["raid", "expedition"],
    )

    assert notes == ["RAID", "EXP"]
    assert [call[0] for call in calls] == ["raid", "expedition"]
    assert calls[0][1][203] == 80
    assert calls[0][2]["max_active_missions"] == 3  # 일반4 - 원정예약1


def test_policy_raider_expedition_uses_personality_key_for_cargo_exposure(monkeypatch):
    """정책형 raider 원정은 인자 이름 오류 없이 화물선 노출 비율을 적용해야 한다."""
    captured: dict[int, int] = {}
    monkeypatch.setattr(
        post_actions,
        "_expedition_can_preserve_slot",
        lambda *a, **k: True,
    )

    def expedition(_client, _email, levels, **_kwargs):
        captured.update(levels)
        return "EXP"

    monkeypatch.setattr(post_actions.explorer, "expedition_tick", expedition)

    notes = post_actions._run_policy_actions(
        object(),
        "ai02@bots.local",
        {202: 100, 203: 50},
        "raider",
        1,
        energy=0,
        raid_options={},
        defense_ratio=None,
        policy={"autonomy": "full"},
        action_plan=["expedition"],
    )

    assert notes == ["EXP"]
    assert captured[202] == 60
    assert captured[203] == 30


def test_full_policy_forwards_colony_and_fleetsave_limits(monkeypatch):
    calls: dict[str, dict] = {}
    monkeypatch.setattr(
        post_actions.colony,
        "colony_tick",
        lambda *a, **kw: calls.setdefault("colony", kw) and None,
    )
    monkeypatch.setattr(
        post_actions.fleetsave,
        "fleetsave_tick",
        lambda *a, **kw: calls.setdefault("fleetsave", kw) and None,
    )
    monkeypatch.setattr(post_actions.explorer, "fleet_slots", lambda *_args: 9)
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )

    post_actions._run_policy_actions(
        object(), "ai04@bots.local", {108: 6}, "explorer", 1,
        energy=0,
        raid_options={},
        defense_ratio=None,
        policy={
            "autonomy": "full",
            "limits": {
                "colony_scan_span": 12,
                "colonize_cooldown_sec": 0,
                "max_active_fleetsaves": 4,
            },
            "fleetsave": {"mode": "always", "min_value": 1},
        },
        action_plan=["colonize", "fleetsave"],
    )

    assert calls["colony"]["scan_span"] == 12
    assert calls["colony"]["cooldown_sec"] == 0
    assert calls["fleetsave"]["max_active_fleetsaves"] == 4
    assert calls["fleetsave"]["fleet_slot_cap"] == 9


def test_full_policy_without_fleetsave_limit_has_no_artificial_count_cap(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        post_actions.fleetsave,
        "fleetsave_tick",
        lambda *a, **kw: seen.update(kw) or None,
    )
    monkeypatch.setattr(post_actions.explorer, "fleet_slots", lambda *_args: 7)
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )

    post_actions._run_policy_actions(
        object(), "ai01@bots.local", {}, "raider", 1,
        energy=0,
        raid_options={},
        defense_ratio=None,
        policy={"autonomy": "full"},
        action_plan=["fleetsave"],
    )

    assert seen["max_active_fleetsaves"] == 0
    assert seen["fleet_slot_cap"] == 7


def test_all_rule_accounts_keep_full_intel_with_hard_raid_safety_controls(monkeypatch):
    raid_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        post_actions.raider,
        "raid_tick",
        lambda _client, email, _levels, **kw: raid_calls.append((email, kw)) or None,
    )
    monkeypatch.setattr(post_actions.colony, "colony_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)

    post_actions.run_post_actions(
        object(), "ai06@bots.local", {}, "raider", 1, energy=0
    )
    post_actions.run_post_actions(
        object(), "ai01@bots.local", {}, "raider", 1, energy=0
    )
    post_actions.run_post_actions(
        object(), "bot002@bots.local", {}, "raider", 1, energy=0
    )

    assert all("battle_mode" not in call for _, call in raid_calls)
    assert all(
        call["controls"]["limits"]["bashing_max_attacks"]
        == post_actions.raider.BASH_MAX_ATTACKS
        for _, call in raid_calls
    )
    assert all(
        call["controls"]["limits"]["spy_global_cooldown_sec"]
        == post_actions.raider.SPY_GLOBAL_COOLDOWN_SEC
        for _, call in raid_calls
    )
    assert all(call["controls"]["targeting"]["allow_defended_targets"] for _, call in raid_calls)


def test_rule_explorer_uses_full_evenly_distributed_expedition_exposure(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(post_actions.raider, "raid_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.explorer,
        "expedition_tick",
        lambda *a, **kw: seen.update(kw) or None,
    )
    monkeypatch.setattr(post_actions.colony, "colony_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.miner_defense, "base_defense_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )

    post_actions.run_post_actions(
        object(), "bot004@bots.local", {}, "explorer", 1, energy=0
    )

    assert seen["use_server_cap"] is True
    assert seen["risk_fraction"] == 1.0
    assert seen["total_exposure_fraction"] == 1.0


def test_turtle_policy_override_and_energy_are_both_forwarded(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        post_actions.turtle,
        "defense_tick",
        lambda *a, **kw: seen.update(kw) or None,
    )
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: None)
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.colony,
        "colony_tick", lambda *a, **k: None,
    )

    post_actions.run_post_actions(
        object(), "ai@test", {}, "turtle", 1, energy=-50, defense_ratio=0.12
    )
    assert seen == {"energy": -50, "defense_ratio": 0.12}


def test_miner_still_runs_colony_before_save(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        post_actions.colony, "colony_tick", lambda *a, **k: order.append("colony") or None
    )
    monkeypatch.setattr(
        post_actions.fleetsave,
        "fleetsave_tick",
        lambda *a, **k: order.append("save") or None,
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: order.append("colonydev") or None,
    )
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: order.append("sink-shadow") or None,
    )
    post_actions.run_post_actions(object(), "bot@test", {}, "miner", 3, energy=1)
    assert order == ["colony", "save", "colonydev", "sink-shadow"]


def test_empty_structured_development_keeps_legacy_colony_rotation(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)
    monkeypatch.setattr(post_actions, "_colony_development_visits", lambda _email: 2)
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: (
            calls.append("structured" if k.get("structured") is True else "legacy")
            or "COLONYDEV"
        ),
    )

    notes = post_actions.run_post_actions(
        object(),
        "ai01@bots.local",
        {},
        "miner",
        3,
        energy=1,
        policy={
            "autonomy": "full",
            "action_plan": ["develop_colonies"],
        },
        plans={"development": []},
    )

    assert notes == ["COLONYDEV", "COLONYDEV"]
    assert calls == ["structured", "structured"]


def test_legacy_development_visit_persists_queued_receipt_with_restore_state(
    monkeypatch,
):
    persisted: list[dict] = []
    captured: list[dict] = []
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)
    monkeypatch.setattr(post_actions, "_colony_development_visits", lambda _email: 1)
    monkeypatch.setattr(
        post_actions.action_feedback,
        "append_action_outcome",
        lambda _email, outcome: persisted.append(dict(outcome)) or dict(outcome),
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: {
            "ok": True,
            "status": "executed",
            "reason": "development_actions_queued",
            "planet_id": 42,
            "coordinate": "1:2:3",
            "actions": ["build solar 8"],
            "note": "colonydev [1:2:3] build solar 8",
            "start_planet_id": 1,
            "home_planet_id": 1,
            "restored_planet_id": 1,
            "restore_ok": True,
        },
    )

    notes = post_actions.run_post_actions(
        object(),
        "ai01@bots.local",
        {},
        "miner",
        9,
        energy=-10,
        policy={"autonomy": "full", "action_plan": ["develop_colonies"]},
        plans={"development": []},
        outcomes=captured,
    )

    assert notes == ["colonydev [1:2:3] build solar 8"]
    assert len(captured) == len(persisted) == 1
    receipt = persisted[0]
    assert receipt["status"] == receipt["lifecycle"] == "queued"
    assert receipt["status"] != "completed"
    assert receipt["planet_id"] == 42
    assert receipt["coordinates"] == "1:2:3"
    assert receipt["actions"] == ["build solar 8"]
    assert receipt["restore_ok"] is True
    assert receipt["restored_planet_id"] == 1
    assert receipt["command_id"].startswith("legacy:development:")


def test_development_idle_and_rejection_are_visible_feedback(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setattr(
        post_actions.action_feedback,
        "append_action_outcome",
        lambda _email, outcome: persisted.append(dict(outcome)) or dict(outcome),
    )
    raw_outcomes = iter([
        {
            "ok": True,
            "status": "idle",
            "reason": "no_affordable_or_available_actions",
            "planet_id": 20,
            "coordinate": "1:1:2",
            "actions": [],
            "restore_ok": True,
        },
        {
            "ok": False,
            "status": "rejected",
            "reason": "planet_switch_failed",
            "planet_id": 21,
            "coordinate": "1:1:3",
            "actions": [],
            "restore_ok": True,
        },
    ])
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: next(raw_outcomes),
    )

    for visit_index in range(2):
        post_actions._run_legacy_development_visit(
            object(),
            "ai01@bots.local",
            "miner",
            10,
            visit_index,
            policy={"autonomy": "full"},
            outcomes=[],
        )

    assert [(row["status"], row["reason"]) for row in persisted] == [
        ("skipped", "no_affordable_or_available_actions"),
        ("failed", "planet_switch_failed"),
    ]


def test_structured_development_command_has_authority_and_is_not_reexecuted(
    monkeypatch,
):
    persisted: list[dict] = []
    calls: list[str] = []
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.action_feedback,
        "append_action_outcome",
        lambda _email, outcome: persisted.append(dict(outcome)) or dict(outcome),
    )
    monkeypatch.setattr(
        post_actions,
        "_recent_command_status",
        lambda _email, _command_id: (None, True),
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_outcome",
        lambda *a, **k: (
            calls.append("structured")
            or {
                "ok": True,
                "status": "executed",
                "reason": "development_actions_queued",
                "planet_id": 42,
                "coordinate": "1:2:3",
                "actions": ["build mine"],
                "restore_ok": True,
            }
        ),
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("legacy development must not run")
        ),
    )
    plan = {
        "command_id": "development:42:mine:7",
        "planet_id": 42,
        "role": "mining",
    }

    outcomes: list[dict] = []
    post_actions.run_post_actions(
        object(),
        "ai01@bots.local",
        {},
        "miner",
        11,
        energy=1,
        policy={"autonomy": "full", "action_plan": ["develop_colonies"]},
        plans={"development": [plan]},
        outcomes=outcomes,
    )
    monkeypatch.setattr(
        post_actions,
        "_recent_command_status",
        lambda _email, _command_id: ("queued", True),
    )
    post_actions.run_post_actions(
        object(),
        "ai01@bots.local",
        {},
        "miner",
        12,
        energy=1,
        policy={"autonomy": "full", "action_plan": ["develop_colonies"]},
        plans={"development": [plan]},
        outcomes=outcomes,
    )

    assert calls == ["structured"]
    assert [row["status"] for row in outcomes] == ["queued", "skipped"]
    assert all(row["command_id"] == plan["command_id"] for row in outcomes)
    assert [row["status"] for row in persisted] == ["queued"]


def test_nonempty_structured_development_replaces_legacy_rotation(monkeypatch):
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions,
        "_run_structured_development",
        lambda *a, **k: "STRUCTURED-DEV",
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("legacy colony development must not also run")
        ),
    )

    notes = post_actions.run_post_actions(
        object(),
        "ai01@bots.local",
        {},
        "miner",
        3,
        energy=1,
        policy={
            "autonomy": "full",
            "action_plan": ["develop_colonies"],
        },
        plans={"development": [{"role": "balanced"}]},
    )

    assert notes == ["STRUCTURED-DEV"]


def test_shadow_note_is_appended_last_without_executing_another_action(monkeypatch):
    monkeypatch.setattr(post_actions.colony, "colony_tick", lambda *a, **k: "COLONY")
    monkeypatch.setattr(post_actions.fleetsave, "fleetsave_tick", lambda *a, **k: "SAVE")
    monkeypatch.setattr(post_actions.colony_dev, "colony_dev_tick", lambda *a, **k: "DEV")
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: "metal-sink SHADOW HOLD unit-queue-busy",
    )
    notes = post_actions.run_post_actions(
        object(), "bot@test", {}, "miner", 3, energy=1
    )
    assert notes == ["COLONY", "SAVE", "DEV", "metal-sink SHADOW HOLD unit-queue-busy"]


def test_ai_character_class_waits_for_fleet_then_selects_once(monkeypatch):
    class _Cursor:
        def __init__(self, row):
            self.row = row
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, _sql, _params): pass
        def fetchone(self): return self.row
    class _Conn:
        def __init__(self, row): self.row = row
        def cursor(self): return _Cursor(self.row)
    class _Client:
        def __init__(self): self.selected = []
        def select_class(self, class_id):
            self.selected.append(class_id)
            return {"status": "success"}

    client = _Client()
    post_actions._CLASS_CONFIRMED.clear()
    monkeypatch.setattr(post_actions.db_reader, "_conn", lambda: _Conn({
        "character_class": None, "active": 1,
    }))
    assert post_actions.ensure_ai_character_class(client, "ai04@bots.local", "explorer") is None
    assert client.selected == []

    monkeypatch.setattr(post_actions.db_reader, "_conn", lambda: _Conn({
        "character_class": None, "active": 0,
    }))
    assert post_actions.ensure_ai_character_class(
        client, "ai04@bots.local", "explorer"
    ) == "class DISCOVERER"
    assert client.selected == [3]
    assert post_actions.ensure_ai_character_class(client, "ai04@bots.local", "explorer") is None
    assert client.selected == [3]

    assert post_actions.ensure_ai_character_class(
        client, "pirate01@bots.local", "raider"
    ) == "class GENERAL"
    assert client.selected == [3, 2]
    assert post_actions.ensure_ai_character_class(
        client, "pirate01@bots.local", "raider"
    ) is None
    assert client.selected == [3, 2]


def test_rule_accounts_do_not_query_character_class(monkeypatch):
    post_actions._CLASS_CONFIRMED.clear()
    monkeypatch.setattr(
        post_actions.db_reader, "_conn", lambda: (_ for _ in ()).throw(AssertionError())
    )
    assert post_actions.ensure_ai_character_class(
        object(), "bot004@bots.local", "explorer"
    ) is None
