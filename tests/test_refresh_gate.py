from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.ai_agents import memory_integrity as memory
from bots.ai_agents import refresh_gate as gate
from bots.ai_agents.experience_scorecard import LESSONS_END, LESSONS_START


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 12, tzinfo=UTC)


def _policy():
    return {
        "activation_id": "activation-1",
        "activated_at": "2026-07-27T10:00:00Z",
        "updated_at": "2026-07-27T11:30:00Z",
        "ttl_hours": 2,
        "personality": "miner",
        "energy_target": 0,
    }


def _context():
    return {
        "resources": {"metal": 1_000_000, "crystal": 500_000, "deuterium": 200_000},
        "normalized_state": {
            "status": "ready",
            "planet_count": 1,
            "planets": [{
                "planet_id": 7,
                "status": "ready",
                "core_status": "ready",
                "issues": [],
                "resources": {
                    "metal": 1_000_000,
                    "crystal": 500_000,
                    "deuterium": 200_000,
                },
                "energy": {
                    "balance": 250,
                    "maximum": 1000,
                    "used": 750,
                    "production_factor_pct": 100,
                },
                "levels": {
                    "buildings": {
                        "metal_mine": 20,
                        "crystal_mine": 17,
                        "deuterium_synthesizer": 14,
                        "solar_plant": 20,
                    },
                    "facilities": {"robot_factory": 6},
                },
                "production_status": {"status": "ready", "reason": None},
                "queues": {
                    "building": {
                        "status": "ready",
                        "occupied": False,
                        "remaining_seconds": 0,
                    },
                },
            }],
            "account": {
                "research_levels": {"energy_technology": 8},
                "fleet_slots": {"status": "ready", "active": 2, "maximum": 8},
            },
        },
        "empire": {
            "raid_opportunity": {"status": "ready", "cargo_shortfall": 0},
            "secondary_goals": {"deathstar": {"status": "locked"}},
        },
        "recent_spy": [],
        "recent_action_outcomes": [],
        "recent_action_rejections": [],
        "action_feedback_status": {"status": "ready", "count": 10},
        "action_reconciliation": {
            "status": "ready",
            "examined": 0,
            "transitioned": 0,
        },
        "game_outcome_sync": {"status": "ready", "imported": 0},
        "diplomacy": {
            "status": "ready",
            "recent_messages": [],
            "contracts": [],
        },
        "highscore": {"my_rank": 20, "gap_to_top10": 1000},
    }


def _state(context, policy=None):
    policy = policy or _policy()
    return {
        "last_model_at": "2026-07-27T10:30:00Z",
        "last_model_activation_id": policy["activation_id"],
        "last_model_context_fingerprint": gate.context_fingerprint(context),
        "last_model_policy_fingerprint": gate.effective_policy_fingerprint(policy),
        "last_model_rejection_fingerprint": gate.rejection_fingerprint(context),
        "last_model_diplomacy_fingerprint": gate.diplomacy_fingerprint(context),
    }


def test_unchanged_stable_context_skips_expensive_model_call():
    context = _context()
    result = gate.evaluate_refresh_gate(
        _policy(), context, _state(context), now=NOW
    )
    assert result["decision"] == "skip_model"
    assert result["call_model"] is False
    assert result["reason"] == "stable-context-and-fresh-policy"
    assert result["fingerprints"]["context"] == gate.context_fingerprint(context)


def test_routine_small_resource_drift_is_not_material():
    context = _context()
    state = _state(context)
    context["resources"]["metal"] += 1_000
    context["normalized_state"]["planets"][0]["resources"]["metal"] += 1_000
    result = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert result["decision"] == "skip_model"


def test_changed_context_calls_model():
    context = _context()
    state = _state(context)
    context["normalized_state"]["planets"][0]["levels"]["buildings"]["metal_mine"] = 21
    result = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert result["decision"] == "call_model"
    assert result["reason"] == "material-context-changed"


def test_changed_effective_policy_calls_model_even_when_context_is_stable():
    context = _context()
    policy = _policy()
    state = _state(context, policy)
    policy["energy_target"] = 100
    result = gate.evaluate_refresh_gate(policy, context, state, now=NOW)
    assert result["decision"] == "call_model"
    assert result["reason"] == "effective-policy-changed-since-model"


def test_anomaly_and_critical_health_fail_open_to_model():
    context = _context()
    state = _state(context)
    context["action_feedback_status"] = {
        "status": "unavailable",
        "reason": "database error",
    }
    anomaly = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert anomaly["decision"] == "call_model"
    assert anomaly["reason"] == "observation-anomaly"

    context = _context()
    state = _state(context)
    context["normalized_state"]["planets"][0]["energy"]["balance"] = -1
    critical = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert critical["decision"] == "call_model"
    assert critical["reason"] == "critical-colony-health"
    assert "negative-energy" in "\n".join(critical["signals"])


def test_known_moon_telemetry_gap_can_skip_only_without_active_moon_plan():
    context = _context()
    context["normalized_state"].update({
        "status": "partial",
        "reason": "one-or-more-moons-partial",
        "moons": [{
            "moon_id": 77,
            "status": "partial",
            "issues": [
                "resources-unavailable",
                "fields-unavailable",
                "facility-levels-unavailable",
                "building-queue:moon-queue-unavailable",
                "unit-queue:moon-queue-unavailable",
            ],
        }],
    })
    policy = _policy()
    state = _state(context, policy)
    stable = gate.evaluate_refresh_gate(policy, context, state, now=NOW)
    assert stable["decision"] == "skip_model"

    policy["plans"] = {
        "moon_development": [{"command_id": "moon-build-1"}]
    }
    active = gate.evaluate_refresh_gate(policy, context, state, now=NOW)
    assert active["decision"] == "call_model"
    assert active["reason"] == "observation-anomaly"


def test_new_rejection_and_diplomacy_events_call_model():
    context = _context()
    state = _state(context)
    context["recent_action_rejections"] = [{
        "command_id": "build-p7-21",
        "status": "rejected",
        "sequence": 11,
    }]
    rejected = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert rejected["reason"] == "new-action-rejection-event"

    context = _context()
    state = _state(context)
    context["diplomacy"]["recent_messages"] = [{
        "message_id": 91,
        "sender_id": 4,
        "kind": "message",
    }]
    diplomatic = gate.evaluate_refresh_gate(_policy(), context, state, now=NOW)
    assert diplomatic["reason"] == "new-diplomacy-event"


def test_skip_refreshes_liveness_without_changing_activation(tmp_path):
    policy_path = tmp_path / "policy.json"
    decision_path = tmp_path / "decision.json"
    policy_path.write_text(json.dumps(_policy()), encoding="utf-8")
    decision_path.write_text(json.dumps({
        "decision": "skip_model",
        "call_model": False,
        "reason": "stable-context-and-fresh-policy",
        "checked_at": "2026-07-27T12:00:00Z",
        "policy_activation_id": "activation-1",
    }), encoding="utf-8")

    result = gate.refresh_policy_liveness(
        policy_path, decision_path, now=NOW
    )
    refreshed = json.loads(policy_path.read_text(encoding="utf-8"))
    assert result["status"] == "refreshed"
    assert refreshed["activation_id"] == "activation-1"
    assert refreshed["activated_at"] == "2026-07-27T10:00:00Z"
    assert refreshed["updated_at"] == "2026-07-27T12:00:00Z"
    assert "activation" not in result or result["activation_id"] == "activation-1"


def test_policy_journal_dedupes_and_preserves_unmanaged_and_lessons(tmp_path):
    memory_path = tmp_path / "memory.md"
    original = (
        "깨진 옛 일기 @@ -1,2 +1,2\n"
        f"{LESSONS_START}\n"
        "## 검증된 교훈\n"
        "- 서버 관리 교훈\n"
        f"{LESSONS_END}\n"
        "사용자 보존 메모\n"
    )
    memory_path.write_text(original, encoding="utf-8")
    policy = {
        **_policy(),
        "note": "  compact   note <!-- fake marker -->\nsecond line  ",
    }

    first = memory.record_policy_journal(memory_path, policy)
    after_first = memory_path.read_text(encoding="utf-8")
    second = memory.record_policy_journal(memory_path, policy)
    after_second = memory_path.read_text(encoding="utf-8")

    assert first["status"] == "recorded"
    assert second["status"] == "deduplicated"
    assert after_second == after_first
    assert original in after_first
    assert after_first.count(LESSONS_START) == 1
    assert after_first.count(LESSONS_END) == 1
    assert after_first.count("activation=activation-1") == 1
    assert "fake marker" in after_first
    assert "<!-- fake marker -->" not in after_first


def test_malformed_managed_journal_is_reported_without_rewrite(tmp_path):
    memory_path = tmp_path / "memory.md"
    original = f"old\n{memory.JOURNAL_START}\ntruncated\n"
    memory_path.write_text(original, encoding="utf-8")
    result = memory.record_policy_journal(memory_path, _policy())
    assert result["status"] == "quarantined"
    assert memory_path.read_text(encoding="utf-8") == original
    report = memory.analyze_memory_file(memory_path)
    assert report["status"] == "quarantine-recommended"
    assert "unbalanced-policy-journal-markers" in report["issues"]


def test_policy_journal_is_safely_bounded(tmp_path):
    memory_path = tmp_path / "memory.md"
    for index in range(memory.JOURNAL_MAX_ENTRIES + 5):
        policy = {
            **_policy(),
            "activation_id": f"activation-{index}",
            "activated_at": f"2026-07-27T{index % 24:02d}:00:00Z",
        }
        assert memory.record_policy_journal(memory_path, policy)["status"] == "recorded"
    report = memory.analyze_memory_file(memory_path)
    text = memory_path.read_text(encoding="utf-8")
    assert report["entry_count"] == memory.JOURNAL_MAX_ENTRIES
    assert "activation=activation-0 " not in text
    assert f"activation=activation-{memory.JOURNAL_MAX_ENTRIES + 4} " in text
