"""Covers /api/weather — a direct proxy to HomeAssistantClient.get_weather, the same
call the get_weather chat tool already dispatches to, just reachable without an LLM
turn (a page refreshing its own weather panel shouldn't need one).
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db
from assistant.core.engine import HomeAssistantContext
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


class FakeHAClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get_weather(self, forecast_type="daily"):
        self.calls.append(forecast_type)
        return self.result


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
    ])


def _login(client):
    r = client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert r.status_code == 200
    return client


def test_requires_login(cfg):
    client = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert client.get("/api/weather").status_code == 401


def test_returns_503_when_home_assistant_is_not_configured(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    assert client.get("/api/weather").status_code == 503


def test_proxies_real_weather_data(cfg):
    ha_client = FakeHAClient({"entity_id": "weather.home", "condition": "sunny", "temperature": 72.0,
                             "temperature_unit": "°F", "forecast_type": "daily", "forecast": []})
    ha_context = HomeAssistantContext(mcp_client=ha_client, sensitive_domains=set())
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
                                          home_assistant=ha_context)))
    resp = client.get("/api/weather")
    assert resp.status_code == 200
    assert resp.json()["condition"] == "sunny"
    assert ha_client.calls == ["daily"]


def test_forecast_type_query_param_is_passed_through(cfg):
    ha_client = FakeHAClient({"condition": "clear", "forecast": []})
    ha_context = HomeAssistantContext(mcp_client=ha_client, sensitive_domains=set())
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
                                          home_assistant=ha_context)))
    client.get("/api/weather?forecast_type=hourly")
    assert ha_client.calls == ["hourly"]


def test_an_error_from_home_assistant_becomes_a_502_not_a_500(cfg):
    ha_client = FakeHAClient({"error": "no weather entity is configured in Home Assistant"})
    ha_context = HomeAssistantContext(mcp_client=ha_client, sensitive_domains=set())
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
                                          home_assistant=ha_context)))
    resp = client.get("/api/weather")
    assert resp.status_code == 502
