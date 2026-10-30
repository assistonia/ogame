from __future__ import annotations

from datetime import datetime, timezone

from bots.ai_agents import action_feedback
from bots.ai_agents import action_reconciler as reconciler


NOW = 2_000_000_000


def _state(
    *,
    metal_mine: int = 0,
    robot_factory: int = 0,
    energy_technology: int = 0,
    solar_satellite: int = 0,
    rocket_launcher: int = 0,
    building_items: list[dict] | None = None,
    research_items: list[dict] | None = None,
    unit_items: list[dict] | None = None,
    queues_available: bool = True,
) -> dict:
    def queue(items):
        return {
            "status": "ready" if queues_available else "unavailable",
            "available": queues_available,
            "occupied": bool(items) if queues_available else None,
            "items": items or [],
        }

    return {
        "status": "ready",
        "observed_at": NOW,
        "planets": [{
            "planet_id": 42,
            "levels": {
                "buildings": {"metal_mine": metal_mine},
                "facilities": {"robot_factory": robot_factory},
            },
            "ships": {"solar_satellite": solar_satellite},
            "defense": {"rocket_launcher": rocket_launcher},
            "queues": {
                "building": queue(building_items),
                "unit": queue(unit_items),
            },
        }],
        "account": {
            "research_levels": {"energy_technology": energy_technology},
            "research_queue": queue(research_items),
        },
    }


def _append_queued(
    tmp_path,
    *,
    queued_actions: list[dict] | None,
    occurred_at: int = NOW - 60,
    untracked_action_count: int = 0,
) -> None:
    action_feedback.append_action_outcome(
        "ai01@bots.local",
        {
            "command_id": "development:42:targets",
            "action": "development",
            "status": "queued",
            "policy_activation_id": "activation-canary",
            "planet_id": 42,
            "coordinates": "1:2:3",
            "occurred_at": datetime.fromtimestamp(
                occurred_at, tz=timezone.utc
            ).isoformat(),
            "details": {
                "queued_actions": queued_actions,
                "untracked_action_count": untracked_action_count,
            },
        },
        state_root=tmp_path,
    )


def test_all_machine_readable_build_facility_research_unit_targets_complete(
    tmp_path,
):
    actions = [
        {
            "object_type": "building",
            "object_id": 1,
            "planet_id": 42,
            "target_level": 7,
        },
        {
            "object_type": "facility",
            "object_id": 14,
            "planet_id": 42,
            "target_level": 3,
        },
        {
            "object_type": "research",
            "object_id": 113,
            "planet_id": 42,
            "target_level": 5,
        },
        {
            "object_type": "ship",
            "object_id": 212,
            "planet_id": 42,
            "target_total": 12,
        },
        {
            "object_type": "defense",
            "object_id": 401,
            "planet_id": 42,
            "target_total": 20,
        },
    ]
    _append_queued(tmp_path, queued_actions=actions)
    state = _state(
        metal_mine=7,
        robot_factory=3,
        energy_technology=5,
        solar_satellite=12,
        rocket_launcher=20,
    )

    first = reconciler.reconcile_queued_actions(
        "ai01@bots.local", state, state_root=tmp_path, now=NOW
    )
    second = reconciler.reconcile_queued_actions(
        "ai01@bots.local", state, state_root=tmp_path, now=NOW
    )
    recent = action_feedback.load_recent_action_outcomes(
        "ai01@bots.local", limit=10, state_root=tmp_path
    )

    assert first["transitioned"] == 1
    assert second["transitioned"] == 0
    assert second["examined"] == 0
    assert recent[0]["status"] == "completed"
    assert recent[0]["policy_activation_id"] == "activation-canary"
    assert len(recent[0]["details"]["observations"]) == 5


def test_matching_active_queue_keeps_receipt_pending(tmp_path):
    action = {
        "object_type": "building",
        "object_id": 1,
        "planet_id": 42,
        "target_level": 7,
    }
    _append_queued(tmp_path, queued_actions=[action])
    state = _state(
        metal_mine=6,
        building_items=[{"object_id": 1, "object_level_target": 7}],
    )

    result = reconciler.reconcile_queued_actions(
        "ai01@bots.local", state, state_root=tmp_path, now=NOW
    )

    assert result["transitioned"] == 0
    assert result["pending"] == 1
    assert result["reasons"] == {"development_queue_active": 1}


def test_old_missing_queue_and_unmet_target_becomes_stalled(tmp_path):
    action = {
        "object_type": "research",
        "object_id": 113,
        "planet_id": 42,
        "target_level": 5,
    }
    _append_queued(
        tmp_path,
        queued_actions=[action],
        occurred_at=NOW - reconciler.STALL_AFTER_SECONDS - 1,
    )

    result = reconciler.reconcile_queued_actions(
        "ai01@bots.local",
        _state(energy_technology=4),
        state_root=tmp_path,
        now=NOW,
    )
    recent = action_feedback.load_recent_action_outcomes(
        "ai01@bots.local", limit=2, state_root=tmp_path
    )

    assert result["transitioned"] == 1
    assert recent[0]["status"] == "stalled"
    assert recent[0]["reason"] == "development_target_missing_after_queue_grace"


def test_notes_without_machine_targets_never_complete(tmp_path):
    _append_queued(tmp_path, queued_actions=None)

    result = reconciler.reconcile_queued_actions(
        "ai01@bots.local",
        _state(metal_mine=99, robot_factory=99, energy_technology=99),
        state_root=tmp_path,
        now=NOW,
    )

    assert result["transitioned"] == 0
    assert result["pending"] == 1
    assert result["reasons"] == {"machine_target_missing": 1}


def test_unavailable_queue_never_stalls_even_after_grace(tmp_path):
    action = {
        "object_type": "ship",
        "object_id": 212,
        "planet_id": 42,
        "target_total": 20,
    }
    _append_queued(
        tmp_path,
        queued_actions=[action],
        occurred_at=NOW - reconciler.STALL_AFTER_SECONDS - 1,
    )

    result = reconciler.reconcile_queued_actions(
        "ai01@bots.local",
        _state(solar_satellite=10, queues_available=False),
        state_root=tmp_path,
        now=NOW,
    )

    assert result["transitioned"] == 0
    assert result["pending"] == 1
    assert result["reasons"] == {"target_or_queue_state_unavailable": 1}


def test_stale_snapshot_is_rejected_before_feedback_read(tmp_path):
    result = reconciler.reconcile_queued_actions(
        "ai01@bots.local",
        {**_state(), "observed_at": NOW - reconciler.MAX_SNAPSHOT_AGE_SECONDS - 1},
        state_root=tmp_path,
        now=NOW,
    )

    assert result == {
        "status": "unavailable",
        "reason": "fresh_empire_state_required",
        "examined": 0,
        "transitioned": 0,
    }
