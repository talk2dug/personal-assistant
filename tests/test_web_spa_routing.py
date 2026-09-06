"""Regression test for a real bug: client-side routes 404'd on a direct load.

The UI is normally entered at / and navigates client-side, so /finance and /review only
exist in the browser's router — never as files. A plain static mount therefore 404s on a
hard refresh, a bookmark, or a kiosk browser booting straight to a deep link. The Pi
voice terminal opens /device cold and hit it immediately.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.core import db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    device_api_key: str | None = "devkey"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None
    generated_media_path: str = "generated"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")

    path = str(tmp_path / "test.db")
    db.init_db(path)
    cfg = FakeConfig(db_path=path)
    return TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=str(dist)))


def test_root_serves_the_app(client):
    resp = client.get("/")
    assert resp.status_code == 200 and "id=root" in resp.text


@pytest.mark.parametrize("route", ["/device", "/finance", "/review", "/agents", "/chat"])
def test_client_side_routes_serve_the_app_on_a_direct_load(client, route):
    """This is the bug: each of these 404'd before, so a hard refresh or a kiosk deep
    link showed raw JSON instead of the UI."""
    resp = client.get(route)
    assert resp.status_code == 200, f"{route} should serve the SPA"
    assert "id=root" in resp.text


def test_real_files_are_still_served_normally(client):
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200 and "console.log" in resp.text


def test_a_missing_api_endpoint_still_404s(client):
    """A missing endpoint must look missing. Returning index.html to something expecting
    JSON turns a clear 404 into a confusing parse error."""
    resp = client.get("/api/definitely-not-a-route")
    assert resp.status_code == 404
    assert "id=root" not in resp.text


def test_device_route_serves_the_app_with_its_query_string(client):
    resp = client.get("/device?id=touch1&key=devkey")
    assert resp.status_code == 200 and "id=root" in resp.text
