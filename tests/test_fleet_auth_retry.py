from __future__ import annotations

import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared import fleet


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        url: str = "http://game.test/ajax/fleet/dispatch/check-target",
        payload: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class _Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes):
        self.base_url = "http://game.test"
        self.session = _Session(outcomes)
        self._token = "stale-token"
        self._fleet_csrf = "stale-token"
        self.recoveries = []

    @staticmethod
    def _is_login_response(response):
        return response.url.rstrip("/").endswith("/login")

    def _recover_auth(self, *, force_relogin=False):
        self.recoveries.append(force_relogin)
        self._token = "fresh-token"
        self._fleet_csrf = None


@pytest.mark.parametrize(("status", "force_relogin"), [(401, True), (419, False)])
def test_fleet_post_retries_only_explicit_auth_rejection(status, force_relogin):
    client = _Client([
        _Response(status),
        _Response(200, payload={"status": "success"}),
    ])

    result = fleet._post(client, "/ajax/fleet/dispatch/check-target", {"galaxy": 1})

    assert result == {"status": "success"}
    assert client.recoveries == [force_relogin]
    assert len(client.session.calls) == 2
    assert client.session.calls[0]["data"]["_token"] == "stale-token"
    assert client.session.calls[1]["data"]["_token"] == "fresh-token"


def test_fleet_post_login_redirect_forces_relogin_once():
    client = _Client([
        _Response(200, url="http://game.test/login"),
        _Response(200, payload={"success": True}),
    ])

    assert fleet._post(client, "/ajax/galaxy", {"galaxy": 1}) == {"success": True}
    assert client.recoveries == [True]
    assert len(client.session.calls) == 2


def test_fleet_post_does_not_replay_ambiguous_5xx_or_transport_failure():
    server_error = _Client([_Response(503)])
    with pytest.raises(requests.HTTPError):
        fleet._post(server_error, "/ajax/fleet/dispatch/send-fleet", {"mission": 1})
    assert len(server_error.session.calls) == 1
    assert server_error.recoveries == []

    transport_error = _Client([requests.ConnectionError("reset")])
    with pytest.raises(requests.ConnectionError):
        fleet._post(
            transport_error, "/ajax/fleet/dispatch/send-fleet", {"mission": 1}
        )
    assert len(transport_error.session.calls) == 1
    assert transport_error.recoveries == []
