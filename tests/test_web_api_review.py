"""Covers the review queue — the surface where the team's work waits for the owner.

Two properties matter most. Approving must **write through** to the pipeline row the item
references, or the office will show a design as approved that the Art Director still
can't act on. And a decided item must not be decidable twice, or a stale tab could
silently overturn a call the owner already made.
"""
import pathlib
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db
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
def media_dir(tmp_path):
    d = tmp_path / "generated"
    d.mkdir()
    return d


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def client(db_path, media_dir):
    cfg = FakeConfig(
        db_path=db_path, generated_media_path=str(media_dir),
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def test_requires_login(db_path, media_dir):
    cfg = FakeConfig(db_path=db_path, generated_media_path=str(media_dir))
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/review/items").status_code == 401


def test_empty_queue(client):
    body = client.get("/api/review/items").json()
    assert body["items"] == [] and body["pending"] == 0


def test_a_yes_no_item_round_trips(client, db_path, owner_id):
    business_db.create_review_item(
        db_path, owner_id, "Apply to Spooktoberfest?", kind="other",
        summary="Oct 4, $70", source_agent="market_finder")
    body = client.get("/api/review/items").json()
    assert body["pending"] == 1
    item = body["items"][0]
    assert item["title"] == "Apply to Spooktoberfest?"
    assert item["options"] == []  # no options == straight approve/reject


def test_a_choice_carries_its_options_in_order(client, db_path, owner_id):
    business_db.create_review_item(
        db_path, owner_id, "Pick a treatment", kind="art",
        options=[{"label": "Bold"}, {"label": "Retro"}, {"label": "Monoline"}])
    item = client.get("/api/review/items").json()["items"][0]
    assert [o["label"] for o in item["options"]] == ["Bold", "Retro", "Monoline"]


def test_choosing_an_option_marks_exactly_one_winner(client, db_path, owner_id):
    item_id = business_db.create_review_item(
        db_path, owner_id, "Pick a treatment", kind="art",
        options=[{"label": "Bold"}, {"label": "Retro"}])
    options = client.get("/api/review/items").json()["items"][0]["options"]

    resp = client.post(f"/api/review/items/{item_id}/decide",
                       json={"decision": "approved", "option_id": options[1]["id"], "note": "suits the crowd"})
    assert resp.status_code == 200
    decided = resp.json()["item"]
    chosen = [o for o in decided["options"] if o["chosen"] == 1]
    assert len(chosen) == 1 and chosen[0]["label"] == "Retro"
    assert decided["decision_note"] == "suits the crowd"


def test_approval_writes_through_to_the_concept(client, db_path, owner_id):
    """The property that keeps the queue and the pipeline honest with each other."""
    concept_id, _ = business_db.create_product_concept(db_path, owner_id, "RVA Decal", "sticker")
    item_id = business_db.create_review_item(
        db_path, owner_id, "Approve concept", kind="concept",
        ref_table="product_concepts", ref_id=concept_id)

    resp = client.post(f"/api/review/items/{item_id}/decide", json={"decision": "approved"})
    assert resp.status_code == 200
    assert "product_concepts" in (resp.json()["written_through"] or "")
    concept = business_db.list_product_concepts(db_path, owner_id)[0]
    assert concept["status"] == "approved"


def test_rejection_writes_through_too(client, db_path, owner_id):
    listing_id = business_db.create_store_listing(db_path, owner_id, "RVA Decal", price=6.0)
    item_id = business_db.create_review_item(
        db_path, owner_id, "Approve listing", kind="listing",
        ref_table="store_listings", ref_id=listing_id)
    client.post(f"/api/review/items/{item_id}/decide", json={"decision": "rejected"})
    assert business_db.list_store_listings(db_path, owner_id)[0]["status"] == "rejected"


def test_an_item_cannot_be_decided_twice(client, db_path, owner_id):
    """A stale tab must not be able to overturn a decision already made."""
    item_id = business_db.create_review_item(db_path, owner_id, "One shot")
    assert client.post(f"/api/review/items/{item_id}/decide", json={"decision": "approved"}).status_code == 200
    assert client.post(f"/api/review/items/{item_id}/decide", json={"decision": "rejected"}).status_code == 404


def test_a_bad_decision_value_is_rejected(client, db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Item")
    assert client.post(f"/api/review/items/{item_id}/decide", json={"decision": "maybe"}).status_code == 400


def test_deciding_removes_it_from_pending_and_files_it(client, db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Item")
    client.post(f"/api/review/items/{item_id}/decide", json={"decision": "approved"})
    assert client.get("/api/review/items?status=pending").json()["pending"] == 0
    assert len(client.get("/api/review/items?status=approved").json()["items"]) == 1


def test_media_is_served_for_a_real_file(client, db_path, owner_id, media_dir):
    art = media_dir / "job1_art.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    item_id = business_db.create_review_item(
        db_path, owner_id, "Art", kind="art", options=[{"label": "A", "media_path": str(art)}])
    option_id = business_db.get_review_item(db_path, owner_id, item_id)["options"][0]["id"]

    resp = client.get(f"/api/review/media/{option_id}")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG\r\n\x1a\nfake"
    assert resp.headers["content-type"].startswith("image/png")


def test_media_outside_the_generated_directory_is_refused(client, db_path, owner_id, tmp_path):
    """The path comes from a database row, so it is resolved and confined — otherwise
    this endpoint becomes an arbitrary-file read."""
    secret = tmp_path / "secrets.txt"
    secret.write_text("not for the browser")
    item_id = business_db.create_review_item(
        db_path, owner_id, "Sneaky", options=[{"label": "A", "media_path": str(secret)}])
    option_id = business_db.get_review_item(db_path, owner_id, item_id)["options"][0]["id"]
    assert client.get(f"/api/review/media/{option_id}").status_code == 403


def test_missing_media_file_is_a_404_not_a_crash(client, db_path, owner_id, media_dir):
    item_id = business_db.create_review_item(
        db_path, owner_id, "Gone",
        options=[{"label": "A", "media_path": str(media_dir / "deleted.png")}])
    option_id = business_db.get_review_item(db_path, owner_id, item_id)["options"][0]["id"]
    assert client.get(f"/api/review/media/{option_id}").status_code == 404


def test_high_priority_sorts_to_the_top_of_the_stack(client, db_path, owner_id):
    business_db.create_review_item(db_path, owner_id, "Normal thing")
    business_db.create_review_item(db_path, owner_id, "Urgent thing", priority="high")
    titles = [i["title"] for i in client.get("/api/review/items").json()["items"]]
    assert titles[0] == "Urgent thing"
