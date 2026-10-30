from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.ai_agents import experience_scorecard as sc
from scripts import metrics_collector, repair_scorecard_contamination


UTC = dt.timezone.utc


def _policy(path, *, updated_at="2026-07-12T10:00:00Z"):
    path.write_text(json.dumps({
        "updated_at": updated_at,
        "ttl_hours": 2,
        "personality": "miner",
        "ship_priority": [{"id": 202, "batch": 2, "max_total": 20}],
        "energy_target": 0,
        "note": "카고 상한을 지키며 경제 유지",
    }), encoding="utf-8")


def _create_metrics(path, *, expanded=True):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE snapshots (
            ts TEXT NOT NULL, email TEXT NOT NULL, personality TEXT,
            planets INTEGER, building_sum INTEGER, facility_sum INTEGER,
            defense_sum INTEGER, ships_sum INTEGER,
            metal REAL, crystal REAL, deuterium REAL, score INTEGER
        );
    """)
    if expanded:
        con.executescript("""
            CREATE TABLE battles (
                ts TEXT NOT NULL, battle_id INTEGER UNIQUE,
                attacker_id INTEGER, defender_id INTEGER,
                loot_metal INTEGER, loot_crystal INTEGER, loot_deuterium INTEGER,
                battle_ts TEXT, attacker_email TEXT, defender_email TEXT,
                attacker_loss REAL, defender_loss REAL,
                is_probe_only INTEGER DEFAULT 0,
                is_admin_test INTEGER DEFAULT 0
            );
            CREATE TABLE debris_harvests (
                message_id INTEGER PRIMARY KEY, event_ts TEXT, email TEXT,
                metal INTEGER, crystal INTEGER, deuterium INTEGER
            );
            CREATE TABLE metric_capabilities (
                metric TEXT PRIMARY KEY, last_success_ts TEXT NOT NULL
            );
            INSERT INTO metric_capabilities VALUES
                ('battle_outcomes','2026-07-12T12:00:00Z'),
                ('debris_harvests','2026-07-12T12:00:00Z');
        """)
    else:
        con.execute("""CREATE TABLE battles (
            ts TEXT NOT NULL, battle_id INTEGER UNIQUE,
            attacker_id INTEGER, defender_id INTEGER,
            loot_metal INTEGER, loot_crystal INTEGER, loot_deuterium INTEGER
        )""")
    return con


def _snap(con, ts, email, personality, building, stock, score):
    con.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, email, personality, 1, building, 0, 0, 0, stock, 0, 0, score),
    )


def _economic_fixture(con):
    # AI: 실질자산 +300, 자원건물 +4, 보유자원 +100k.
    _snap(con, "2026-07-12T10:05:00Z", "ai03@bots.local", "ai_agent", 20, 0, 1000)
    _snap(con, "2026-07-12T11:55:00Z", "ai03@bots.local", "ai_agent", 24, 100_000, 1200)
    # 동급 miner 규칙봇: 각각 +150/+2/+50k → AI 상대치 2.0x.
    _snap(con, "2026-07-12T10:05:00Z", "bot003@bots.local", "miner", 20, 0, 1000)
    _snap(con, "2026-07-12T11:55:00Z", "bot003@bots.local", "miner", 22, 50_000, 1100)
    # 시작 자산 2배 밖 miner와 다른 personality는 cohort에서 제외돼야 한다.
    _snap(con, "2026-07-12T10:05:00Z", "bot099@bots.local", "miner", 40, 0, 10_000)
    _snap(con, "2026-07-12T11:55:00Z", "bot099@bots.local", "miner", 60, 0, 20_000)
    _snap(con, "2026-07-12T10:05:00Z", "bot001@bots.local", "raider", 20, 0, 1000)
    _snap(con, "2026-07-12T11:55:00Z", "bot001@bots.local", "raider", 40, 0, 3000)


def test_gate_is_default_off_and_requires_tech_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "FEATURE_MARKER", tmp_path / "feature")
    monkeypatch.setattr(sc, "TECH_TREE_MARKER", tmp_path / "tech")
    assert sc.gate_status({}) == (False, "feature-flag-off")
    (tmp_path / "feature").touch()
    assert sc.gate_status({}) == (False, "tech-tree-not-deployed")
    (tmp_path / "tech").touch()
    assert sc.gate_status({}) == (True, "enabled")
    # 명시 env=0은 marker보다 우선하는 즉시 kill switch다.
    assert sc.gate_status({sc.FEATURE_FLAG: "0"}) == (False, "feature-flag-off")
    assert sc.gate_status({sc.FEATURE_FLAG: "1", sc.TECH_TREE_FLAG: "1"}) == (
        True, "enabled"
    )


def test_policy_activation_uses_server_time_and_closes_exact_interval(tmp_path):
    history = tmp_path / "policy_activation_history.json"
    first = sc.record_policy_activation(
        "ai10@bots.local",
        {"updated_at": "2099-01-01T00:00:00Z", "personality": "raider"},
        None,
        history,
        now=dt.datetime(2026, 7, 12, 10, tzinfo=UTC),
    )
    second = sc.record_policy_activation(
        "ai10@bots.local",
        {
            "updated_at": "1900-01-01T00:00:00Z",
            "personality": "raider",
            "energy_target": 50,
        },
        first,
        history,
        now=dt.datetime(2026, 7, 12, 11, tzinfo=UTC),
    )

    assert first["updated_at"] == first["activated_at"] == "2026-07-12T10:00:00Z"
    assert second["updated_at"] == second["activated_at"] == "2026-07-12T11:00:00Z"
    assert len(first["activation_id"]) == 24
    data = json.loads(history.read_text(encoding="utf-8"))
    assert data["activations"][0]["deactivated_at"] == "2026-07-12T11:00:00Z"
    assert data["activations"][1]["deactivated_at"] is None

    pending, reason = sc._latest_completed_activation(
        history, dt.datetime(2026, 7, 12, 11, 9, tzinfo=UTC), 600
    )
    assert pending is None
    assert reason == "no-completed-policy-past-collection-grace"
    eligible, reason = sc._latest_completed_activation(
        history, dt.datetime(2026, 7, 12, 11, 10, tzinfo=UTC), 600
    )
    assert reason is None
    assert eligible["activation_id"] == first["activation_id"]


def test_effective_policy_noop_keeps_activation_and_refreshes_ttl(tmp_path):
    history = tmp_path / "policy_activation_history.json"
    first = sc.record_policy_activation(
        "ai03@bots.local",
        {
            "personality": "miner",
            "energy_target": 0,
            "note": "first wording",
        },
        None,
        history,
        now=dt.datetime(2026, 7, 12, 10, tzinfo=UTC),
    )
    refreshed = sc.record_policy_activation(
        "ai03@bots.local",
        {
            "personality": "miner",
            "energy_target": 0,
            "note": "same controls, different wording",
        },
        first,
        history,
        now=dt.datetime(2026, 7, 12, 11, tzinfo=UTC),
    )

    assert refreshed["activation_id"] == first["activation_id"]
    assert refreshed["activated_at"] == first["activated_at"]
    assert refreshed["updated_at"] == "2026-07-12T11:00:00Z"
    data = json.loads(history.read_text(encoding="utf-8"))
    assert len(data["activations"]) == 1
    assert data["activations"][0]["deactivated_at"] is None


def test_live_scorecard_uses_closed_activation_and_requires_backfill(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    _economic_fixture(con)
    con.execute(
        "UPDATE metric_capabilities SET last_success_ts='2026-07-12T10:59:59Z'"
    )
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    history = tmp_path / "history.json"
    first = sc.record_policy_activation(
        "ai03@bots.local",
        {"personality": "miner", "energy_target": 0},
        None,
        history,
        now=dt.datetime(2026, 7, 12, 10, tzinfo=UTC),
    )
    second = sc.record_policy_activation(
        "ai03@bots.local",
        {"personality": "miner", "energy_target": 50},
        first,
        history,
        now=dt.datetime(2026, 7, 12, 11, tzinfo=UTC),
    )
    policy.write_text(json.dumps(second), encoding="utf-8")

    result = sc.build_scorecard(
        "ai03@bots.local",
        policy,
        db,
        now=dt.datetime(2026, 7, 12, 11, 10, tzinfo=UTC),
        activation_history_path=history,
    )

    assert result["activation_id"] == first["activation_id"]
    assert result["interval"] == {
        "start": "2026-07-12T10:00:00Z",
        "end": "2026-07-12T11:00:00Z",
    }
    assert result["interval_source"] == "server-activation-history"
    assert result["military"]["battle"]["status"] == "unavailable"
    assert result["military"]["battle"]["reason"] == (
        "collector-has-not-backfilled-interval-end"
    )


def test_scorecard_pairs_policy_with_relative_economy_and_military(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    _economic_fixture(con)
    con.executemany(
        """INSERT INTO battles (
               ts,battle_id,attacker_id,defender_id,
               loot_metal,loot_crystal,loot_deuterium,
               battle_ts,attacker_email,defender_email,attacker_loss,defender_loss
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("2026-07-12T11:01:00Z", 1, 3, 4, 100, 200, 300,
             "2026-07-12T11:00:00Z", "ai03@bots.local", "other@local", 100, 50),
            ("2026-07-12T11:11:00Z", 2, 4, 3, 1, 2, 3,
             "2026-07-12T11:10:00Z", "other@local", "ai03@bots.local", 20, 400),
            # 전용 Admin 수동 전투는 정책 성과에서 제외되어야 한다.
            ("2026-07-12T11:16:00Z", 3, 3, 5, 9_000_000, 0, 0,
             "2026-07-12T11:15:00Z", "ai03@bots.local", "simulation-admin@ogamex.local",
             8_000_000, 0),
        ],
    )
    con.execute("UPDATE battles SET is_admin_test=1 WHERE battle_id=3")
    con.execute(
        "INSERT INTO debris_harvests VALUES (?,?,?,?,?,?)",
        (9, "2026-07-12T11:20:00Z", "ai03@bots.local", 50, 25, 0),
    )
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    _policy(policy)

    result = sc.build_scorecard(
        "ai03@bots.local", policy, db, now=dt.datetime(2026, 7, 12, 12, tzinfo=UTC)
    )

    assert result["status"] == "ready"
    assert result["economy"]["peer_baseline"]["emails"] == ["bot003@bots.local"]
    relative = result["economy"]["relative"]
    assert relative["score_ratio"] == 2.0
    assert relative["resource_building_levels_ratio"] == 2.0
    assert relative["resource_stock_ratio"] == 2.0
    assert relative["effective_asset_ratio"] == 2.0
    battle = result["military"]["battle"]
    assert battle["loot_income"] == 600
    assert battle["attacking_fleet_loss"] == 100
    assert battle["defending_unit_loss"] == 400
    assert result["military"]["debris"]["debris_income"] == 75
    assert result["military"]["raid_net_after_reported_losses"] == 500
    assert result["military"]["net_after_reported_losses"] == 175
    assert result["policy_outcome_pair"] == {
        "economic_verdict": "positive",
        "raid_verdict": "positive",
        "harvest_verdict": "positive",
        "military_verdict": "positive",
    }

    prompt = sc.render_prompt(result)
    assert "같은 personality" in prompt
    assert "personality=miner는 성적 때문에 바꾸지 마라" in prompt
    assert "실질자산 성장 상대치(점수+보유자원/1000): 2.00x" in prompt
    assert "직전 정책(성과와 한 쌍)" in prompt
    assert "[약탈 전투 — 정찰기-only 제외]" in prompt
    assert "[데브리 수거 — 약탈 판정과 분리]" in prompt


def test_economy_uses_normalized_growth_when_no_peer_is_within_two_x(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    # AI는 시작 실질자산 1,000에서 +100, 규칙봇은 10,000에서 +500이다.
    # 원시 증가량은 규칙봇이 5배지만 시작 체급 대비 성장률은 AI가 2배다.
    _snap(con, "2026-07-12T10:05:00Z", "ai01@bots.local", "ai_agent", 10, 0, 1000)
    _snap(con, "2026-07-12T11:55:00Z", "ai01@bots.local", "ai_agent", 11, 0, 1100)
    _snap(con, "2026-07-12T10:05:00Z", "bot001@bots.local", "raider", 20, 0, 10_000)
    _snap(con, "2026-07-12T11:55:00Z", "bot001@bots.local", "raider", 21, 0, 10_500)
    con.commit()
    con.row_factory = sqlite3.Row

    economy = sc._economy(
        con,
        "ai01@bots.local",
        "raider",
        dt.datetime(2026, 7, 12, 10, tzinfo=UTC),
        dt.datetime(2026, 7, 12, 12, tzinfo=UTC),
    )
    con.close()

    assert economy["status"] == "ready"
    assert "시작 자산 대비 시간당 성장률" in economy["peer_baseline"]["selection"]
    assert economy["peer_baseline"]["emails"] == ["bot001@bots.local"]
    assert economy["relative"]["effective_asset_ratio"] == 2.0
    assert economy["relative"]["score_ratio"] is None


def test_probe_only_interval_is_neutral_and_never_a_raid_failure(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    _economic_fixture(con)
    con.execute(
        """INSERT INTO battles (
               ts,battle_id,attacker_id,defender_id,
               loot_metal,loot_crystal,loot_deuterium,
               battle_ts,attacker_email,defender_email,
               attacker_loss,defender_loss,is_probe_only
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-07-12T11:01:00Z", 7, 3, 4, 0, 0, 0,
         "2026-07-12T11:00:00Z", "ai03@bots.local", "other@local", 7000, 0, 1),
    )
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    _policy(policy)

    result = sc.build_scorecard(
        "ai03@bots.local", policy, db, now=dt.datetime(2026, 7, 12, 12, tzinfo=UTC)
    )

    battle = result["military"]["battle"]
    assert battle["attack_count"] == 0
    assert battle["defense_count"] == 0
    assert battle["probe_only_count"] == 1
    assert battle["espionage_cost"] == 7000
    assert result["military"]["raid_net_after_reported_losses"] == 0
    assert result["policy_outcome_pair"]["raid_verdict"] == "neutral"
    assert result["policy_outcome_pair"]["military_verdict"] == "neutral"
    assert "공격 실패가 아닌 관측값" in sc.render_prompt(result)


def test_defense_only_interval_is_not_scored_as_failed_raid(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    _economic_fixture(con)
    con.execute(
        """INSERT INTO battles (
               ts,battle_id,attacker_id,defender_id,
               loot_metal,loot_crystal,loot_deuterium,
               battle_ts,attacker_email,defender_email,
               attacker_loss,defender_loss,is_probe_only
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-07-12T11:01:00Z", 8, 4, 3, 0, 0, 0,
         "2026-07-12T11:00:00Z", "other@local", "ai03@bots.local", 100, 9000, 0),
    )
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    _policy(policy)

    result = sc.build_scorecard(
        "ai03@bots.local", policy, db, now=dt.datetime(2026, 7, 12, 12, tzinfo=UTC)
    )

    assert result["military"]["battle"]["attack_count"] == 0
    assert result["military"]["battle"]["defense_count"] == 1
    assert result["military"]["raid_net_after_reported_losses"] == 0
    assert result["military"]["net_after_reported_losses"] == -9000
    assert result["policy_outcome_pair"]["raid_verdict"] == "no-activity"


def test_legacy_metrics_schema_is_null_not_zero(tmp_path):
    db = tmp_path / "legacy.db"
    con = _create_metrics(db, expanded=False)
    _economic_fixture(con)
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    _policy(policy)

    result = sc.build_scorecard(
        "ai03@bots.local", policy, db, now=dt.datetime(2026, 7, 12, 12, tzinfo=UTC)
    )

    assert result["status"] == "partial"
    battle = result["military"]["battle"]
    assert battle["status"] == "unavailable"
    assert battle["loot_income"] is None
    assert battle["attacking_fleet_loss"] is None
    assert result["military"]["net_after_reported_losses"] is None
    prompt = sc.render_prompt(result)
    assert "약탈 수입: unavailable/NULL" in prompt
    assert "unavailable/NULL은 0이 아니라 미수집·표본부족" in prompt


def test_schema_without_success_marker_cannot_report_zero_combat(tmp_path):
    db = tmp_path / "never_collected.db"
    con = _create_metrics(db)
    _economic_fixture(con)
    con.execute("DELETE FROM metric_capabilities")
    con.commit()
    con.close()
    policy = tmp_path / "policy.json"
    _policy(policy)

    result = sc.build_scorecard(
        "ai03@bots.local", policy, db, now=dt.datetime(2026, 7, 12, 12, tzinfo=UTC)
    )

    assert result["military"]["battle"]["attack_count"] is None
    assert result["military"]["debris"]["harvest_count"] is None
    assert result["military"]["net_after_reported_losses"] is None


def _observed_scorecard(day, verdict="positive", activation_id=None):
    policy = {
        "personality": "miner",
        "ship_priority": [],
        "aggression": {},
        "defense_ratio": None,
        "energy_target": 0,
    }
    result = {
        "email": "ai03@bots.local",
        "interval": {
            "start": f"2026-07-{day:02d}T10:00:00Z",
            "end": f"2026-07-{day:02d}T12:00:00Z",
        },
        "policy": policy,
        "policy_fingerprint": sc._policy_fingerprint(policy),
        "policy_outcome_pair": {
            "economic_verdict": verdict,
            "military_verdict": "unavailable",
        },
    }
    if activation_id is not None:
        result["activation_id"] = activation_id
    return result


def test_memory_promotes_only_three_unique_reproductions_and_preserves_diary(tmp_path):
    ledger = tmp_path / "experience_ledger.json"
    memory = tmp_path / "memory.md"
    memory.write_text("2026-07-10 [policy] 기존 일기\n", encoding="utf-8")

    assert sc.update_learning_ledger(_observed_scorecard(10), ledger, memory) == []
    assert sc.update_learning_ledger(_observed_scorecard(11), ledger, memory) == []
    promoted = sc.update_learning_ledger(_observed_scorecard(12), ledger, memory)
    assert len(promoted) == 1
    assert "3회 재현됨" in promoted[0]

    # 같은 정책 구간 재시도는 네 번째 증거로 세지 않는다.
    promoted_retry = sc.update_learning_ledger(_observed_scorecard(12), ledger, memory)
    assert "3회 재현됨" in promoted_retry[0]
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(data["observations"]) == 3
    text = memory.read_text(encoding="utf-8")
    assert text.count(sc.LESSONS_START) == 1
    assert text.count(sc.LESSONS_END) == 1
    assert "기존 일기" in text

    # 반대 증거가 생기면 장기 교훈에서 보수적으로 내린다(일기/관측은 보존).
    assert sc.update_learning_ledger(_observed_scorecard(13, "negative"), ledger, memory) == []
    assert "기존 일기" in memory.read_text(encoding="utf-8")


def test_ledger_counts_only_server_activated_post_cleanup_observations(tmp_path):
    ledger = tmp_path / "ledger.json"
    memory = tmp_path / "memory.md"
    ledger.write_text(json.dumps({
        "schema_version": 2,
        "observations": [],
        "last_contamination_cleanup": {
            "cleaned_at": "2026-07-10T00:00:00Z",
            "email": "ai03@bots.local",
        },
    }), encoding="utf-8")

    sc.update_learning_ledger(
        _observed_scorecard(11, activation_id="server-activation-1"), ledger, memory
    )

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["post_cleanup_observation_count"] == 1
    assert data["post_cleanup_observation_target"] == 4


def test_probe_contamination_repair_is_selective_and_preserves_policy_data(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    con.executemany(
        """INSERT INTO battles (
               ts,battle_id,attacker_id,defender_id,
               loot_metal,loot_crystal,loot_deuterium,
               battle_ts,attacker_email,defender_email,
               attacker_loss,defender_loss,is_probe_only
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("2026-07-12T10:31:00Z", 21, 10, 20, 0, 0, 0,
             "2026-07-12T10:30:00Z", "ai10@bots.local", "x@local", 7000, 0, 1),
            ("2026-07-12T12:31:00Z", 22, 10, 20, 0, 0, 0,
             "2026-07-12T12:30:00Z", "ai10@bots.local", "x@local", 7000, 0, 0),
        ],
    )
    con.commit()
    con.close()
    policy = {"personality": "raider", "energy_target": 0, "aggression": {"min_loot": 50_000}}
    fingerprint = sc._policy_fingerprint(policy)
    ledger = {
        "schema_version": 1,
        "promoted_lessons": ["오염된 군사 교훈"],
        "observations": [
            {
                "id": "ai10@bots.local|2026-07-12T10:00:00Z",
                "observed_at": "2026-07-12T11:00:00Z",
                "policy_fingerprint": fingerprint,
                "policy": policy,
                "facets": sc._policy_facets(policy),
                "economy": "positive",
                "military": "negative",
                "persona_correction": "raider-lock-preserved",
            },
            {
                "id": "ai10@bots.local|2026-07-12T12:00:00Z",
                "observed_at": "2026-07-12T13:00:00Z",
                "policy_fingerprint": fingerprint,
                "policy": policy,
                "facets": sc._policy_facets(policy),
                "economy": "neutral",
                "military": "negative",
            },
        ],
    }

    repaired, touched = repair_scorecard_contamination.repair_observations(
        ledger, db, "ai10@bots.local", cleaned_at="2026-07-13T00:00:00Z"
    )

    assert [item["id"] for item in touched] == [ledger["observations"][0]["id"]]
    probe_item, real_battle_item = repaired["observations"]
    assert probe_item["legacy_military"] == "negative"
    assert probe_item["raid"] == probe_item["military"] == "neutral"
    assert probe_item["persona_correction"] == "raider-lock-preserved"
    assert probe_item["policy"] == policy
    assert real_battle_item["military"] == "negative"
    assert "raid" not in real_battle_item
    assert repaired["promoted_lessons"] == []


def test_debris_or_legacy_combined_military_cannot_promote_raid_lesson():
    policy = {"personality": "raider", "aggression": {"min_loot": 12_000}}
    observations = []
    for index in range(3):
        observations.append({
            "id": f"legacy-{index}",
            "policy_fingerprint": sc._policy_fingerprint(policy),
            "policy": policy,
            "facets": sc._policy_facets(policy),
            "economy": "neutral",
            "military": "positive",
            "harvest": "positive",
        })
    assert sc.promoted_lessons_from_observations(observations) == []


def test_promotions_are_scoped_to_the_outcome_dimension():
    policy = {
        "personality": "raider",
        "build_priority": ["metal_mine"],
        "energy_target": 75,
        "fleetsave": {"mode": "always"},
        "defense_ratio": 0.9,
        "aggression": {"min_loot": 12_000},
        "targeting": {"allow_defended_targets": False},
        "limits": {
            "attack_target_cooldown_sec": 1800,
            "colonize_cooldown_sec": 99,
        },
        "action_plan": ["build", "development", "spy", "raid"],
    }
    raid_observations = [
        {
            "id": f"raid-{index}",
            "policy_fingerprint": sc._policy_fingerprint(policy),
            "policy": policy,
            # Legacy all-domain facets must not be trusted for promotion.
            "facets": sc._policy_facets(policy),
            "economy": "neutral",
            "raid": "negative",
        }
        for index in range(3)
    ]
    raid_lessons = sc.promoted_lessons_from_observations(raid_observations)
    joined = "\n".join(raid_lessons)
    assert "aggression=" in joined
    assert "targeting=" in joined
    assert "raid_limits=" in joined
    assert "raid_action_plan=" in joined
    assert "energy_target=" not in joined
    assert "build_priority=" not in joined
    assert "fleetsave=" not in joined
    assert "defense_ratio=" not in joined
    assert "colonize_cooldown_sec" not in joined

    economy_observations = [
        {
            "id": f"economy-{index}",
            "policy_fingerprint": sc._policy_fingerprint(policy),
            "policy": policy,
            "facets": sc._policy_facets(policy),
            "economy": "positive",
            "raid": "no-activity",
        }
        for index in range(3)
    ]
    economy_lessons = sc.promoted_lessons_from_observations(economy_observations)
    joined = "\n".join(economy_lessons)
    assert "build_priority=" in joined
    assert "energy_target=" in joined
    assert "economy_action_plan=" in joined
    assert "aggression=" not in joined
    assert "targeting=" not in joined
    assert "fleetsave=" not in joined


def test_debris_only_positive_repair_preserves_economy_and_marks_harvest(tmp_path):
    db = tmp_path / "metrics.db"
    con = _create_metrics(db)
    con.execute(
        "INSERT INTO debris_harvests VALUES (?,?,?,?,?,?)",
        (81, "2026-07-12T10:30:00Z", "ai06@bots.local", 5000, 3000, 0),
    )
    con.commit()
    con.close()
    policy = {"personality": "raider", "aggression": {"min_loot": 5000}}
    observation = {
        "id": "ai06@bots.local|2026-07-12T10:00:00Z",
        "observed_at": "2026-07-12T11:00:00Z",
        "policy_fingerprint": sc._policy_fingerprint(policy),
        "policy": policy,
        "facets": sc._policy_facets(policy),
        "economy": "positive",
        "military": "positive",
    }

    repaired, touched = repair_scorecard_contamination.repair_observations(
        {"observations": [observation], "promoted_lessons": ["legacy"]},
        db,
        "ai06@bots.local",
        cleaned_at="2026-07-13T00:00:00Z",
        kind="debris-only-positive",
    )

    assert len(touched) == 1
    item = repaired["observations"][0]
    assert item["economy"] == "positive"
    assert item["legacy_military"] == "positive"
    assert item["raid"] == item["military"] == "no-activity"
    assert item["harvest"] == "positive"
    assert item["contamination_cleanup"]["debris_income"] == 8000


def test_disabled_generation_does_not_touch_memory_or_ledger(tmp_path):
    memory = tmp_path / "memory.md"
    memory.write_text("legacy\n", encoding="utf-8")
    result = sc.generate_artifacts(
        "ai03@bots.local",
        tmp_path / "missing-policy.json",
        tmp_path / "missing-metrics.db",
        tmp_path / "scorecard.json",
        tmp_path / "prompt.txt",
        tmp_path / "ledger.json",
        memory,
        env={sc.FEATURE_FLAG: "0", sc.TECH_TREE_FLAG: "0"},
    )
    assert result["status"] == "disabled"
    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == ""
    assert memory.read_text(encoding="utf-8") == "legacy\n"
    assert not (tmp_path / "ledger.json").exists()


def test_metrics_collector_migrates_old_battles_idempotently(tmp_path):
    con = sqlite3.connect(tmp_path / "metrics.db")
    con.execute("""CREATE TABLE battles (
        ts TEXT NOT NULL, battle_id INTEGER UNIQUE,
        attacker_id INTEGER, defender_id INTEGER,
        loot_metal INTEGER, loot_crystal INTEGER, loot_deuterium INTEGER
    )""")
    metrics_collector.init_db(con)
    metrics_collector.init_db(con)
    columns = {row[1] for row in con.execute("PRAGMA table_info(battles)")}
    assert {
        "battle_ts", "attacker_email", "defender_email",
        "attacker_loss", "defender_loss", "is_probe_only", "is_admin_test",
    }.issubset(columns)
    assert con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='debris_harvests'"
    ).fetchone()
    assert metrics_collector._nullable_number("NULL") is None
    assert metrics_collector._nullable_number("0") == 0
    con.close()


def test_metrics_collector_backfills_null_rule_personalities(tmp_path):
    con = sqlite3.connect(tmp_path / "metrics.db")
    metrics_collector.init_db(con)
    rows = [
        ("2026-07-13T00:00:00Z", "bot004@bots.local", None),
        ("2026-07-13T00:05:00Z", "bot004@bots.local", None),
        ("2026-07-13T00:00:00Z", "ai04@bots.local", "ai_agent"),
    ]
    for ts, email, personality in rows:
        con.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, email, personality, 1, 1, 0, 0, 0, 0, 0, 0, 1),
        )
    con.commit()

    assert metrics_collector.backfill_snapshot_personalities(con) == 2
    assert con.execute(
        "SELECT DISTINCT personality FROM snapshots WHERE email='bot004@bots.local'"
    ).fetchone()[0] == "explorer"
    assert con.execute(
        "SELECT personality FROM snapshots WHERE email='ai04@bots.local'"
    ).fetchone()[0] == "ai_agent"
    con.close()


def test_metrics_collector_backfills_outcomes_and_harvests_without_live_db(
    tmp_path, monkeypatch
):
    con = sqlite3.connect(tmp_path / "metrics.db")
    metrics_collector.init_db(con)

    def fake_game_sql(query):
        if "FROM users u" in query:
            return [["ai03@bots.local", "1", "20", "5", "0", "2",
                     "100", "200", "300", "1000"]]
        if "FROM battle_reports br" in query:
            return [["42", "3", "4", "10", "20", "30", "1000",
                     "ai03@bots.local", "bot004@bots.local", "40", "50", "0", "0"]]
        if "FROM messages m" in query:
            return [["77", "1100", "ai03@bots.local", "7", "8", "9"]]
        return [["0"]]

    monkeypatch.setattr(metrics_collector, "game_sql", fake_game_sql)
    metrics_collector.collect(con)

    battle = con.execute(
        """SELECT attacker_email, defender_email, attacker_loss, defender_loss,
                  loot_metal+loot_crystal+loot_deuterium, is_probe_only, is_admin_test
           FROM battles WHERE battle_id=42"""
    ).fetchone()
    assert battle == ("ai03@bots.local", "bot004@bots.local", 40, 50, 60, 0, 0)
    harvest = con.execute(
        "SELECT email, metal, crystal, deuterium FROM debris_harvests WHERE message_id=77"
    ).fetchone()
    assert harvest == ("ai03@bots.local", 7, 8, 9)
    assert {
        row[0] for row in con.execute("SELECT metric FROM metric_capabilities")
    } == {"battle_outcomes", "debris_harvests"}
    con.close()
