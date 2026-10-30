from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import miner_bot, production_capacity
from bots.shared import db_reader, economy_batch


class _Client:
    def __init__(self) -> None:
        self.email = "ai01@bots.local"
        self.legacy: list[tuple] = []

    def build(self, technology_id):
        self.legacy.append(("build", technology_id))
        return {"status": "success"}

    def build_facility(self, technology_id):
        self.legacy.append(("facility", technology_id))
        return {"status": "success"}

    def research(self, technology_id):
        self.legacy.append(("research", technology_id))
        return {"status": "success"}

    def build_ships(self, technology_id, amount):
        self.legacy.append(("ship", technology_id, amount))
        return {"status": "success"}


RS = {
    "planet_id": 77,
    "metal": 1_000_000,
    "crystal": 1_000_000,
    "deuterium": 1_000_000,
    "metal_max": 2_000_000,
    "crystal_max": 2_000_000,
    "deuterium_max": 2_000_000,
}
LEVELS = {1: 5, 2: 5, 3: 5, 4: 5, 14: 2, 31: 2, 113: 1}


def _enable(monkeypatch):
    monkeypatch.setattr(economy_batch, "enabled_for_account", lambda _email: True)
    monkeypatch.setattr(miner_bot.storage, "in_backoff", lambda *_a, **_k: False)
    monkeypatch.setattr(miner_bot.storage, "clear_backoff", lambda *_a, **_k: None)
    monkeypatch.setattr(miner_bot.storage, "set_backoff", lambda *_a, **_k: None)


def test_queue_actions_batches_ordered_candidates_and_real_accepts(monkeypatch):
    _enable(monkeypatch)
    captured = {}

    def send(_client, actions, **kwargs):
        captured["actions"] = actions
        captured.update(kwargs)
        return {
            "request_id": "r",
            "results": [
                {"action_id": "queue-0", "success": False, "status": "rejected", "code": "queue_canceled"},
                {"action_id": "queue-1", "success": True, "status": "accepted", "code": "accepted"},
                {"action_id": "queue-2", "success": True, "status": "accepted", "code": "accepted"},
            ],
        }

    monkeypatch.setattr(economy_batch, "try_economy_batch", send)
    monkeypatch.setattr(miner_bot.storage, "classify_cancel", lambda *_a, **_k: "선행조건/기타")
    report = []
    client = _Client()
    candidates = [("build", 1), ("build", 2), ("build", 3)]
    done, _ = miner_bot._queue_actions(
        client,
        candidates,
        2,
        levels=dict(LEVELS),
        resource_state=dict(RS),
        report=report,
        tick_index=9,
    )
    assert done == [("build", 2), ("build", 3)]
    assert client.legacy == []
    assert [item["technology_id"] for item in captured["actions"]] == [1, 2, 3]
    assert all(item["planet_id"] == 77 for item in captured["actions"])
    assert all("expected_cost" in item for item in captured["actions"])
    assert captured["max_success"] == 2
    assert captured["budget"] == {
        "metal": 1_000_000,
        "crystal": 1_000_000,
        "deuterium": 1_000_000,
    }
    assert any("취소:선행조건/기타" in line for line in report)


def test_feature_hidden_404_falls_back_to_legacy_once(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(economy_batch, "try_economy_batch", lambda *_a, **_k: None)
    monkeypatch.setattr(db_reader, "latest_queue_canceled", lambda *_a, **_k: 0)
    client = _Client()
    done, _ = miner_bot._queue_actions(
        client,
        [("build", 1)],
        1,
        levels=dict(LEVELS),
        resource_state=dict(RS),
        tick_index=3,
    )
    assert done == [("build", 1)]
    assert client.legacy == [("build", 1)]


def test_local_prefilter_clamps_max_success_to_payload_size(monkeypatch):
    _enable(monkeypatch)
    captured = {}

    def send(_client, actions, **kwargs):
        captured["actions"] = actions
        captured.update(kwargs)
        return {
            "request_id": "r",
            "results": [
                {"action_id": "queue-1", "success": True, "status": "accepted"}
            ],
        }

    monkeypatch.setattr(economy_batch, "try_economy_batch", send)
    state = dict(RS)
    state["metal"] = 100
    state["crystal"] = 100
    done, _ = miner_bot._queue_actions(
        _Client(),
        [("build", 1), ("build", 2)],
        3,
        levels={**LEVELS, 1: 30, 2: 1},
        resource_state=state,
        tick_index=3,
    )
    assert done == [("build", 2)]
    assert [item["technology_id"] for item in captured["actions"]] == [2]
    assert captured["max_success"] == 1


def test_ambiguous_batch_failure_never_replays_legacy(monkeypatch):
    _enable(monkeypatch)

    def fail(*_a, **_k):
        raise economy_batch.EconomyBatchError("unknown partial")

    monkeypatch.setattr(economy_batch, "try_economy_batch", fail)
    client = _Client()
    done, message = miner_bot._queue_actions(
        client,
        [("build", 1)],
        1,
        levels=dict(LEVELS),
        resource_state=dict(RS),
        tick_index=3,
    )
    assert done == []
    assert "unknown partial" in message
    assert client.legacy == []


def test_try_first_ship_batch_reports_actual_clipped_amount(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(db_reader, "get_resource_state_db", lambda _email: dict(RS))
    monkeypatch.setattr(
        db_reader,
        "get_levels_db",
        lambda _email, **_kwargs: {21: 12, 15: 0, 202: 0, 203: 0},
    )
    monkeypatch.setattr(
        production_capacity,
        "production_queue_snapshot",
        lambda email: production_capacity.ProductionQueueSnapshot(
            available=True,
            email=email,
            planet_id=77,
            observed_at=0,
            economy_speed=25,
            building_queue_depth=0,
            building_queue_seconds=0,
            building_targets=(),
            unit_queue_depth=0,
            unit_queue_seconds=0,
            unit_remaining_amount=0,
            unit_items=(),
        ),
    )

    def send(_client, _actions, **_kwargs):
        return {
            "request_id": "r",
            "results": [
                {
                    "action_id": "ship-0",
                    "success": True,
                    "status": "accepted",
                    "accepted_amount": 4,
                }
            ],
        }

    monkeypatch.setattr(economy_batch, "try_economy_batch", send)
    client = _Client()
    action, message = miner_bot._try_first(
        client, [("ship", 203, 20), ("ship", 202, 10)]
    )
    assert action == ("ship", 203, 4)
    assert message == ""
    assert client.legacy == []
