"""Unit tests for OmadaClient: OAuth2 client-credentials token acquisition/refresh, the
AccessToken= header quirk, and the call_tool dispatch every other integration in this repo
(GitOpsClient, etc.) follows. All HTTP is mocked via a custom httpx transport -- this must
never make a real network call.
"""
import httpx
import pytest

from assistant.core.omada_client import OmadaClient, OmadaError


class ScriptedTransport(httpx.BaseTransport):
    """Returns whatever the next queued response is, regardless of the request, and
    records every request made -- lets a test script "first call fails like an expired
    token, second call (the refresh) succeeds" without guessing exact call counts."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.responses.pop(0) if self.responses else {"errorCode": 0, "result": {}}
        return httpx.Response(200, json=body)


def make_client(responses: list[dict]) -> tuple[OmadaClient, ScriptedTransport]:
    client = OmadaClient(
        "https://192.168.0.100", "cid", "secret", "omada123", "site456", db_path=None,
    )
    transport = ScriptedTransport(responses)
    client._http = httpx.Client(base_url=client.controller_url, transport=transport)
    return client, transport


TOKEN_OK = {
    "errorCode": 0, "msg": "Success",
    "result": {"accessToken": "tok-1", "refreshToken": "refresh-1", "tokenType": "bearer", "expiresIn": 7200},
}


def test_authenticate_stores_tokens_and_uses_accesstoken_header_not_bearer():
    client, transport = make_client([TOKEN_OK, {"errorCode": 0, "result": {"data": []}}])
    client.list_devices()

    auth_header = transport.requests[1].headers["authorization"]
    assert auth_header == "AccessToken=tok-1"


def test_authentication_failure_raises_omada_error_with_the_real_message():
    client, _ = make_client([{"errorCode": -44106, "msg": "Client Id Or Client Secret is Invalid"}])
    with pytest.raises(OmadaError, match="Client Id Or Client Secret is Invalid"):
        client.list_devices()


def test_expired_access_token_triggers_exactly_one_transparent_refresh():
    client, transport = make_client([
        TOKEN_OK,
        {"errorCode": -44112, "msg": "token expired"},
        {"errorCode": 0, "result": {"accessToken": "tok-2", "refreshToken": "refresh-2", "expiresIn": 7200}},
        {"errorCode": 0, "result": {"data": [{"mac": "AA", "name": "ap1", "status": 1}]}},
    ])
    devices = client.list_devices()

    assert devices == [{"mac": "AA", "name": "ap1", "status": 1}]
    # auth, first (expired) attempt, refresh, retry -- never a second uncontrolled loop.
    assert len(transport.requests) == 4
    assert transport.requests[-1].headers["authorization"] == "AccessToken=tok-2"


def test_dead_refresh_token_falls_back_to_a_fresh_client_credentials_grant():
    client, transport = make_client([
        TOKEN_OK,
        {"errorCode": -44112, "msg": "token expired"},
        {"errorCode": -44111, "msg": "refresh token invalid"},
        TOKEN_OK,
        {"errorCode": 0, "result": {"data": []}},
    ])
    client.list_devices()
    assert len(transport.requests) == 5


def test_a_non_expiry_error_raises_immediately_without_retrying():
    client, transport = make_client([TOKEN_OK, {"errorCode": -1, "msg": "something else broke"}])
    with pytest.raises(OmadaError, match="something else broke"):
        client.list_devices()
    assert len(transport.requests) == 2


def test_list_devices_returns_the_data_array():
    client, _ = make_client([TOKEN_OK, {"errorCode": 0, "result": {"data": [{"mac": "AA"}, {"mac": "BB"}]}}])
    assert client.list_devices() == [{"mac": "AA"}, {"mac": "BB"}]


def test_list_clients_returns_the_data_array():
    client, _ = make_client([TOKEN_OK, {"errorCode": 0, "result": {"data": [{"mac": "CC"}]}}])
    assert client.list_clients() == [{"mac": "CC"}]


def test_reboot_device_posts_the_mac_in_a_list():
    client, transport = make_client([TOKEN_OK, {"errorCode": 0, "result": {}}])
    client.reboot_device("AA:BB:CC")
    import json
    body = json.loads(transport.requests[-1].content)
    assert body == {"macList": ["AA:BB:CC"]}


def test_call_tool_list_omada_devices():
    client, _ = make_client([TOKEN_OK, {"errorCode": 0, "result": {"data": [{"mac": "AA"}]}}])
    assert client.call_tool("list_omada_devices", {}) == {"devices": [{"mac": "AA"}]}


def test_call_tool_list_omada_clients():
    client, _ = make_client([TOKEN_OK, {"errorCode": 0, "result": {"data": [{"mac": "BB"}]}}])
    assert client.call_tool("list_omada_clients", {}) == {"clients": [{"mac": "BB"}]}


def test_call_tool_reboot_requires_a_mac():
    client, _ = make_client([])
    result = client.call_tool("reboot_omada_device", {})
    assert result == {"ok": False, "error": "mac is required"}


def test_call_tool_reboot_with_a_mac_actually_calls_reboot(monkeypatch):
    client, _ = make_client([])
    calls = []
    monkeypatch.setattr(client, "reboot_device", lambda mac: calls.append(mac) or {"ok": True})
    result = client.call_tool("reboot_omada_device", {"mac": "AA:BB"})
    assert calls == ["AA:BB"]
    assert result == {"ok": True, "result": {"ok": True}}


def test_call_tool_network_health_without_db_path_returns_an_error():
    client, _ = make_client([])
    assert client.db_path is None
    result = client.call_tool("get_omada_network_health", {})
    assert "error" in result


def test_call_tool_unknown_name_returns_an_error():
    client, _ = make_client([])
    result = client.call_tool("not_a_real_tool", {})
    assert "unknown omada tool" in result["error"]
