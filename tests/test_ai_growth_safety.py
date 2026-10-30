"""AI 정책 개성과 공용 성장 안전망의 회귀 테스트."""
from __future__ import annotations

import json

from bots.ai_agents import policy_engine as pe


def test_expired_or_broken_policy_keeps_known_ai_personality(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)

    (tmp_path / "ai01.json").write_text(json.dumps({
        "updated_at": "2000-01-01T00:00:00+00:00",
        "ttl_hours": 1,
        "personality": "raider",
    }))
    assert pe.load_policy("ai01@bots.local")["personality"] == "raider"
    assert pe.load_policy("ai01@bots.local")["source"] == "expired"

    (tmp_path / "ai04.json").write_text("{broken")
    assert pe.load_policy("ai04@bots.local")["personality"] == "explorer"
    assert pe.load_policy("ai04@bots.local")["source"] == "broken"

    # 미등록/테스트 계정은 기존 안전 기본값 miner를 유지한다.
    assert pe.load_policy("ai99@bots.local")["personality"] == "miner"


def test_policy_without_personality_uses_account_persona(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)
    (tmp_path / "ai02.json").write_text(json.dumps({"build_priority": [1]}))
    loaded = pe.load_policy("ai02@bots.local")
    assert loaded["source"] == "policy"
    assert loaded["personality"] == "turtle"


def test_named_spark_agents_keep_persona_and_full_autonomy_without_policy(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pe, "POLICY_DIR", tmp_path)

    pirate = pe.load_policy("pirate01@bots.local")
    explorer = pe.load_policy("explorer01@bots.local")

    assert pirate["personality"] == "raider"
    assert explorer["personality"] == "explorer"
    assert pirate["autonomy"] == explorer["autonomy"] == "full"
    assert pirate["source"] == explorer["source"] == "no-file"


def test_scorecard_persona_lock_uses_canonical_ai_mapping_before_previous_policy():
    # ai10은 과거 정책이 hybrid로 drift했고 새 모델도 hybrid를 냈더라도 canonical raider로 복구한다.
    locked, target = pe.enforce_persona_lock(
        "ai10@bots.local",
        {"personality": "hybrid", "note": "new"},
        {"personality": "hybrid", "note": "previous drift"},
    )
    assert target == "raider"
    assert locked["personality"] == "raider"

    # 정상 canonical hybrid인 ai05는 그대로 유지한다(직전값이 달라도 canonical 우선).
    locked, target = pe.enforce_persona_lock(
        "ai05@bots.local",
        {"personality": "hybrid"},
        {"personality": "raider"},
    )
    assert target == "hybrid"
    assert locked["personality"] == "hybrid"


def test_scorecard_persona_lock_keeps_previous_valid_personality_for_unregistered_account():
    new_policy = {"personality": "miner", "note": "test account"}
    locked, target = pe.enforce_persona_lock(
        "test5@ogamex.dev", new_policy, {"personality": "explorer"}
    )
    assert target == "explorer"
    assert locked["personality"] == "explorer"
    # 공용 helper는 입력 dict를 mutate하지 않는다.
    assert new_policy["personality"] == "miner"


def test_build_priority_cannot_bypass_storage_facility_or_energy_safety():
    store = ("build", 23)
    facility = ("facility", 31)
    solar = ("build", 4)
    metal = ("build", 1)
    candidates = [store, facility, solar, metal]

    # 정책은 metal을 맨 앞으로 요구하지만 저장고/시설 진행은 보호된다.
    out = pe.apply_build_priority(
        candidates,
        [1, 4, 23],
        energy=500,
        protected_actions=[store, facility],
    )
    assert out == [store, facility, metal, solar]

    # 정책 energy_target 미달이면 저장고 뒤, 시설 앞에 태양광을 고정한다.
    out_energy = pe.apply_build_priority(
        candidates,
        [1],
        energy=0,
        energy_target=100,
        protected_actions=[store, facility],
    )
    assert out_energy == [store, solar, facility, metal]


def test_primary_safety_detects_proactive_solar_and_facility_head():
    # metal 10→11의 추가 소비가 E=1을 넘으므로 공용 에너지 계산은 선제 solar를 고른다.
    levels = {1: 10, 4: 10, 21: 0}
    candidates = [("build", 1), ("build", 4)]
    protected = pe._primary_safety_actions(
        candidates, levels, 1, {"temp_max": 40}
    )
    assert protected == [("build", 4)]

    facility_candidates = [("build", 22), ("facility", 31), ("build", 1)]
    protected_facility = pe._primary_safety_actions(
        facility_candidates, levels, 500, {"temp_max": 40}
    )
    assert protected_facility == [("build", 22), ("facility", 31)]


def test_research_policy_only_reorders_finite_shared_candidates():
    candidates = [("research", 113), ("research", 115)]
    out = pe.apply_research_priority(candidates, [109, 115], levels={31: 6})
    assert out == [("research", 115), ("research", 113)]
    assert all(action[1] != 109 for action in out)


def test_policy_ship_extras_can_be_paused_without_removing_shared_ships():
    shared = [("ship", 202, 3)]
    policy = [{"id": 204, "batch": 5, "max_total": 100}]
    levels = {21: 2, 115: 2, 202: 0, 204: 0}

    paused = pe.apply_ship_priority(
        shared, policy, levels, allow_policy_extras=False
    )
    assert paused == shared

    enabled = pe.apply_ship_priority(
        shared, policy, levels, allow_policy_extras=True
    )
    assert enabled[0] == ("ship", 204, 5)
    assert shared[0] in enabled


def test_policy_ship_extras_wait_for_identity_planets(monkeypatch):
    monkeypatch.setattr(pe.db_reader, "list_planets_db", lambda email: [{"id": 1}])
    assert not pe._identity_planets_ready("ai01@bots.local", "raider")
    assert not pe._identity_planets_ready("ai03@bots.local", "miner")
    assert pe._identity_planets_ready("ai02@bots.local", "turtle")

    monkeypatch.setattr(
        pe.db_reader,
        "list_planets_db",
        lambda email: [{"id": 1}, {"id": 2}, {"id": 3}],
    )
    assert pe._identity_planets_ready("ai01@bots.local", "raider")
    assert pe._identity_planets_ready("ai03@bots.local", "miner")


def test_policy_ship_extras_fail_closed_when_planet_read_fails(monkeypatch):
    def _boom(email):
        raise RuntimeError("db down")

    monkeypatch.setattr(pe.db_reader, "list_planets_db", _boom)
    assert not pe._identity_planets_ready("ai01@bots.local", "raider")
