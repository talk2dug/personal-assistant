"""Verifies HomeAssistantClient's REST calls and response shaping, with httpx
mocked out — no real Home Assistant instance needed for these."""
import httpx
import pytest

from assistant.core.home_assistant_client import HomeAssistantClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


@pytest.fixture
def client():
    return HomeAssistantClient("http://ha.local", "test-token")


def test_list_entities_filters_by_domain(client, monkeypatch):
    states = [
        {"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "Living Room"}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    ]
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(200, states))

    result = client.list_entities(domain="light")

    assert result["entities"] == [{"entity_id": "light.living_room", "name": "Living Room", "state": "on"}]


def test_list_entities_no_filter_returns_all(client, monkeypatch):
    states = [{"entity_id": "sensor.temp", "state": "72", "attributes": {}}]
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(200, states))

    result = client.list_entities()

    assert len(result["entities"]) == 1


def test_get_entity_state_returns_attributes(client, monkeypatch):
    payload = {"entity_id": "climate.living_room", "state": "heat", "attributes": {"temperature": 70}}
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(200, payload))

    result = client.get_entity_state("climate.living_room")

    assert result == {"entity_id": "climate.living_room", "state": "heat", "attributes": {"temperature": 70}}


def test_get_entity_state_missing_entity_returns_error(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(404))

    result = client.get_entity_state("light.nonexistent")

    assert "error" in result


def test_call_service_posts_entity_and_data(client, monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(200, [])

    monkeypatch.setattr(httpx, "post", fake_post)

    result = client.call_service("climate", "set_temperature", "climate.living_room", {"temperature": 72})

    assert captured["url"] == "http://ha.local/api/services/climate/set_temperature"
    assert captured["json"] == {"entity_id": "climate.living_room", "temperature": 72}
    assert result["ok"] is True


def test_call_tool_dispatches_by_name(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(200, []))
    assert client.call_tool("list_entities", {})["entities"] == []
    assert "error" in client.call_tool("bogus_tool", {})
