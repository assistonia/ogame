from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import ai_learning_health as health


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_agent_audit_counts_colony_feedback_and_memory_integrity(tmp_path):
    agent = tmp_path / "ai01"
    agent.mkdir()
    _write(
        agent / "empire_status.json",
        {
            "empire": {
                "planets": [
                    {
                        "mines": [20, 16, 16],
                        "energy_balance": -100,
                        "production_factor_pct": 75,
                    },
                    {
                        "mines": [0, 0, 0],
                        "energy_balance": 0,
                        "production_factor_pct": 100,
                    },
                ]
            }
        },
    )
    _write(
        agent / "action_feedback.json",
        {
            "outcomes": [
                {
                    "action": "development",
                    "status": "executed",
                    "lifecycle": "queued",
                },
                {
                    "action": "expedition",
                    "status": "confirmed",
                    "lifecycle": "completed",
                },
            ]
        },
    )
    (agent / "memory.md").write_text("ok\n@@\nbroken\n", encoding="utf-8")

    row = health.audit_agent_state(agent)

    assert row["planet_count"] == 2
    assert row["negative_energy_planets"] == 1
    assert row["throttled_planets"] == 1
    assert row["zero_mine_planets"] == 1
    assert row["development_feedback_count"] == 1
    assert row["feedback_statuses"] == {"confirmed": 1, "executed": 1}
    assert row["feedback_lifecycles"] == {"completed": 1, "queued": 1}
    assert row["memory_artifact_count"] == 1


def test_aggregate_exposes_missing_state_and_sums_acceptance_metrics():
    result = health.aggregate(
        [
            {
                "agent": "ai01",
                "planet_count": 2,
                "negative_energy_planets": 1,
                "throttled_planets": 1,
                "zero_mine_planets": 0,
                "feedback_count": 2,
                "development_feedback_count": 1,
                "feedback_lifecycles": {"queued": 1, "completed": 1},
                "memory_artifact_count": 0,
                "snapshot_available": True,
                "feedback_available": True,
            },
            {
                "agent": "ai02",
                "planet_count": 0,
                "memory_artifact_count": 2,
                "snapshot_available": False,
                "feedback_available": False,
            },
        ]
    )

    assert result["agent_count"] == 2
    assert result["planet_count"] == 2
    assert result["development_feedback_count"] == 1
    assert result["feedback_lifecycles"] == {"completed": 1, "queued": 1}
    assert result["memory_artifact_count"] == 2
    assert result["agents_without_snapshot"] == ["ai02"]
    assert result["agents_without_feedback"] == ["ai02"]
