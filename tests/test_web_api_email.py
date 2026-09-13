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
        if name == "mark_email_read":
            if arguments["uid"] not in self.messages:
                return {"error": f"no message with uid {arguments['uid']}"}
            return {"ok": True, "uid": arguments["uid"], "folder": arguments.get("folder", "INBOX")}
        if name == "archive_email":
            if arguments["uid"] not in self.messages:
                return {"error": f"no message with uid {arguments['uid']}"}
            return {"ok": True, "uid": arguments["uid"], "from_folder": arguments.get("folder", "INBOX"), "to_folder": "Archive"}
        if name == "delete_email":
            if arguments["uid"] not in self.messages:
                return {"error": f"no message with uid {arguments['uid']}"}
            return {"ok": True, "uid": arguments["uid"], "from_folder": arguments.get("folder", "INBOX"), "to_folder": "Deleted Messages"}
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


# --- mark-read/archive/delete: a direct click through these endpoints IS the
# confirmation (no pending_actions gate on the web surface, only in chat) -----------

def test_mark_read_executes_directly(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.post("/api/email/messages/101/read")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.calls[0] == ("mark_email_read", {"uid": "101", "folder": "INBOX"})


def test_mark_read_missing_uid_is_a_404(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.post("/api/email/messages/does-not-exist/read")
    assert resp.status_code == 404


def test_archive_executes_directly_with_no_pending_action(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.post("/api/email/messages/101/archive")
    assert resp.status_code == 200
    assert resp.json()["to_folder"] == "Archive"
    assert client.calls[0] == ("archive_email", {"uid": "101", "folder": "INBOX"})


def test_archive_missing_uid_is_a_404(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.post("/api/email/messages/does-not-exist/archive")
    assert resp.status_code == 404


def test_delete_executes_directly_with_no_pending_action(db_path):
    client = FakeMailClient()
    c = _client(db_path, mail=FakeMailContext(client))
    resp = c.post("/api/email/messages/101/delete")
    assert resp.status_code == 200
    assert resp.json()["to_folder"] == "Deleted Messages"
    assert client.calls[0] == ("delete_email", {"uid": "101", "folder": "INBOX"})


def test_delete_missing_uid_is_a_404(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.post("/api/email/messages/does-not-exist/delete")
    assert resp.status_code == 404


def test_write_routes_require_owner(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()), login_as="Partner")
    assert c.post("/api/email/messages/101/read").status_code == 403
    assert c.post("/api/email/messages/101/archive").status_code == 403
    assert c.post("/api/email/messages/101/delete").status_code == 403


def test_write_routes_503_when_mail_is_not_configured(db_path):
    c = _client(db_path, mail=None)
    assert c.post("/api/email/messages/101/read").status_code == 503
    assert c.post("/api/email/messages/101/archive").status_code == 503
    assert c.post("/api/email/messages/101/delete").status_code == 503


# --- junk-log: read-only visibility into the already-autonomous junk scan ----------

def test_junk_log_returns_recorded_entries(db_path):
    from assistant.core import mail_db
    mail_db.init_mail_db(db_path)
    mail_db.log_junk_action(
        db_path, "9", "INBOX", "spammer@example.com", "You won!", 7.5, ["viagra", "urgent"],
        moved=True, moved_to="Junk",
    )
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/junk-log")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["subject"] == "You won!"
    assert entries[0]["score"] == 7.5
    assert entries[0]["moved"] is True
    assert entries[0]["moved_to"] == "Junk"
    assert entries[0]["reasons"] == ["viagra", "urgent"]


def test_junk_log_empty_when_nothing_recorded(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/junk-log")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_junk_log_503_when_mail_is_not_configured(db_path):
    c = _client(db_path, mail=None)
    assert c.get("/api/email/junk-log").status_code == 503


def test_junk_log_requires_owner(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()), login_as="Partner")
    assert c.get("/api/email/junk-log").status_code == 403
