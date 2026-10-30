from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.ai_agents import experience_scorecard as sc
from scripts import repair_scorecard_contamination as repair


def _metrics(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
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
        """
    )
    return con


def test_defense_only_negative_raid_observation_is_quarantined(tmp_path):
    db = tmp_path / "metrics.db"
    con = _metrics(db)
    con.execute(
        "INSERT INTO battles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-07-12T10:30:00Z",
            1,
            7,
            3,
            0,
            0,
            0,
            "2026-07-12T10:30:00Z",
            "enemy@local",
            "ai06@bots.local",
            0,
            1_000_000,
            0,
            0,
        ),
    )
    con.commit()
    con.close()
    policy = {
        "personality": "raider",
        "aggression": {"min_loot": 50_000},
        "energy_target": 0,
    }
    observation = {
        "id": "ai06@bots.local|activation-1|fingerprint",
        "interval": {
            "start": "2026-07-12T10:00:00Z",
            "end": "2026-07-12T11:00:00Z",
        },
        "policy_fingerprint": sc._policy_fingerprint(policy),
        "policy": policy,
        "facets": sc._policy_facets(policy),
        "economy": "neutral",
        "raid": "negative",
        "harvest": "no-activity",
        "military": "negative",
    }

    repaired, touched = repair.repair_observations(
        {"observations": [observation], "promoted_lessons": ["contaminated"]},
        db,
        "ai06@bots.local",
        cleaned_at="2026-07-13T00:00:00Z",
        kind="defense-only-negative",
    )

    assert len(touched) == 1
    item = repaired["observations"][0]
    assert item["legacy_raid"] == "negative"
    assert item["raid"] == item["military"] == "no-activity"
    assert item["economy"] == "neutral"
    assert item["contamination_cleanup"]["conventional_attacks"] == 0
    assert item["contamination_cleanup"]["conventional_defenses"] == 1


def test_real_attack_interval_is_not_rewritten_as_defense_only(tmp_path):
    db = tmp_path / "metrics.db"
    con = _metrics(db)
    con.execute(
        "INSERT INTO battles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-07-12T10:30:00Z",
            2,
            3,
            7,
            100,
            0,
            0,
            "2026-07-12T10:30:00Z",
            "ai06@bots.local",
            "enemy@local",
            500,
            0,
            0,
            0,
        ),
    )
    con.commit()
    con.close()
    observation = {
        "id": "ai06@bots.local|activation-2|fingerprint",
        "interval": {
            "start": "2026-07-12T10:00:00Z",
            "end": "2026-07-12T11:00:00Z",
        },
        "policy_fingerprint": "fingerprint",
        "policy": {"personality": "raider"},
        "raid": "negative",
        "military": "negative",
    }

    repaired, touched = repair.repair_observations(
        {"observations": [observation], "promoted_lessons": []},
        db,
        "ai06@bots.local",
        cleaned_at="2026-07-13T00:00:00Z",
        kind="defense-only-negative",
    )

    assert touched == []
    assert repaired["observations"][0]["raid"] == "negative"
