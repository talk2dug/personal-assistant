"""Covers /api/email: list/search/read the inbox and the one real write (send), which
must only ever fire on an explicit call to this endpoint -- the frontend's confirm modal
is the approval, there is no further server-side gate (same precedent as credit.py's
mail_letter), so this pins the request validation that stands in its place.
"""
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
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


class FakeMailClient:
    """Mirrors the real MailClient.call_tool dispatch shape exactly, so these tests
    exercise the same name->method contract the real client honors."""

    def __init__(self):
        self.calls = []
        self.messages = {
            "101": {"uid": "101", "from": "a@x.com", "to": "me@x.com", "subject": "Hi", "date": "d", "body": "hello"},
        }

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "list_emails":
            return {"emails": [{"uid": "101", "from": "a@x.com", "subject": "Hi", "date": "d", "unread": True}]}
        if name == "search_emails":
            return {"emails": [{"uid": "101", "from": "a@x.com", "subject": "Hi", "date": "d", "unread": True}]}
        if name == "read_email":
            msg = self.messages.get(arguments["uid"])
            return msg if msg else {"error": f"no message with uid {arguments['uid']}"}
        if name == "send_email":
            return {"ok": True, "to": arguments["to"], "subject": arguments["subject"]}
        return {"error": f"unknown mail tool {name}"}


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


def _client(db_path, mail=None, login_as="Dug"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, mail=mail)
    c = TestClient(app)
    pw = "pw" if login_as == "Dug" else "pw2"
    assert c.post("/api/login", json={"name": login_as, "password": pw}).status_code == 200
    return c


def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, mail=FakeMailContext(FakeMailClient()))
    c = TestClient(app)
    assert c.get("/api/email/messages").status_code == 401


def test_partner_is_refused_owner_only_access(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()), login_as="Partner")
    assert c.get("/api/email/messages").status_code == 403


def test_returns_503_when_mail_is_not_configured(db_path):
    c = _client(db_path, mail=None)
    assert c.get("/api/email/messages").status_code == 503


def test_lists_recent_messages(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.get("/api/email/messages")
    assert resp.status_code == 200
    assert resp.json()["emails"][0]["subject"] == "Hi"
    assert client.calls[0] == ("list_emails", {"folder": "INBOX", "limit": 20})


def test_a_query_param_searches_instead_of_listing(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.get("/api/email/messages?query=invoice")
    assert resp.status_code == 200
    assert client.calls[0][0] == "search_emails"
    assert client.calls[0][1]["query"] == "invoice"


def test_reads_one_message_by_uid(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/messages/101")
    assert resp.status_code == 200
    assert resp.json()["body"] == "hello"


def test_reading_a_missing_uid_is_a_404_not_a_200_with_an_error_body(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/messages/does-not-exist")
    assert resp.status_code == 404


def test_send_requires_all_three_fields(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    for payload in ({"subject": "s", "body": "b"}, {"to": "a@x.com", "body": "b"}, {"to": "a@x.com", "subject": "s"}):
        resp = c.post("/api/email/send", json=payload)
        assert resp.status_code == 400


def test_send_rejects_a_to_address_with_no_at_sign(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.post("/api/email/send", json={"to": "not-an-email", "subject": "s", "body": "b"})
    assert resp.status_code == 400


def test_send_actually_calls_send_email_with_exactly_what_was_posted(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.post("/api/email/send", json={"to": "a@x.com", "subject": "Hello", "body": "Test body"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.calls[0] == ("send_email", {"to": "a@x.com", "subject": "Hello", "body": "Test body"})
