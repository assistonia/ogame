"""LLM full-autonomy 행동 명세·집행 배선 회귀 테스트."""
from __future__ import annotations

import json
import pathlib

from bots.ai_agents import action_catalog
from bots.ai_agents import empire_snapshot
from bots.ai_agents import policy_engine as pe
from bots.rule_based import post_actions
from bots.rule_based import raider


def test_structured_colony_development_persists_queued_receipt_after_restore(
    monkeypatch,
):
    persisted: list[dict] = []
    switches: list[int] = []
    monkeypatch.setattr(pe, "action_enabled", lambda *_args: True)
    monkeypatch.setattr(pe.planets_mod, "current_planet_id", lambda _client: 1)
    monkeypatch.setattr(
        pe.planets_mod,
        "switch_planet",
        lambda _client, planet_id: switches.append(planet_id) or True,
    )
    monkeypatch.setattr(
        post_actions,
        "_recent_command_status",
        lambda _email, _command_id: (None, True),
    )
    monkeypatch.setattr(
        pe.colony_dev,
        "_build_on_colony",
        lambda *a, **k: {
            "ok": True,
            "status": "executed",
            "reason": "development_actions_queued",
            "actions": ["build solar 8"],
            "energy": -100,
        },
    )
    monkeypatch.setattr(
        post_actions,
        "_persist_development_outcome",
        lambda _email, outcome: persisted.append(dict(outcome)) or True,
    )
    policy = {
        "plans": {
            "production": [],
            "development": [{
                "command_id": "development:42:solar:8",
                "planet_id": 42,
                "minimum_floors": {},
                "role": "solar",
            }],
        },
    }
    empire_state = {
        "status": "ready",
        "planet_count": 2,
        "planets": [{
            "planet_id": 42,
            "coordinates": {"galaxy": 1, "system": 2, "position": 3},
        }],
    }
    outcomes: list[dict] = []

    notes, restored = pe._execute_structured_planet_plans(
        object(),
        "ai01@bots.local",
        policy,
        "miner",
        5,
        empire_state,
        set(),
        outcomes,
    )

    assert notes == ["build solar 8"]
    assert restored is True
    assert switches == [42, 1]
    assert len(persisted) == 1
    receipt = persisted[0]
    assert receipt["command_id"] == "development:42:solar:8"
    assert receipt["status"] == receipt["lifecycle"] == "queued"
    assert receipt["status"] != "completed"
    assert receipt["planet_id"] == 42
    assert receipt["coordinates"] == "1:2:3"
    assert receipt["restore_ok"] is True
    assert receipt["restored_planet_id"] == 1


def test_known_ai_defaults_to_full_autonomy_and_unclamped_ship_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "ai01.json").write_text(json.dumps({
        "personality": "raider",
        "ship_priority": [{"id": 204, "batch": 75}],
        "activation_id": "activation-runtime-link",
    }))

    policy = pe.load_policy("ai01@bots.local")

    assert policy["autonomy"] == "full"
    assert policy["activation_id"] == "activation-runtime-link"
    assert policy["ship_priority"] == []
    assert policy["action_plan"] is None
    assert policy["targeting"] == {
        "allow_strong_targets": True,
        "allow_defended_targets": True,
        "excluded_systems": [],
        "priority_targets": [],
    }
    assert policy["limits"]["spy_global_cooldown_sec"] == 600
    assert policy["limits"]["spy_target_cooldown_sec"] == 1200
    assert policy["limits"]["attack_target_cooldown_sec"] == 1500
    assert policy["limits"]["bashing_max_attacks"] == 6
    assert policy["limits"]["fleet_risk_percent"] == 100
    assert policy["limits"]["harvest_max_concurrent"] == 0
    assert policy["limits"]["harvest_cooldown_sec"] == 0
    assert policy["limits"]["harvest_reserved_raid_slots"] == 0
    assert policy["limits"]["max_active_fleetsaves"] == 0
    assert policy["limits"]["colonize_cooldown_sec"] == 0
    assert action_catalog.effective_action_plan(policy) == [
        name
        for name in action_catalog.FULL_ACTION_PLAN
        if name not in action_catalog.AI_STRUCTURED_ACTIONS
    ]


def test_full_explicit_plan_keeps_adaptive_defense_and_fleetsave_available():
    policy = {
        "autonomy": "full",
        "action_plan": ["build", "develop_colonies"],
        "defense_strategy": {"mode": "adaptive"},
        "fleetsave": {"mode": "adaptive"},
    }

    assert action_catalog.effective_action_plan(policy) == [
        "build", "develop_colonies", "defense", "fleetsave",
    ]

    policy["defense_strategy"]["mode"] = "off"
    policy["fleetsave"]["mode"] = "off"
    assert action_catalog.effective_action_plan(policy) == [
        "build", "develop_colonies",
    ]


def test_counter_fleetsave_mode_survives_policy_sanitizer(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "test.json").write_text(json.dumps({
        "strategy_version": 2,
        "fleetsave": {"mode": "counter", "min_value": 12345},
    }))

    policy = pe.load_policy("test@local")

    assert policy["fleetsave"] == {"mode": "counter", "min_value": 12345}
    assert "fleetsave" in action_catalog.effective_action_plan({
        "autonomy": "full",
        "action_plan": [],
        "fleetsave": policy["fleetsave"],
    })


def test_llm_policy_prompt_exposes_counter_fleetsave_contract():
    tick = (
        pathlib.Path(__file__).resolve().parents[1]
        / "bots" / "ai_agents" / "tick.sh"
    ).read_text()

    assert r'\"mode\": \"off|adaptive|counter|always\"' in tick
    assert "counter는" in tick
    assert "리사이클러를 ETA 뒤 자기 데브리" in tick


def test_llm_policy_prompt_has_non_rigid_empire_manager_charter():
    tick = (
        pathlib.Path(__file__).resolve().parents[1]
        / "bots" / "ai_agents" / "tick.sh"
    ).read_text()

    assert "독립 우주제국의 총관리자" in tick
    assert "제국의 장기 순성장과 생존" in tick
    assert "전 식민지를" in tick and "하나의 제국으로 관리" in tick
    assert "식민·경제·연구·함대·방어·정찰·약탈·원정·양측 데브리 회수" in tick
    assert "함선 손실·연료·화물 적재·슬롯" in tick
    assert "도피·방어 매복·전후 데브리 회수·재건" in tick
    assert "무조건 공격하라는 명령이 아니다" in tick


def test_llm_policy_prompt_has_expedition_growth_and_allocation_contract():
    root = pathlib.Path(__file__).resolve().parents[1]
    tick = (root / "bots" / "ai_agents" / "tick.sh").read_text()

    assert "원정 횟수가 아니라 계정의 지속 가능한 순성장이 최우선" in tick
    assert "use_server_expedition_slots=true" in tick
    assert "서버 원정 슬롯" in tick and "한도까지 사용" in tick
    assert "동시 원정 슬롯당 파스파인더(PF) 1기" in tick
    assert "가용 화물선" in tick and "남은 위험예산" in tick
    assert "미사용 슬롯에 균등하게 나눈다" in tick
    assert "전투함은 필요한 함종 티어" in tick
    assert "최소 호위를 넘어 과적하지 마라" in tick
    assert "현재 화물선을 나누는 것으로 끝내지 마라" in tick
    assert "전투함 증산은 멈추지 않는다" in tick
    assert "무손실 표적이 되지 않게 한다" in tick
    assert "함대는 행성을 지키지 못하므로 상시 방어시설" in tick
    assert "원정 슬롯 목표를 먼저 채운 뒤에도 일반 함대 슬롯이 남으면" in tick
    assert "100은 무조건 전량 투입 명령이 아니라 최대 허용치" in tick


def test_full_catalog_schedules_expedition_before_leftover_raid_slots():
    plan = list(action_catalog.FULL_ACTION_PLAN)

    assert plan.index("expedition") < plan.index("spy")
    assert plan.index("expedition") < plan.index("raid")
    assert plan.index("expedition") < plan.index("harvest")


def test_explorer_personas_repeat_expedition_slot_contract():
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("ai04.md", "explorer01.md"):
        persona = (root / "bots" / "ai_agents" / "personas" / name).read_text()
        assert "순성장이 최우선" in persona
        assert "서버 원정 슬롯" in persona and "한도까지" in persona
        assert "PF 1기" in persona
        assert "남은 슬롯에 균등하게 나눈다" in persona
        assert "최소 호위" in persona and "전투함" in persona
        assert "계속" in persona and "증산" in persona
        assert "남는 일반 함대 슬롯" in persona

    pirate = (
        root / "bots" / "ai_agents" / "personas" / "pirate01.md"
    ).read_text()
    assert "원정 슬롯을 기본 수익으로 먼저 채우고" in pirate
    assert "남는 일반 함대 슬롯" in pirate
    assert "함께 증산" in pirate


def test_strategy_v2_can_choose_manual_batch_and_dynamic_doctrine(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "ai01.json").write_text(json.dumps({
        "strategy_version": 2,
        "personality": "raider",
        "ship_priority": [{"id": 204, "batch": 75}],
        "ship_strategy": {
            "reinvestment_ratio": 0.5,
            "doctrine": "debris_hunter",
        },
    }))

    policy = pe.load_policy("ai01@bots.local")

    assert policy["ship_priority"] == [{"id": 204, "batch": 75}]
    assert policy["ship_strategy"]["reinvestment_ratio"] == 0.5
    assert policy["ship_strategy"]["doctrine"] == "debris_hunter"


def test_guided_policy_preserves_legacy_batch_cap_and_explicit_action_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "test.json").write_text(json.dumps({
        "autonomy": "guided",
        "action_plan": ["build", "spy", "unknown", "spy"],
        "ship_priority": [{"id": 204, "batch": 75}],
    }))

    policy = pe.load_policy("test@local")

    assert policy["ship_priority"][0]["batch"] == pe.MAX_SHIP_BATCH
    assert policy["action_plan"] == ["build", "spy"]
    assert policy["targeting"] == {}


def test_full_action_plan_dispatches_all_identity_actions_in_declared_order(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(post_actions, "ensure_ai_character_class", lambda *a, **k: None)
    monkeypatch.setattr(
        post_actions.raider,
        "raid_tick",
        lambda *a, **k: calls.append(("raid", k)) or "RAID",
    )
    monkeypatch.setattr(
        post_actions.explorer,
        "expedition_tick",
        lambda *a, **k: calls.append(("expedition", k)) or "EXP",
    )
    monkeypatch.setattr(
        post_actions.colony,
        "colony_tick",
        lambda *a, **k: calls.append(("colonize", k)) or "COL",
    )
    monkeypatch.setattr(
        post_actions.turtle,
        "defense_tick",
        lambda *a, **k: calls.append(("defense", k)) or "DEF",
    )
    monkeypatch.setattr(
        post_actions.fleetsave,
        "fleetsave_tick",
        lambda *a, **k: calls.append(("fleetsave", k)) or "SAVE",
    )
    monkeypatch.setattr(
        post_actions.colony_dev,
        "colony_dev_tick",
        lambda *a, **k: calls.append(("develop", k)) or "DEV",
    )
    monkeypatch.setattr(
        post_actions.metal_sink_observer,
        "observe_metal_sink_shadow",
        lambda *a, **k: None,
    )
    policy = {
        "autonomy": "full",
        "action_plan": [
            "spy", "raid", "harvest", "expedition", "colonize",
            "defense", "fleetsave", "develop_colonies",
        ],
        "limits": {"max_planets": 7, "use_server_expedition_slots": True},
        "targeting": {"allow_strong_targets": True, "excluded_systems": []},
    }

    notes = post_actions.run_post_actions(
        object(), "ai03@bots.local", {108: 5}, "miner", 9,
        energy=10, policy=policy,
    )

    assert notes == ["RAID", "EXP", "COL", "DEF", "SAVE", "DEV"]
    assert [name for name, _ in calls] == [
        "raid", "expedition", "colonize", "defense", "fleetsave", "develop"
    ]
    raid_kwargs = calls[0][1]
    assert raid_kwargs["allow_spy"] is True
    assert raid_kwargs["allow_attack"] is True
    assert raid_kwargs["allow_harvest"] is True
    assert raid_kwargs["max_active_missions"] == 6
    assert raid_kwargs["controls"]["targeting"]["allow_strong_targets"] is True
    assert calls[2][1]["max_planets"] == 7
    assert calls[2][1]["enforce_strategy_cap"] is False


def test_full_targeting_includes_strong_player_and_zero_cannot_disable_bashing_guard():
    row = {
        "galaxy": 1,
        "system": 39,
        "position": 5,
        "player_id": 777,
        "strong": True,
    }
    assert raider._is_biddable(row)
    assert not raider._is_biddable(row, allow_strong_targets=False)
    assert raider._is_biddable(
        row, allow_strong_targets=True, excluded_systems=set()
    )
    state = {"targets": {"1:39:5": {"attacks": [1, 2, 3, 4, 5, 6]}}}
    assert raider._bashing_limited(state, (1, 39, 5), 10, max_attacks=6)
    assert raider._bashing_limited(state, (1, 39, 5), 10, max_attacks=0)


def test_registered_ai_clamps_unsafe_limits_and_ignores_static_priority_targets(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "ai06.json").write_text(json.dumps({
        "strategy_version": 3,
        "personality": "raider",
        "ship_strategy": {"doctrine": "expeditioneer"},
        "targeting": {"priority_targets": ["1:39:5", "1:39:8"]},
        "limits": {
            "spy_global_cooldown_sec": 0,
            "spy_target_cooldown_sec": 0,
            "attack_target_cooldown_sec": 0,
            "bashing_max_attacks": 0,
        },
    }))

    policy = pe.load_policy("ai06@bots.local")

    assert policy["limits"]["spy_global_cooldown_sec"] == 600
    assert policy["limits"]["spy_target_cooldown_sec"] == 1200
    assert policy["limits"]["attack_target_cooldown_sec"] == 1500
    assert policy["limits"]["bashing_max_attacks"] == 1
    assert policy["targeting"]["priority_targets"] == []
    assert policy["ship_strategy"]["doctrine"] in {
        "cargo_raider", "debris_hunter", "balanced",
    }


def test_bashing_rolls_up_all_planets_owned_by_same_player():
    state = {"targets": {}, "players": {"777": {"attacks": [1, 2, 3, 4, 5, 6]}}}

    assert raider._bashing_limited(
        state, (1, 42, 8), 10, max_attacks=6, player_id=777
    )
    assert not raider._bashing_limited(
        state, (1, 42, 8), 10, max_attacks=6, player_id=778
    )


def test_priority_targets_add_only_their_systems_and_rotate_by_last_spy(monkeypatch):
    scanned: list[int] = []

    def fake_scan(_client, galaxy, system):
        scanned.append(system)
        rows = {
            39: [{
                "galaxy": galaxy, "system": 39, "position": 5,
                "player_id": 777, "strong": True,
            }],
            42: [{
                "galaxy": galaxy, "system": 42, "position": 8,
                "player_id": 777, "strong": True,
            }],
        }
        return rows.get(system, [])

    monkeypatch.setattr(raider, "cached_scan_galaxy", fake_scan)
    priority = {(1, 39, 5), (1, 42, 8)}
    candidates = raider.scan_targets(
        object(), 1, 10, (1, 10, 5), span=0,
        allow_strong_targets=True, excluded_systems=set(), priority_targets=priority,
    )
    assert scanned == [10, 39, 42]
    state = {
        "targets": {
            "1:39:5": {"spy": 200.0},
            "1:42:8": {"spy": 100.0},
        }
    }
    ordered = raider._prioritize_targets(candidates, state, priority)
    assert [row["coord"] for row in ordered] == [(1, 42, 8), (1, 39, 5)]


def test_defended_target_uses_dynamic_full_fleet_sizing():
    levels = {
        raider.LIGHT_FIGHTER: 1000,
        raider.LARGE_CARGO: 100,
        109: 10,
        110: 10,
        111: 10,
    }
    result = raider._autonomous_sized_attack(
        {401: 100},
        {109: 0, 110: 0, 111: 0},
        1_000_000,
        levels,
        have_rec=0,
        exposure_percent=100,
    )
    assert result is not None
    fleet, decision = result
    assert fleet.get(raider.LIGHT_FIGHTER, 0) > raider.ATTACK_FIGHTERS
    assert fleet.get(raider.LARGE_CARGO, 0) > 0
    assert decision["recommend"] is True


def test_empire_snapshot_lists_every_planet_and_production_factor(monkeypatch):
    monkeypatch.setattr(
        empire_snapshot.empire_scheduler,
        "build_normalized_empire_state",
        lambda _email: {
            "planet_count": 2,
            "moon_count": 0,
            "world_count": 2,
            "canonical_home_id": 1,
            "account": {
                "ship_totals": {},
                "missions": {"status": "ready", "by_type": {}},
                "fleet_slots": {
                    "available": True, "active": 0, "maximum": 8, "free": 8,
                },
                "research_levels": {"astrophysics": 7},
                "production_per_hour": {
                    "metal": 0, "crystal": 0, "deuterium": 0,
                },
            },
            "planets": [
                {
                    "planet_id": planet_id,
                    "name": name,
                    "coordinates": {"label": coord},
                    "is_current": planet_id == 1,
                    "is_canonical_home": planet_id == 1,
                    "core_status": "ready",
                    "relevant_levels": {
                        "metal_mine": 20 + planet_id,
                        "crystal_mine": 15,
                        "deuterium_synthesizer": 10,
                        "solar_plant": 12,
                        "shipyard": 4,
                    },
                    "resources": {"metal": 1, "crystal": 2, "deuterium": 3},
                    "energy": {
                        "balance": -100 * planet_id,
                        "maximum": 800,
                        "used": 1000,
                    },
                    "temperature": {},
                    "production_per_hour": {
                        "metal": 0, "crystal": 0, "deuterium": 0,
                    },
                    "defense": {},
                    "ships": {},
                }
                for planet_id, name, coord in (
                    (1, "Home", "1:10:5"),
                    (2, "Colony", "1:11:6"),
                )
            ],
            "moons": [],
        },
    )

    snapshot = empire_snapshot.build_empire_snapshot("ai01@bots.local")

    assert snapshot["status"] == "ready"
    assert snapshot["planet_count"] == 2
    assert snapshot["astro_level"] == 7
    assert [row["coord"] for row in snapshot["planets"]] == ["1:10:5", "1:11:6"]
    assert snapshot["planets"][1]["production_factor_pct"] == 80
