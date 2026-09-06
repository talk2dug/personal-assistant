"""Covers the weather and travel-time tools, and the backend-aware tool routing.

The routing test pins a real capability gap this replaced: Jarvis could not answer
"what's the weather" despite Home Assistant exposing a live weather entity, purely
because "weather" was missing from HOME_ASSISTANT_KEYWORDS.
"""
import httpx
import pytest

from assistant.core.engine import (
    HOME_ASSISTANT_TOOLS, MAIL_TOOLS, HomeAssistantContext, MailContext, build_system_prompt,
    select_tools,
)
from assistant.core.home_assistant_client import HomeAssistantClient

STATES = [
    {"entity_id": "weather.forecast_home", "state": "partlycloudy",
     "attributes": {"friendly_name": "Forecast Home", "temperature": 93, "temperature_unit": "°F",
                    "humidity": 53, "wind_speed": 4.04, "wind_speed_unit": "mph", "uv_index": 1.7,
                    "pressure": 29.94}},
    {"entity_id": "sensor.morning_commute", "state": "27",
     "attributes": {"friendly_name": "Morning commute", "unit_of_measurement": "min",
                    "route": "I-40 W", "distance": 18.2, "duration": 27}},
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}},
]

FORECAST = {"service_response": {"weather.forecast_home": {"forecast": [
    {"datetime": "2026-09-04T16:00:00+00:00", "condition": "partlycloudy", "temperature": 98, "templow": 77},
    {"datetime": "2026-09-05T16:00:00+00:00", "condition": "rainy", "temperature": 88, "templow": 72},
]}}}


@pytest.fixture
def client(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("/api/states"):
            return httpx.Response(200, json=STATES, request=httpx.Request("GET", url))
        entity_id = url.rsplit("/", 1)[-1]
        match = next((e for e in STATES if e["entity_id"] == entity_id), None)
        if match is None:
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        return httpx.Response(200, json=match, request=httpx.Request("GET", url))

    def fake_post(url, **kwargs):
        return httpx.Response(200, json=FORECAST, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return HomeAssistantClient("http://ha.local", "token")


def test_weather_finds_the_entity_and_returns_conditions_plus_forecast(client):
    result = client.call_tool("get_weather", {})
    assert result["entity_id"] == "weather.forecast_home"
    assert result["condition"] == "partlycloudy"
    assert result["temperature"] == 93
    assert [f["condition"] for f in result["forecast"]] == ["partlycloudy", "rainy"]


def test_weather_survives_a_forecast_failure(client, monkeypatch):
    """Current conditions are still worth having — a forecast outage shouldn't turn
    'it's 93 degrees' into an error."""
    def boom(url, **kwargs):
        raise httpx.ConnectError("forecast service down")

    monkeypatch.setattr(httpx, "post", boom)
    result = client.call_tool("get_weather", {})
    assert result["temperature"] == 93
    assert "error" in result["forecast"][0]


def test_weather_reports_honestly_when_no_entity_exists(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=[STATES[2]], request=httpx.Request("GET", url)))
    client = HomeAssistantClient("http://ha.local", "token")
    assert "error" in client.call_tool("get_weather", {})


def test_travel_time_discovers_route_sensors(client):
    result = client.call_tool("get_travel_time", {})
    assert len(result["routes"]) == 1
    route = result["routes"][0]
    assert route["name"] == "Morning commute"
    assert route["minutes"] == "27"
    assert route["route"] == "I-40 W"
    # A plain light must not be mistaken for a route.
    assert all("light" not in r["entity_id"] for r in result["routes"])


def test_travel_time_tells_the_model_not_to_invent_a_number(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=[STATES[2]], request=httpx.Request("GET", url)))
    client = HomeAssistantClient("http://ha.local", "token")
    result = client.call_tool("get_travel_time", {})
    assert result["routes"] == []
    assert "never" in result["note"].lower() or "rather than estimating" in result["note"].lower()


def test_unrouted_catalog_exposes_weather_regardless_of_wording():
    """The actual regression: 'what's the weather' matches no HA keyword, so routing
    hid the weather tool entirely."""
    ha = HomeAssistantContext(mcp_client=object(), sensitive_domains=set())
    routed = select_tools("what's the weather like?", home_assistant=ha, route=True)
    unrouted = select_tools("what's the weather like?", home_assistant=ha, route=False)

    assert "get_weather" not in [t["function"]["name"] for t in routed]
    assert "get_weather" in [t["function"]["name"] for t in unrouted]


def test_unrouted_catalog_includes_every_configured_integration():
    ha = HomeAssistantContext(mcp_client=object(), sensitive_domains=set())
    mail = MailContext(mcp_client=object(), sensitive_tools=set())
    names = [t["function"]["name"] for t in select_tools("hello", home_assistant=ha, mail=mail, route=False)]

    for tool in HOME_ASSISTANT_TOOLS + MAIL_TOOLS:
        assert tool["function"]["name"] in names
    assert "add_reminder" in names


def test_routing_still_applies_for_the_local_model():
    """Ollama still needs the filtered catalog — it falls apart on large tool sets."""
    mail = MailContext(mcp_client=object(), sensitive_tools=set())
    names = [t["function"]["name"] for t in select_tools("turn on the lights", mail=mail, route=True)]
    assert "send_email" not in names


def test_web_search_note_only_appears_when_the_backend_has_it():
    assert "search the web" not in build_system_prompt("America/New_York").lower()
    assert "search the web" in build_system_prompt("America/New_York", web_search=True).lower()
