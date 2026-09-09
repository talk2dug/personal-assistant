"""The enrollment step: an 'unrecognised face' review item, approved on the Review page
with a name typed into the existing note field, becomes a known_people row. Same
submit_for_review-style approval the rest of the app already uses, per
assistant/web/routes/review.py's docstring.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db, vision
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


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    vision.init_vision_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def client(db_path, tmp_path):
    media = tmp_path / "generated"
    media.mkdir()
    cfg = FakeConfig(
        db_path=db_path, generated_media_path=str(media),
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def _create_enrollment_item(db_path, owner_id, embedding=(1.0, 0.0, 0.0)):
    face = vision.upsert_unknown_face(db_path, "touch1", list(embedding), "generated/faces/x.jpg")
    item_id = business_db.create_review_item(
        db_path, owner_id, "Unrecognised face at Touch1", kind="other",
        source_agent="camera_watch", ref_table="unknown_faces", ref_id=face["id"],
        options=[{"label": "Unknown person", "media_path": face["thumbnail_path"]}],
    )
    vision.mark_unknown_face_asked(db_path, face["id"])
    return item_id, face


def test_approving_without_a_name_is_rejected(client, db_path, owner_id):
    item_id, _ = _create_enrollment_item(db_path, owner_id)
    resp = client.post(f"/api/review/items/{item_id}/decide", json={"decision": "approved"})
    assert resp.status_code == 400
    # And the item must still be pending -- a failed validation must not burn the
    # one-shot decision.
    assert business_db.get_review_item(db_path, owner_id, item_id)["status"] == "pending"


def test_approving_with_a_name_enrolls_a_known_person(client, db_path, owner_id):
    item_id, face = _create_enrollment_item(db_path, owner_id)
    resp = client.post(f"/api/review/items/{item_id}/decide",
                       json={"decision": "approved", "note": "Dug"})
    assert resp.status_code == 200
    body = resp.json()
    assert "known_people#dug" in body["written_through"]

    person = vision.get_known_person(db_path, "dug")
    assert person["name"] == "Dug"
    assert vision.get_unknown_face(db_path, face["id"])["resolved_person_key"] == "dug"


def test_rejecting_leaves_the_face_unidentified(client, db_path, owner_id):
    item_id, face = _create_enrollment_item(db_path, owner_id)
    resp = client.post(f"/api/review/items/{item_id}/decide", json={"decision": "rejected"})
    assert resp.status_code == 200
    assert vision.get_unknown_face(db_path, face["id"])["resolved_person_key"] is None
    assert vision.list_known_people(db_path) == []


def test_enrollment_is_visible_on_the_review_queue_with_its_thumbnail_option(client, db_path, owner_id):
    _create_enrollment_item(db_path, owner_id)
    items = client.get("/api/review/items").json()["items"]
    assert len(items) == 1
    assert items[0]["ref_table"] == "unknown_faces"
    assert len(items[0]["options"]) == 1
