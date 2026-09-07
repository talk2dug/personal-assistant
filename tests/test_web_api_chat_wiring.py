"""Regression test for a real bug: home_assistant was wired into app.state and every
other transport, but never actually passed through to handle_message from the web
chat route — so /api/chat/message silently ran with home_assistant=None the whole
time despite the integration otherwise working. Asserts every optional context
app.state carries actually reaches handle_message's call, by inspecting the real
kwargs handle_message is invoked with rather than just checking the HTTP response
shape (which can't tell None apart from a real object)."""
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, vision
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "Hello from Jarvis"}


class Sentinel:
    """A distinct dummy object per context so identity comparison proves the exact
    instance from app.state made it through, not just any truthy value."""


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    # /api/chat/message unconditionally checks for a pending show_camera result.
    vision.init_vision_db(path)
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(
        db_path=db_path,
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass")],
    )


@pytest.fixture
def client(cfg):
    db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    era, phone, mail, obsidian, home_assistant = (Sentinel(), Sentinel(), Sentinel(), Sentinel(), Sentinel())
    app = create_app(
        cfg, FakeLLM(), era=era, calendar=None, phone=phone, mail=mail, obsidian=obsidian,
        home_assistant=home_assistant, static_dir=None,
    )
    test_client = TestClient(app)
    resp = test_client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert resp.status_code == 200
    return test_client, {"era": era, "phone": phone, "mail": mail, "obsidian": obsidian, "home_assistant": home_assistant}


def test_every_owner_context_reaches_handle_message(client):
    test_client, contexts = client
    with patch("assistant.web.routes.chat.handle_message", return_value="reply") as mock_handle:
        resp = test_client.post("/api/chat/message", json={"text": "hi"})
    assert resp.status_code == 200
    kwargs = mock_handle.call_args.kwargs
    for name, expected in contexts.items():
        assert kwargs.get(name) is expected, f"{name} did not reach handle_message (got {kwargs.get(name)!r})"
