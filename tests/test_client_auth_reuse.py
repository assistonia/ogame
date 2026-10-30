from __future__ import annotations

import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared import client as client_mod


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        url: str = "http://game.test/overview",
        text: str = "",
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.text = text
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _ScriptedSession:
    def __init__(self, get, post) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.get_handler = get
        self.post_handler = post
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_handler(url, kwargs)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_handler(url, kwargs)

    def close(self) -> None:
        pass


def _client(tmp_path, monkeypatch) -> client_mod.OGameClient:
    monkeypatch.setattr(client_mod, "SESSIONS_DIR", tmp_path)
    return client_mod.OGameClient("bot001@bots.local", "pw", base_url="http://game.test")


def test_healthy_ensure_login_does_not_get_overview_every_lease(tmp_path, monkeypatch):
    def get(url, _kwargs):
        assert url.endswith("/overview")
        return _Response(url=url)

    session = _ScriptedSession(get, lambda *_: AssertionError("unexpected POST"))
    session.cookies.set("session", "valid")
    client = _client(tmp_path, monkeypatch)
    client.session = session

    client.ensure_login()
    client.ensure_login()

    assert [url for url, _ in session.get_calls] == ["http://game.test/overview"]
    assert client._authenticated is True


def test_validation_overview_token_is_reused_by_first_action(tmp_path, monkeypatch):
    def get(url, _kwargs):
        assert url.endswith("/overview")
        return _Response(
            url=url,
            text='<meta name="csrf-token" content="overview-token">',
        )

    def post(url, kwargs):
        assert url.endswith("/resources/add-buildrequest")
        assert kwargs["data"]["_token"] == "overview-token"
        return _Response(url=url, payload={"status": "success"})

    session = _ScriptedSession(get, post)
    session.cookies.set("session", "valid")
    client = _client(tmp_path, monkeypatch)
    client.session = session

    client.ensure_login()
    assert client.build(1) == {"status": "success"}
    assert [url for url, _ in session.get_calls] == ["http://game.test/overview"]


def test_419_revalidates_session_clears_both_tokens_and_retries_once(tmp_path, monkeypatch):
    posts = iter([
        _Response(419, url="http://game.test/resources/add-buildrequest"),
        _Response(
            200,
            url="http://game.test/resources/add-buildrequest",
            payload={"status": "success"},
        ),
    ])

    def get(url, _kwargs):
        if url.endswith("/overview"):
            return _Response(url=url)
        if url.endswith("/resources"):
            return _Response(url=url, text='<meta name="csrf-token" content="fresh">')
        raise AssertionError(url)

    session = _ScriptedSession(get, lambda _url, _kwargs: next(posts))
    session.cookies.set("session", "valid")
    client = _client(tmp_path, monkeypatch)
    client.session = session
    client._authenticated = True
    client._token = "stale"
    client._fleet_csrf = "stale-fleet"

    result = client.build(1)

    assert result == {"status": "success"}
    assert [url.rsplit("/", 1)[-1] for url, _ in session.get_calls] == [
        "overview", "resources",
    ]
    assert session.post_calls[0][1]["data"]["_token"] == "stale"
    assert session.post_calls[1][1]["data"]["_token"] == "fresh"
    assert client._fleet_csrf is None
    assert client._authenticated is True


def test_login_redirect_forces_full_relogin_before_single_retry(tmp_path, monkeypatch):
    action_attempt = 0

    def get(url, _kwargs):
        if url.endswith("/login"):
            return _Response(
                url=url,
                text='<input name="_token" value="login-token">',
            )
        if url.endswith("/resources"):
            return _Response(url=url, text='<meta name="csrf-token" content="fresh">')
        raise AssertionError(url)

    def post(url, _kwargs):
        nonlocal action_attempt
        if url.endswith("/login"):
            session.cookies.set("session", "renewed")
            return _Response(url="http://game.test/overview")
        if url.endswith("/resources/add-buildrequest"):
            action_attempt += 1
            if action_attempt == 1:
                return _Response(url="http://game.test/login")
            return _Response(url=url, payload={"status": "success"})
        raise AssertionError(url)

    session = _ScriptedSession(get, post)
    session.cookies.set("session", "expired")
    client = _client(tmp_path, monkeypatch)
    client.session = session
    client._authenticated = True
    client._token = "stale"

    result = client.build(1)

    assert result == {"status": "success"}
    assert [url for url, _ in session.post_calls] == [
        "http://game.test/resources/add-buildrequest",
        "http://game.test/login",
        "http://game.test/resources/add-buildrequest",
    ]
    assert session.cookies.get("session") == "renewed"
    assert client._authenticated is True


def test_due_settlement_json_401_repairs_auth_and_retries_get(tmp_path, monkeypatch):
    responses = iter([
        _Response(401, url="http://game.test/overview"),
        _Response(
            200,
            url="http://game.test/overview",
            text='<meta name="csrf-token" content="settled-token">',
        ),
    ])
    session = _ScriptedSession(lambda _url, _kwargs: next(responses), lambda *_: None)
    client = _client(tmp_path, monkeypatch)
    client.session = session
    client._authenticated = True
    recoveries = []

    def recover(*, force_relogin=False):
        recoveries.append(force_relogin)
        client._authenticated = True
        client._invalidate_csrf_tokens()

    client._recover_auth = recover

    response = client.settle_overview(max_attempts=2)

    assert response.status_code == 200
    assert len(session.get_calls) == 2
    assert recoveries == [True]
    assert client._token == "settled-token"


def test_due_settlement_419_revalidates_without_forced_login(tmp_path, monkeypatch):
    responses = iter([
        _Response(419, url="http://game.test/overview"),
        _Response(200, url="http://game.test/overview"),
    ])
    session = _ScriptedSession(lambda _url, _kwargs: next(responses), lambda *_: None)
    client = _client(tmp_path, monkeypatch)
    client.session = session
    recoveries = []
    client._recover_auth = lambda *, force_relogin=False: recoveries.append(force_relogin)

    assert client.settle_overview(max_attempts=2).status_code == 200
    assert recoveries == [False]
    assert len(session.get_calls) == 2


def test_due_settlement_safely_retries_transport_and_temporary_5xx(
    tmp_path, monkeypatch
):
    outcomes = iter([
        requests.ConnectionError("reset"),
        _Response(503, url="http://game.test/overview"),
        _Response(200, url="http://game.test/overview"),
    ])

    def get(_url, _kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    session = _ScriptedSession(get, lambda *_: None)
    client = _client(tmp_path, monkeypatch)
    client.session = session

    assert client.settle_overview(max_attempts=3).status_code == 200
    assert len(session.get_calls) == 3


def test_direct_chat_uses_authenticated_endpoint_and_server_contract(
    tmp_path, monkeypatch,
):
    def get(url, _kwargs):
        assert url.endswith("/chat")
        return _Response(
            url=url,
            text='<meta name="csrf-token" content="chat-token">',
        )

    def post(url, kwargs):
        assert url.endswith("/chat/send")
        assert kwargs["data"] == {
            "_token": "chat-token",
            "mode": 1,
            "playerId": 17,
            "text": "안녕",
            "ajax": 1,
        }
        return _Response(url=url, payload={"status": "OK", "id": 9})

    client = _client(tmp_path, monkeypatch)
    client.session = _ScriptedSession(get, post)

    assert client.send_direct_message(17, "안녕") == {"status": "OK", "id": 9}
