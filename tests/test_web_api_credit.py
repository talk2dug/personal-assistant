"""Covers /api/credit: score history CRUD, the dispute-item state machine, and the
LetterStream-backed draft/mail/track flow. This whole feature (personal_db's
credit_score_entries/dispute_items/dispute_letters schema and CRUD, added in PR #8) had
zero test coverage until now -- a later squash-merge silently deleted all twelve
personal_db functions credit.py depends on (schema and all), and nothing caught it: every
/api/credit/* call and every credit/dispute chat tool would 500 with an AttributeError.
These tests pin the restored behavior so that regression can't recur silently again.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, personal_db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


class FakeLetterStreamClient:
    """Mirrors LetterStreamTools.call_tool's dispatch shape (name -> dict)."""

    def __init__(self):
        self.calls = []
        self.next_track_result = {"cert": "9400111899223344556677"}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "letterstream_send_mail":
            return {"cost": "5.42", "authcode": "auth-abc", "job": "job-1", "doc_id": "job-1-0"}
        if name == "letterstream_authorize_mail":
            return {"ok": True, "authcode": arguments["authcode"]}
        if name == "letterstream_track_mail":
            return self.next_track_result
        return {"error": f"unknown letterstream tool {name}"}


class FakeLetterStreamContext:
    def __init__(self, client):
        self.mcp_client = client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


def _client(db_path, letterstream=None, login_as="Dug"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, letterstream=letterstream)
    c = TestClient(app)
    pw = "pw" if login_as == "Dug" else "pw2"
    assert c.post("/api/login", json={"name": login_as, "password": pw}).status_code == 200
    return c


def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    assert c.get("/api/credit/scores").status_code == 401


def test_partner_is_refused_owner_only_access(db_path):
    c = _client(db_path, login_as="Partner")
    assert c.get("/api/credit/scores").status_code == 403


# --- scores ------------------------------------------------------------------

def test_add_list_and_delete_a_score(db_path):
    c = _client(db_path)
    resp = c.post("/api/credit/scores", json={"bureau": "experian", "score": 680, "source": "Credit Karma"})
    assert resp.status_code == 200
    entry_id = resp.json()["entry_id"]

    listed = c.get("/api/credit/scores").json()
    assert len(listed) == 1
    assert listed[0]["bureau"] == "experian" and listed[0]["score"] == 680

    assert c.delete(f"/api/credit/scores/{entry_id}").status_code == 200
    assert c.get("/api/credit/scores").json() == []


def test_add_score_rejects_bad_bureau_or_out_of_range_score(db_path):
    c = _client(db_path)
    assert c.post("/api/credit/scores", json={"bureau": "not-a-bureau", "score": 680}).status_code == 400
    assert c.post("/api/credit/scores", json={"bureau": "experian", "score": 200}).status_code == 400


def test_deleting_a_missing_score_is_404(db_path):
    c = _client(db_path)
    assert c.delete("/api/credit/scores/999").status_code == 404


# --- dispute items -------------------------------------------------------------

def test_create_list_and_update_a_dispute(db_path):
    c = _client(db_path)
    resp = c.post("/api/credit/disputes", json={
        "bureau": "equifax", "creditor_name": "Acme Collections",
        "item_description": "Charge-off not mine", "reason": "Identity theft",
    })
    assert resp.status_code == 200
    dispute_id = resp.json()["dispute_item_id"]

    listed = c.get("/api/credit/disputes").json()
    assert len(listed) == 1 and listed[0]["status"] == "drafted"

    resp = c.put(f"/api/credit/disputes/{dispute_id}", json={"status": "resolved", "resolution": "Removed"})
    assert resp.status_code == 200
    updated = c.get("/api/credit/disputes", params={"status": "resolved"}).json()
    assert updated[0]["resolution"] == "Removed"


def test_create_dispute_requires_bureau_and_text_fields(db_path):
    c = _client(db_path)
    resp = c.post("/api/credit/disputes", json={"bureau": "equifax", "creditor_name": "", "item_description": "x", "reason": "y"})
    assert resp.status_code == 400
    resp = c.post("/api/credit/disputes", json={"bureau": "bogus", "creditor_name": "a", "item_description": "x", "reason": "y"})
    assert resp.status_code == 400


def test_updating_a_missing_dispute_is_404(db_path):
    c = _client(db_path)
    assert c.put("/api/credit/disputes/999", json={"status": "resolved"}).status_code == 404


# --- dispute letters (LetterStream) --------------------------------------------

def _make_dispute(c):
    resp = c.post("/api/credit/disputes", json={
        "bureau": "transunion", "creditor_name": "Acme Collections",
        "item_description": "Charge-off not mine", "reason": "Identity theft",
    })
    return resp.json()["dispute_item_id"]


def test_drafting_a_letter_quotes_through_letterstream_and_stores_it(db_path):
    client = FakeLetterStreamClient()
    c = _client(db_path, letterstream=FakeLetterStreamContext(client))
    dispute_id = _make_dispute(c)

    resp = c.post(f"/api/credit/disputes/{dispute_id}/letters/draft", json={
        "letter_text": "Please investigate...",
        "recipient_name": "TransUnion", "recipient_address": "PO Box 1",
        "recipient_city": "Chester", "recipient_state": "PA", "recipient_zip": "19016",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["quote"]["cost"] == "5.42"
    assert client.calls[0][0] == "letterstream_send_mail"

    letters = c.get(f"/api/credit/disputes/{dispute_id}/letters").json()
    assert len(letters) == 1
    assert letters[0]["status"] == "quoted"
    assert letters[0]["authcode"] == "auth-abc"


def test_draft_without_letterstream_configured_is_503(db_path):
    c = _client(db_path, letterstream=None)
    dispute_id = _make_dispute(c)
    resp = c.post(f"/api/credit/disputes/{dispute_id}/letters/draft", json={
        "letter_text": "x", "recipient_name": "TU", "recipient_address": "a",
        "recipient_city": "c", "recipient_state": "s", "recipient_zip": "z",
    })
    assert resp.status_code == 503


def test_draft_for_missing_dispute_is_404(db_path):
    c = _client(db_path, letterstream=FakeLetterStreamContext(FakeLetterStreamClient()))
    resp = c.post("/api/credit/disputes/999/letters/draft", json={
        "letter_text": "x", "recipient_name": "TU", "recipient_address": "a",
        "recipient_city": "c", "recipient_state": "s", "recipient_zip": "z",
    })
    assert resp.status_code == 404


def _draft_letter(c):
    dispute_id = _make_dispute(c)
    resp = c.post(f"/api/credit/disputes/{dispute_id}/letters/draft", json={
        "letter_text": "Please investigate...",
        "recipient_name": "TransUnion", "recipient_address": "PO Box 1",
        "recipient_city": "Chester", "recipient_state": "PA", "recipient_zip": "19016",
    })
    return dispute_id, resp.json()["dispute_letter_id"]


def test_mailing_a_letter_authorizes_and_marks_the_dispute_mailed(db_path):
    client = FakeLetterStreamClient()
    c = _client(db_path, letterstream=FakeLetterStreamContext(client))
    dispute_id, letter_id = _draft_letter(c)

    resp = c.post(f"/api/credit/letters/{letter_id}/mail", json={"expected_cost": "5.42"})
    assert resp.status_code == 200
    assert ("letterstream_authorize_mail", {"authcode": "auth-abc"}) in client.calls

    disputes = c.get("/api/credit/disputes").json()
    assert disputes[0]["id"] == dispute_id
    assert disputes[0]["status"] == "mailed"


def test_mailing_twice_is_refused(db_path):
    c = _client(db_path, letterstream=FakeLetterStreamContext(FakeLetterStreamClient()))
    _, letter_id = _draft_letter(c)
    assert c.post(f"/api/credit/letters/{letter_id}/mail", json={}).status_code == 200
    assert c.post(f"/api/credit/letters/{letter_id}/mail", json={}).status_code == 409


def test_mailing_with_a_stale_cost_is_refused(db_path):
    c = _client(db_path, letterstream=FakeLetterStreamContext(FakeLetterStreamClient()))
    _, letter_id = _draft_letter(c)
    resp = c.post(f"/api/credit/letters/{letter_id}/mail", json={"expected_cost": "9.99"})
    assert resp.status_code == 409


def test_tracking_a_letter_updates_the_tracking_number_when_it_changes(db_path):
    client = FakeLetterStreamClient()
    c = _client(db_path, letterstream=FakeLetterStreamContext(client))
    _, letter_id = _draft_letter(c)
    c.post(f"/api/credit/letters/{letter_id}/mail", json={})

    resp = c.post(f"/api/credit/letters/{letter_id}/track", json={})
    assert resp.status_code == 200
    assert resp.json()["tracking"]["cert"] == "9400111899223344556677"
