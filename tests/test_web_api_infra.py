"""Covers /api/infra/ssh-hosts -- the live reachability panel for every registered SSH
host, not just Jarvis's own machines. socket.create_connection is mocked so these never
touch the real network; ssh_health.py's own tests cover the reachability logic itself.
"""
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    ssh_hosts: dict = field(default_factory=dict)
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


def _client(db_path, ssh_hosts=None, login_as="Dug"):
    cfg = FakeConfig(db_path=db_path, ssh_hosts=ssh_hosts or {}, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    pw = "pw" if login_as == "Dug" else "pw2"
    assert c.post("/api/login", json={"name": login_as, "password": pw}).status_code == 200
    return c


def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/infra/ssh-hosts").status_code == 401


def test_partner_is_refused_owner_only_access(db_path):
    c = _client(db_path, ssh_hosts={"jarvisbox": {"host": "127.0.0.1"}}, login_as="Partner")
    assert c.get("/api/infra/ssh-hosts").status_code == 403


def test_no_hosts_registered_returns_an_empty_list_not_an_error(db_path):
    c = _client(db_path, ssh_hosts={})
    resp = c.get("/api/infra/ssh-hosts")
    assert resp.status_code == 200
    assert resp.json() == {"hosts": []}


def test_returns_reachability_for_every_registered_host(db_path):
    hosts = {
        "jarvisbox": {"host": "127.0.0.1", "is_jarvis_host": True, "purpose": "Runs Jarvis."},
        "homeassistant": {"host": "192.168.0.32", "is_jarvis_host": False, "purpose": "HA server."},
    }
    c = _client(db_path, ssh_hosts=hosts)
    with patch("socket.create_connection", side_effect=OSError("unreachable")):
        resp = c.get("/api/infra/ssh-hosts")
    assert resp.status_code == 200
    by_name = {h["name"]: h for h in resp.json()["hosts"]}
    assert set(by_name) == {"jarvisbox", "homeassistant"}
    assert by_name["jarvisbox"]["is_jarvis_host"] is True
    assert by_name["jarvisbox"]["reachable"] is False
    assert by_name["homeassistant"]["purpose"] == "HA server."
