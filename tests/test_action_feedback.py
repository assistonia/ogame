from __future__ import annotations

import json

from bots.ai_agents import action_feedback


def test_lifecycle_is_additive_and_duplicate_transition_is_idempotent(tmp_path):
    outcome = {
        "command_id": "development:planet:42",
        "action": "development",
        "status": "queued",
        "planet_id": 42,
        "coordinates": "1:2:3",
        "reason": "development_actions_queued",
    }

    first = action_feedback.append_action_outcome(
        "ai01@bots.local", outcome, state_root=tmp_path
    )
    duplicate = action_feedback.append_action_outcome(
        "ai01@bots.local", outcome, state_root=tmp_path
    )

    assert first == duplicate
    assert first["sequence"] == 1
    assert first["lifecycle"] == "queued"
    stored = json.loads(
        (tmp_path / "ai01" / "action_feedback.json").read_text(encoding="utf-8")
    )
    assert stored["next_sequence"] == 2
    assert len(stored["outcomes"]) == 1


def test_lifecycle_keeps_distinct_transitions_and_never_promotes_executed_to_completed(
    tmp_path,
):
    base = {
        "command_id": "development:planet:77",
        "action": "development",
        "planet_id": 77,
    }
    for status in ("planned", "accepted", "executed", "completed"):
        action_feedback.append_action_outcome(
            "ai02@bots.local",
            {**base, "status": status},
            state_root=tmp_path,
        )

    recent = list(reversed(action_feedback.load_recent_action_outcomes(
        "ai02@bots.local", state_root=tmp_path
    )))
    assert [row["status"] for row in recent] == [
        "planned",
        "accepted",
        "executed",
        "completed",
    ]
    assert [row["lifecycle"] for row in recent] == [
        "planned",
        "accepted",
        "queued",
        "completed",
    ]


def test_old_version_one_store_without_lifecycle_remains_readable(tmp_path):
    path = tmp_path / "ai03" / "action_feedback.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "version": 1,
            "next_sequence": 2,
            "outcomes": [{
                "feedback_version": 1,
                "recorded_at": "2026-01-01T00:00:00Z",
                "command_id": "old:1",
                "action": "build",
                "status": "accepted",
                "sequence": 1,
            }],
        }),
        encoding="utf-8",
    )

    recent = action_feedback.load_recent_action_outcomes(
        "ai03@bots.local", state_root=tmp_path
    )

    assert recent[0]["command_id"] == "old:1"
    assert "lifecycle" not in recent[0]


def test_compare_and_append_transition_allows_only_current_queued_state(tmp_path):
    action_feedback.append_action_outcome(
        "ai04@bots.local",
        {
            "command_id": "development:44",
            "action": "development",
            "status": "queued",
        },
        state_root=tmp_path,
    )
    transition = {
        "command_id": "development:44",
        "action": "development",
        "status": "completed",
    }

    first, appended_first = action_feedback.append_action_transition_if_current(
        "ai04@bots.local",
        transition,
        expected_lifecycles={"queued"},
        state_root=tmp_path,
    )
    second, appended_second = action_feedback.append_action_transition_if_current(
        "ai04@bots.local",
        transition,
        expected_lifecycles={"queued"},
        state_root=tmp_path,
    )

    assert appended_first is True
    assert first["status"] == "completed"
    assert appended_second is False
    assert second["sequence"] == first["sequence"]
