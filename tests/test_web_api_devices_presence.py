"""End-to-end through /api/devices/*/turn: an unconfirmed voice terminal gets
personal/financial contexts stripped and runs as a guest user; a terminal whose camera
currently confirms the owner gets the real contexts and the real owner user id.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

import assistant.web.routes.devices as devices_module
from assistant.config import UserConfig
from assistant.core import db, kitchen_db, vision
from assistant.web.app import create_app

SENTINEL_ERA = object()
SENTINEL_PERSONAL = object()
SENTINEL_PHONE = object()


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    ha_conversation_api_key: str | None = None
    device_api_key: str = "device-secret"
    presence_confirm_window_seconds: int = 45
    presence_authorized_access_levels: list = field(default_factory=lambda: ["owner"])
    wake_arbitration_window_ms: int = 50
    wake_arbitration_margin: float = 0.05


class FakeSTT:
    def transcribe(self, audio_bytes):
        return "what's my bank balance"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    kitchen_db.init_kitchen_db(path)  # /turn always checks pending_recipe_views
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
    ])


@pytest.fixture
def client(cfg):
    app = create_app(
        cfg, FakeLLM(), era=SENTINEL_ERA, calendar=None, phone=SENTINEL_PHONE, stt=FakeSTT(),
        personal=SENTINEL_PERSONAL, static_dir=None,
    )
    return TestClient(app)


def _headers(cfg):
    return {"Authorization": f"Bearer {cfg.device_api_key}"}


def _post_turn(client, cfg, device_id):
    return client.post(
        f"/api/devices/{device_id}/turn", headers=_headers(cfg),
        files={"audio": ("u.wav", b"fake-audio-bytes", "audio/wav")},
    )


def test_unconfirmed_terminal_strips_sensitive_context_and_runs_as_guest(client, cfg, monkeypatch):
    captured = {}

    def fake_handle_message(db_path, llm, user_id, text, **kwargs):
        captured["user_id"] = user_id
        captured.update(kwargs)
        return "reply"

    monkeypatch.setattr(devices_module, "handle_message", fake_handle_message)

    resp = _post_turn(client, cfg, "touch1")
    assert resp.status_code == 200

    assert captured["era"] is None
    assert captured["personal"] is None
    # Never gated -- not personal/financial.
    assert captured["phone"] is SENTINEL_PHONE

    owner = db.get_user_by_chat_id(cfg.db_path, "111")
    assert captured["user_id"] != owner["id"]
    assert db.get_user_by_id(cfg.db_path, captured["user_id"])["role"] == "guest"


def test_confirmed_owner_on_camera_gets_real_context_and_real_user_id(client, cfg, monkeypatch):
    vision.add_camera(cfg.db_path, "touch1_cam", "Touch1 camera", "http://touch1/snapshot")
    vision.set_terminal_camera(cfg.db_path, "touch1", "touch1_cam")
    vision.enroll_known_person(cfg.db_path, "Dug", [1.0, 0.0, 0.0], access_level="owner")
    vision.record_event(cfg.db_path, "touch1_cam", "identified", person_key="dug", label="Dug")

    captured = {}

    def fake_handle_message(db_path, llm, user_id, text, **kwargs):
        captured["user_id"] = user_id
        captured.update(kwargs)
        return "reply"

    monkeypatch.setattr(devices_module, "handle_message", fake_handle_message)

    resp = _post_turn(client, cfg, "touch1")
    assert resp.status_code == 200

    assert captured["era"] is SENTINEL_ERA
    assert captured["personal"] is SENTINEL_PERSONAL

    owner = db.get_user_by_chat_id(cfg.db_path, "111")
    assert captured["user_id"] == owner["id"]


def test_a_terminal_with_no_camera_never_gets_sensitive_context_even_if_owner_enrolled(client, cfg, monkeypatch):
    """jarvisaudio1 has no camera assigned at all -- day-one coverage is touch1/laptop1
    only, and an audio-only terminal must stay fail-closed regardless of what's enrolled
    elsewhere in the house."""
    vision.enroll_known_person(cfg.db_path, "Dug", [1.0, 0.0, 0.0], access_level="owner")

    captured = {}

    def fake_handle_message(db_path, llm, user_id, text, **kwargs):
        captured.update(kwargs)
        return "reply"

    monkeypatch.setattr(devices_module, "handle_message", fake_handle_message)

    resp = _post_turn(client, cfg, "jarvisaudio1")
    assert resp.status_code == 200
    assert captured["era"] is None


def test_wake_claim_endpoint_solo_terminal_proceeds(client, cfg):
    resp = client.post(
        "/api/devices/touch1/wake_claim", headers=_headers(cfg), json={"score": 0.87},
    )
    assert resp.status_code == 200
    assert resp.json() == {"proceed": True}


def test_wake_claim_requires_device_key(client):
    resp = client.post("/api/devices/touch1/wake_claim", json={"score": 0.5})
    assert resp.status_code == 401
