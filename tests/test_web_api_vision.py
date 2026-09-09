"""Web surface for cameras/known-people/presence — owner-only, read/write for cameras,
read-only for identity data (enrollment happens via the Review page, not a form here).
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
    vision_presence_window_seconds: int = 180
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def client(db_path, tmp_path):
    cfg = FakeConfig(
        db_path=db_path, generated_media_path=str(tmp_path),
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def test_requires_login(db_path, tmp_path):
    cfg = FakeConfig(db_path=db_path, generated_media_path=str(tmp_path))
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/vision/cameras").status_code == 401


def test_add_and_list_cameras(client, db_path):
    resp = client.post("/api/vision/cameras", json={
        "key": "front_door", "name": "Front Door", "url": "http://cam/snapshot",
        "device_id": "kiosk1",
    })
    assert resp.status_code == 200

    cameras = client.get("/api/vision/cameras").json()["cameras"]
    assert len(cameras) == 1
    assert cameras[0]["device_id"] == "kiosk1"


def test_adding_a_camera_without_required_fields_is_rejected(client):
    resp = client.post("/api/vision/cameras", json={"name": "Front Door"})
    assert resp.status_code == 400


def test_known_people_never_returns_raw_embeddings(client, db_path):
    vision.enroll_person(db_path, "Dug", [0.1, 0.2, 0.3])
    people = client.get("/api/vision/known-people").json()["people"]
    assert len(people) == 1
    assert people[0]["name"] == "Dug"
    assert "embeddings" not in people[0]


def test_presence_resolves_person_keys_to_names(client, db_path):
    key = vision.enroll_person(db_path, "Dug", [0.1, 0.2, 0.3])
    vision.record_event(db_path, "front_door", "identified", label="Dug",
                        confidence=0.9, person_key=key)
    presence = client.get("/api/vision/presence").json()["presence"]
    assert presence["front_door"]["people"] == ["Dug"]


def test_events_lists_what_was_recorded(client, db_path):
    vision.record_event(db_path, "front_door", "person", confidence=0.8)
    events = client.get("/api/vision/events").json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "person"
