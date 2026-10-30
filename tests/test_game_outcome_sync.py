from __future__ import annotations

from bots.ai_agents import action_feedback
from bots.ai_agents import game_outcome_sync as sync


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, _sql, params):
        self.params = params

    def fetchall(self):
        cursor = int(self.params[1])
        return [row for row in self.rows if int(row["id"]) > cursor]


class _Conn:
    def __init__(self, rows):
        self.cursor_instance = _Cursor(rows)

    def cursor(self):
        return self.cursor_instance


def test_settled_mission_messages_feed_action_learning_once(tmp_path, monkeypatch):
    rows = [
        {
            "id": 10,
            "key": "espionage_report",
            "params": '{"coordinates":"[1:50:7]"}',
            "espionage_report_id": 77,
            "battle_report_id": None,
            "created_at": "2026-07-27 10:00:00",
        },
        {
            "id": 11,
            "key": "expedition_gain_resources",
            "params": '{"resource_type":"metal","resource_amount":12345}',
            "espionage_report_id": None,
            "battle_report_id": None,
            "created_at": "2026-07-27 10:01:00",
        },
        {
            "id": 12,
            "key": "fleet_lost_contact",
            "params": '{"coordinates":"[1:50:7]"}',
            "espionage_report_id": None,
            "battle_report_id": None,
            "created_at": "2026-07-27 10:02:00",
        },
    ]
    monkeypatch.setattr(sync.db_reader, "_conn", lambda: _Conn(rows))

    first = sync.sync_game_outcomes("ai07@bots.local", state_root=tmp_path)
    second = sync.sync_game_outcomes("ai07@bots.local", state_root=tmp_path)
    outcomes = action_feedback.load_recent_action_outcomes(
        "ai07@bots.local", limit=10, state_root=tmp_path
    )

    assert first["imported"] == 3
    assert second["imported"] == 0
    assert [row["action"] for row in outcomes] == ["fleet", "expedition", "spy"]
    assert outcomes[0]["status"] == "lost"
    assert outcomes[0]["ambiguous"] is True
    assert outcomes[1]["details"]["params"]["resource_amount"] == 12345


def test_settled_outcome_reuses_pending_dispatch_command_id(tmp_path, monkeypatch):
    action_feedback.append_action_outcome(
        "ai04@bots.local",
        {
            "command_id": "legacy:expedition:abc",
            "action": "expedition",
            "status": "dispatched",
            "details": {"note": "expedition SEND 1:50:16"},
        },
        state_root=tmp_path,
    )
    rows = [{
        "id": 20,
        "key": "expedition_failed",
        "params": '{"message_variation_id":1}',
        "espionage_report_id": None,
        "battle_report_id": None,
        "created_at": "2026-07-27 11:00:00",
    }]
    monkeypatch.setattr(sync.db_reader, "_conn", lambda: _Conn(rows))

    sync.sync_game_outcomes("ai04@bots.local", state_root=tmp_path)
    outcomes = action_feedback.load_recent_action_outcomes(
        "ai04@bots.local", limit=10, state_root=tmp_path
    )

    assert outcomes[0]["command_id"] == "legacy:expedition:abc"
    assert outcomes[0]["status"] == "completed"
    assert outcomes[0]["details"]["dispatch_command_id"] == "legacy:expedition:abc"
