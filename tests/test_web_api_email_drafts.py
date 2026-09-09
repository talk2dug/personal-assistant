"""Covers /api/email-drafts: owner-only access, listing/editing drafts, and the
on-demand scan endpoint's wiring to mail_triage.run_mail_triage_once."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, mail_db
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


class FakeMailClient:
    def __init__(self):
        self.scanned_with = None

    def list_recent(self, folder="INBOX", limit=10):
        self.scanned_with = limit
        return {"emails": []}


class FakeMailContext:
    def __init__(self, client):
        self.mcp_client = client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


def _client(db_path, mail=None, llm=None, login_as="Dug"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    app = create_app(cfg, llm or FakeLLM(), era=None, calendar=None, static_dir=None, mail=mail)
    c = TestClient(app)
    pw = "pw" if login_as == "Dug" else "pw2"
    assert c.post("/api/login", json={"name": login_as, "password": pw}).status_code == 200
    return c


def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/email-drafts").status_code == 401


def test_partner_is_forbidden(db_path):
    client = _client(db_path, login_as="Partner")
    assert client.get("/api/email-drafts").status_code == 403


def test_empty_list(db_path):
    client = _client(db_path)
    body = client.get("/api/email-drafts").json()
    assert body == {"drafts": [], "pending": 0}


def test_list_and_get_a_draft(db_path, owner_id):
    mail_db.init_mail_db(db_path)
    draft_id = mail_db.create_draft(
        db_path, owner_id, folder="INBOX", uid="1", from_address="c@example.com", subject="Hi",
        received_at="2026-09-01", category="other", reasoning="r",
        draft_subject="Re: Hi", draft_body="Thanks!",
    )
    client = _client(db_path)

    listed = client.get("/api/email-drafts").json()
    assert listed["pending"] == 1 and listed["drafts"][0]["id"] == draft_id

    got = client.get(f"/api/email-drafts/{draft_id}")
    assert got.status_code == 200 and got.json()["draft_subject"] == "Re: Hi"


def test_get_missing_draft_is_404(db_path):
    client = _client(db_path)
    assert client.get("/api/email-drafts/999").status_code == 404


def test_update_draft_body_marks_it_edited(db_path, owner_id):
    mail_db.init_mail_db(db_path)
    draft_id = mail_db.create_draft(
        db_path, owner_id, folder="INBOX", uid="1", from_address="c@example.com", subject="Hi",
        received_at="2026-09-01", category="other", reasoning="r", draft_subject="Re: Hi", draft_body="Thanks!",
    )
    client = _client(db_path)

    resp = client.put(f"/api/email-drafts/{draft_id}", json={"draft_body": "A better reply."})
    assert resp.status_code == 200
    assert resp.json()["draft"]["status"] == "edited"
    assert resp.json()["draft"]["draft_body"] == "A better reply."


def test_update_rejects_bad_status(db_path, owner_id):
    mail_db.init_mail_db(db_path)
    draft_id = mail_db.create_draft(
        db_path, owner_id, folder="INBOX", uid="1", from_address="c@example.com", subject="Hi",
        received_at="2026-09-01", category="other", reasoning="r", draft_subject="Re: Hi", draft_body="Thanks!",
    )
    client = _client(db_path)
    assert client.put(f"/api/email-drafts/{draft_id}", json={"status": "bogus"}).status_code == 400


def test_scan_without_mail_configured_is_503(db_path):
    client = _client(db_path, mail=None)
    assert client.post("/api/email-drafts/scan").status_code == 503


def test_scan_calls_mail_triage_with_the_mail_client(db_path):
    mail_client = FakeMailClient()
    client = _client(db_path, mail=FakeMailContext(mail_client))

    resp = client.post("/api/email-drafts/scan", params={"limit": 7})
    assert resp.status_code == 200
    assert resp.json() == {"scanned": 0, "drafted": 0}
    assert mail_client.scanned_with == 7
