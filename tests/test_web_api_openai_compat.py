"""Verifies the /v1/chat/completions shim Home Assistant's OpenAI Conversation
integration talks to: bearer-token auth, extracting the latest user message, and
returning an OpenAI-shaped response (both non-streaming and streaming)."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    ha_conversation_api_key: str = "the-secret-key"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "Hello from Jarvis"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
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
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    return TestClient(app)


def test_missing_auth_header_rejected(client):
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401


def test_wrong_key_rejected(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401


def test_valid_request_returns_openai_shaped_reply(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"Authorization": "Bearer the-secret-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello from Jarvis"


def test_extracts_latest_user_message_from_history(client):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "You are an assistant."},
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "second message"},
            ],
            "stream": False,
        },
        headers={"Authorization": "Bearer the-secret-key"},
    )
    assert resp.status_code == 200
    # FakeLLM always replies the same regardless of input, but a real handle_message call
    # having succeeded (200, not 500) confirms the last user message was found and used.
    assert resp.json()["choices"][0]["message"]["content"] == "Hello from Jarvis"


def test_models_endpoint_lists_jarvis(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "jarvis"


def test_streaming_response_returns_sse(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": "Bearer the-secret-key"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data: [DONE]" in resp.text


def test_ha_endpoint_gets_the_same_contexts_as_every_other_surface(cfg, monkeypatch):
    """The HA voice Jarvis must not be a downgraded Jarvis.

    handle_message takes each capability as its own keyword argument, so forgetting one
    here does not fail — it quietly produces an assistant that shares the conversation
    history but cannot act on it. That is exactly how the business context (hiring, staff,
    the crypto feed) went missing from the voice assistant while working everywhere else.

    Rather than pinning today's list, this asserts against handle_message's own signature,
    so a context added later and not wired in here fails the test instead of shipping.
    """
    import inspect

    from assistant.core import engine
    from assistant.web.routes import openai_compat

    seen = {}

    def fake_handle_message(db_path, llm, user_id, text, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(openai_compat, "handle_message", fake_handle_message)

    # Everything handle_message accepts that is a capability context, not a knob.
    non_contexts = {"db_path", "llm", "requesting_user_id", "user_text", "tz_name",
                    "image_bytes", "max_tool_hops"}
    contexts = [p for p in inspect.signature(engine.handle_message).parameters
                if p not in non_contexts]

    db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    sentinels = {name: object() for name in contexts}
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    for name, value in sentinels.items():
        setattr(app.state, name, value)

    resp = TestClient(app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"Authorization": "Bearer the-secret-key"},
    )
    assert resp.status_code == 200

    missing = [name for name in contexts if name not in seen]
    assert not missing, (
        f"the Home Assistant endpoint does not pass {missing} to handle_message — "
        f"voice Jarvis would be missing those abilities. Add them to the call in "
        f"assistant/web/routes/openai_compat.py."
    )
    wrong = [n for n in contexts if seen.get(n) is not sentinels[n]]
    assert not wrong, f"passed something other than app.state for {wrong}"
