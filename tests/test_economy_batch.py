from __future__ import annotations

import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared import economy_batch as eb


class _Response:
    def __init__(self, status: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text
        self.url = "http://localhost/ajax/bot/v1/economy-batch"

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Session:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes: list[object], email: str = "ai01@bots.local") -> None:
        self.email = email
        self.base_url = "http://localhost"
        self.session = _Session(outcomes)
        self._token = "csrf-a"
        self.recoveries: list[bool] = []

    def _csrf_token(self, _path):
        return "csrf-new"

    def _recover_auth(self, *, force_relogin=False):
        self.recoveries.append(force_relogin)
        self._token = "csrf-b"

    @staticmethod
    def _is_login_response(_response):
        return False


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("OGAME_ECONOMY_BATCH_ENABLED", "1")
    monkeypatch.setenv("OGAME_ECONOMY_BATCH_ACCOUNTS", "ai01@bots.local")
    eb.clear_capability_cache()
    yield
    eb.clear_capability_cache()


def _actions():
    return [
        {
            "action_id": "a0",
            "kind": "build",
            "planet_id": 7,
            "technology_id": 1,
            "expected_cost": {"metal": 60, "crystal": 15, "deuterium": 0},
        }
    ]


def test_rollout_requires_master_flag_and_explicit_account(monkeypatch):
    assert eb.enabled_for_account("ai01@bots.local")
    assert not eb.enabled_for_account("ai02@bots.local")
    monkeypatch.setenv("OGAME_ECONOMY_BATCH_ACCOUNTS", "*")
    assert eb.enabled_for_account("bot090@bots.local")
    monkeypatch.setenv("OGAME_ECONOMY_BATCH_ENABLED", "0")
    assert not eb.enabled_for_account("ai01@bots.local")


def test_success_sends_ordered_contract_and_budget():
    response = {
        "status": "completed",
        "request_id": "fixed-id",
        "results": [{"action_id": "a0", "success": True, "status": "accepted"}],
    }
    client = _Client([_Response(200, response)])
    got = eb.try_economy_batch(
        client,
        _actions(),
        max_success=3,
        budget={"metal": 100, "crystal": 80, "deuterium": 10},
        request_id="fixed-id",
    )
    assert got == response
    sent = client.session.calls[0]["json"]
    assert sent["request_id"] == "fixed-id"
    assert sent["actions"] == _actions()
    assert sent["max_success"] == 3
    assert sent["stop_on_queue_full"] is True
    assert sent["budget"] == {"metal": 100, "crystal": 80, "deuterium": 10}
    assert sent["_token"] == "csrf-a"


def test_feature_hidden_404_is_safe_legacy_fallback_and_cached():
    client = _Client([_Response(404, {"message": "not found"})])
    assert eb.try_economy_batch(client, _actions(), max_success=1) is None
    assert eb.try_economy_batch(client, _actions(), max_success=1) is None
    assert len(client.session.calls) == 1


def test_419_repairs_auth_and_retries_same_id():
    ok = {
        "status": "completed",
        "request_id": "retry-id",
        "results": [],
    }
    client = _Client([_Response(419), _Response(200, ok)])
    got = eb.try_economy_batch(
        client, _actions(), max_success=1, request_id="retry-id"
    )
    assert got == ok
    assert client.recoveries == [False]
    assert [call["json"]["request_id"] for call in client.session.calls] == [
        "retry-id", "retry-id"
    ]
    assert client.session.calls[1]["json"]["_token"] == "csrf-b"


def test_json_401_forces_relogin_and_retries_same_id():
    ok = {
        "status": "completed",
        "request_id": "auth-id",
        "results": [],
    }
    client = _Client([_Response(401), _Response(200, ok)])

    got = eb.try_economy_batch(
        client, _actions(), max_success=1, request_id="auth-id"
    )

    assert got == ok
    assert client.recoveries == [True]
    assert [call["json"]["request_id"] for call in client.session.calls] == [
        "auth-id", "auth-id"
    ]
    assert client.session.calls[1]["json"]["_token"] == "csrf-b"


def test_controller_validation_422_is_safe_legacy_fallback():
    client = _Client([
        _Response(422, {
            "status": "invalid_request",
            "code": "validation_failed",
            "message": "The batch request is invalid.",
            "errors": {"actions": ["too many"]},
        })
    ])

    assert eb.try_economy_batch(
        client, _actions(), max_success=1, request_id="invalid-id"
    ) is None
    assert len(client.session.calls) == 1


def test_unknown_422_remains_ambiguous_and_is_not_replayed():
    client = _Client([_Response(422, {"status": "other"}, text="other")])
    with pytest.raises(eb.EconomyBatchError, match="HTTP 422"):
        eb.try_economy_batch(
            client, _actions(), max_success=1, request_id="unknown-422"
        )
    assert len(client.session.calls) == 1


def test_transport_retry_reuses_id_and_never_silently_falls_back():
    err = requests.ConnectionError("down")
    client = _Client([err, requests.ConnectionError("still down")])
    with pytest.raises(eb.EconomyBatchError, match="transport failed"):
        eb.try_economy_batch(
            client, _actions(), max_success=1, request_id="network-id"
        )
    assert len(client.session.calls) == 2
    assert all(
        call["json"]["request_id"] == "network-id"
        for call in client.session.calls
    )
