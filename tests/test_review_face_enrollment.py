"""The enrollment flow: an unfamiliar face becomes a Review-page item, and the owner's
decision there -- not any automatic process -- is what actually creates or updates a
known person. Exercised through the real /api/review endpoint, since the point of this
feature is that enrollment happens via the ordinary submit_for_review path.
"""
import json
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
    cfg = FakeConfig(
        db_path=db_path, generated_media_path=str(tmp_path),
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def _make_face_review(db_path, owner_id, embedding, existing_person_key=None):
    face_id = vision.upsert_unknown_face(db_path, "front_door", embedding, "thumb.jpg")["id"]
    options = [{"label": "New person", "description": "", "media_path": "thumb.jpg",
               "body": json.dumps({"new_person": True})}]
    if existing_person_key:
        options.insert(0, {"label": "This is Dug", "description": "", "media_path": "thumb.jpg",
                           "body": json.dumps({"person_key": existing_person_key})})
    item_id = business_db.create_review_item(
        db_path, owner_id, "Unfamiliar face on Front Door", kind="other",
        source_agent="vision", ref_table="unknown_faces", ref_id=face_id, options=options,
    )
    return item_id, face_id


def test_approving_new_person_with_a_note_enrolls_them(client, db_path, owner_id):
    item_id, face_id = _make_face_review(db_path, owner_id, [0.1, 0.2, 0.3])
    options = client.get("/api/review/items").json()["items"][0]["options"]
    new_person_option = next(o for o in options if o["label"] == "New person")

    resp = client.post(f"/api/review/items/{item_id}/decide", json={
        "decision": "approved", "option_id": new_person_option["id"], "note": "Grandma",
    })
    assert resp.status_code == 200
    assert "enrolled" in resp.json()["written_through"]

    people = vision.get_known_people(db_path)
    assert len(people) == 1
    assert people[0]["name"] == "Grandma"
    face = vision.get_unknown_face(db_path, face_id)
    assert face["resolved_person_key"] == people[0]["key"]
    assert face["asked"] == 1


def test_approving_new_person_without_a_note_does_not_enroll(client, db_path, owner_id):
    item_id, face_id = _make_face_review(db_path, owner_id, [0.1, 0.2, 0.3])
    options = client.get("/api/review/items").json()["items"][0]["options"]
    new_person_option = next(o for o in options if o["label"] == "New person")

    client.post(f"/api/review/items/{item_id}/decide",
               json={"decision": "approved", "option_id": new_person_option["id"]})
    assert vision.get_known_people(db_path) == []
    face = vision.get_unknown_face(db_path, face_id)
    assert face["resolved_person_key"] is None


def test_approving_a_match_to_an_existing_person_adds_an_embedding(client, db_path, owner_id):
    key = vision.enroll_person(db_path, "Dug", [0.9, 0.1, 0.0])
    item_id, face_id = _make_face_review(db_path, owner_id, [0.1, 0.2, 0.3], existing_person_key=key)
    options = client.get("/api/review/items").json()["items"][0]["options"]
    match_option = next(o for o in options if o["label"] == "This is Dug")

    resp = client.post(f"/api/review/items/{item_id}/decide",
                       json={"decision": "approved", "option_id": match_option["id"]})
    assert "linked to existing person" in resp.json()["written_through"]

    person = vision.get_person_by_key(db_path, key)
    assert person["sample_count"] == 2
    face = vision.get_unknown_face(db_path, face_id)
    assert face["resolved_person_key"] == key


def test_rejecting_a_face_review_does_not_enroll_anyone(client, db_path, owner_id):
    item_id, face_id = _make_face_review(db_path, owner_id, [0.1, 0.2, 0.3])
    client.post(f"/api/review/items/{item_id}/decide", json={"decision": "rejected"})

    assert vision.get_known_people(db_path) == []
    face = vision.get_unknown_face(db_path, face_id)
    assert face["resolved_person_key"] is None
    assert face["asked"] == 1
