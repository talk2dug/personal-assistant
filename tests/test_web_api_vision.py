"""The /api/vision/* surface: camera CRUD (owner-only), presence, and the
conversation-mode endpoint device clients poll to decide whether to listen without a
wake word.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, vision
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    generated_media_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None
    device_api_key: str | None = "device-secret"
    device_camera_map: dict = field(default_factory=lambda: {"touch1": "touch1"})


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "GF", "partner")
    return path


@pytest.fixture
def cfg(db_path, tmp_path):
    media = tmp_path / "generated"
    media.mkdir()
    return FakeConfig(
        db_path=db_path, generated_media_path=str(media),
        users=[
            UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
            UserConfig(telegram_chat_id="222", display_name="GF", role="partner", web_password="pw2"),
        ],
    )


@pytest.fixture
def owner_client(cfg):
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


@pytest.fixture
def partner_client(cfg):
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "GF", "password": "pw2"}).status_code == 200
    return c


# --- cameras -------------------------------------------------------------------------

def test_cameras_requires_login(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/vision/cameras").status_code == 401


def test_cameras_is_owner_only(partner_client):
    assert partner_client.get("/api/vision/cameras").status_code == 403


def test_add_and_list_camera(owner_client):
    resp = owner_client.post("/api/vision/cameras", json={
        "key": "laptop1", "name": "laptop1 webcam", "url": "0", "kind": "local",
    })
    assert resp.status_code == 200
    cams = owner_client.get("/api/vision/cameras").json()["cameras"]
    assert [c["key"] for c in cams] == ["laptop1"]


def test_add_camera_requires_the_basics(owner_client):
    resp = owner_client.post("/api/vision/cameras", json={"name": "No key"})
    assert resp.status_code == 400


def test_add_camera_rejects_a_bad_kind(owner_client):
    resp = owner_client.post("/api/vision/cameras", json={
        "key": "x", "name": "X", "url": "http://x", "kind": "bogus",
    })
    assert resp.status_code == 400


# --- presence ------------------------------------------------------------------------

def test_presence_reports_known_people(owner_client, db_path):
    vision.enroll_known_person(db_path, "Dug", [1.0, 0.0])
    vision.record_event(db_path, "touch1", "identified", label="Dug", person_key="dug")
    body = owner_client.get("/api/vision/presence").json()
    assert body["known_people"] == [{"key": "dug", "name": "Dug", "relationship": "household"}]
    assert body["cameras"]["touch1"]["people"] == ["dug"]


# --- conversation-mode (device auth, not session auth) --------------------------------

def test_conversation_mode_requires_the_device_key(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    resp = TestClient(app).get("/api/vision/conversation-mode/touch1")
    assert resp.status_code == 401


def _device_get(app, path):
    return TestClient(app).get(path, headers={"Authorization": "Bearer device-secret"})


def test_conversation_mode_false_with_nobody_present(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    body = _device_get(app, "/api/vision/conversation-mode/touch1").json()
    assert body == {"open_mic": False, "person": None}


def test_conversation_mode_true_for_a_recognised_person(cfg, db_path):
    vision.enroll_known_person(db_path, "Dug", [1.0, 0.0])
    vision.record_event(db_path, "touch1", "identified", label="Dug", person_key="dug")
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    body = _device_get(app, "/api/vision/conversation-mode/touch1").json()
    assert body == {"open_mic": True, "person": "Dug"}


def test_conversation_mode_false_for_an_unrecognised_person(cfg, db_path):
    """A stranger in frame must never enable open-mic -- only a match against
    known_people does."""
    vision.record_event(db_path, "touch1", "unknown_person", detail="unknown_face:1")
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    body = _device_get(app, "/api/vision/conversation-mode/touch1").json()
    assert body == {"open_mic": False, "person": None}


def test_conversation_mode_false_for_a_device_with_no_mapped_camera(cfg, db_path):
    vision.enroll_known_person(db_path, "Dug", [1.0, 0.0])
    vision.record_event(db_path, "touch1", "identified", label="Dug", person_key="dug")
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    body = _device_get(app, "/api/vision/conversation-mode/some-other-device").json()
    assert body == {"open_mic": False, "person": None}
