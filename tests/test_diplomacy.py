from __future__ import annotations

import json

from bots.ai_agents import diplomacy


class _Client:
    def __init__(self):
        self.sent = []

    def send_direct_message(self, recipient_id, text):
        self.sent.append((recipient_id, text))
        return {"status": "OK", "id": len(self.sent)}


def test_only_ai_and_human_are_eligible_participant_types():
    assert diplomacy.participant_kind("ai01@bots.local", "AI 01") == "ai"
    assert diplomacy.participant_kind("pirate01@bots.local", "Pirate") == "ai"
    assert diplomacy.participant_kind("bot001@bots.local", "Rule bot") is None
    assert diplomacy.participant_kind("simulation-admin@ogamex.local", "Admin") is None
    assert diplomacy.participant_kind("legor@ogamex.local", "Legor") is None
    assert diplomacy.participant_kind("generated@example.com", "Generated") is None
    assert diplomacy.participant_kind("player@naver.com", "Player") == "human"


def test_diplomacy_policy_rejects_rule_bot_shape_and_bounds_text():
    clean = diplomacy.clean_diplomacy_policy({
        "messages": [
            {
                "command_id": "m1",
                "recipient_id": 10,
                "kind": "message",
                "text": "x" * 1000,
            },
            {
                "command_id": "bad",
                "recipient_id": 0,
                "kind": "message",
                "text": "no",
            },
        ]
    })

    assert len(clean["messages"]) == 1
    assert len(clean["messages"][0]["text"]) == diplomacy.MAX_MESSAGE_CHARS


def test_nap_requires_explicit_counterparty_acceptance_and_then_blocks_attack(
    tmp_path, monkeypatch,
):
    ledger = tmp_path / "ledger.json"
    client_a = _Client()
    client_b = _Client()

    def peers(email):
        if email == "ai01@bots.local":
            return [{
                "player_id": 2,
                "username": "AI02",
                "email": "ai02@bots.local",
                "kind": "ai",
            }]
        return [{
            "player_id": 1,
            "username": "AI01",
            "email": "ai01@bots.local",
            "kind": "ai",
        }]

    monkeypatch.setattr(diplomacy, "eligible_participants", peers)
    proposal = {
        "messages": [{
            "command_id": "nap-propose-1",
            "recipient_id": 2,
            "kind": "propose_nap",
            "contract_id": "nap-ai01-ai02-1",
            "duration_hours": 24,
            "text": "서로 공격하지 말자.",
        }]
    }
    outcomes, _ = diplomacy.execute_policy(
        client_a, "ai01@bots.local", proposal, now=1000, ledger_path=ledger
    )
    assert outcomes[0]["status"] == "completed"
    assert json.loads(ledger.read_text())["contracts"][0]["status"] == "pending"

    acceptance = {
        "messages": [{
            "command_id": "nap-accept-1",
            "recipient_id": 1,
            "kind": "accept_nap",
            "contract_id": "nap-ai01-ai02-1",
            "text": "수락한다.",
        }]
    }
    outcomes, _ = diplomacy.execute_policy(
        client_b, "ai02@bots.local", acceptance, now=1100, ledger_path=ledger
    )
    assert outcomes[0]["status"] == "completed"
    stored = json.loads(ledger.read_text())
    assert stored["contracts"][0]["status"] == "active"

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, _sql, _params):
            pass

        def fetchall(self):
            return [{"id": 2}]

    class _Conn:
        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(diplomacy.db_reader, "_conn", lambda: _Conn())
    assert diplomacy.blocked_player_ids(
        "ai01@bots.local", now=1200, ledger_path=ledger
    ) == {2}
