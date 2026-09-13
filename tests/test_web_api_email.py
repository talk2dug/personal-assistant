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


# --- bills: read-only visibility into what the bill scan detected (mail_bills.py) ----

def _record_bill(db_path, **overrides):
    from assistant.core import db as core_db, mail_db
    mail_db.init_mail_db(db_path)
    owner = core_db.get_user_by_chat_id(db_path, "111")["id"]
    kwargs = dict(
        folder="INBOX", uid="1", from_address="billing@citypower.example",
        subject="Your October statement", received_at="2026-09-13", payee="City Power & Light",
        amount_text="$142.53", amount=142.53, due_date="2026-10-01",
        due_date_text="due October 1, 2026", is_recurring=True, cadence="monthly",
        confidence="high", reasoning="Monthly electric statement with a balance due.",
    )
    kwargs.update(overrides)
    return mail_db.create_bill(db_path, owner, **kwargs)


def test_bills_returns_detected_bills(db_path):
    _record_bill(db_path)
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/bills")
    assert resp.status_code == 200
    bills = resp.json()["bills"]
    assert len(bills) == 1
    assert bills[0]["payee"] == "City Power & Light"
    assert bills[0]["amount"] == 142.53
    assert bills[0]["amount_text"] == "$142.53"
    assert bills[0]["due_date"] == "2026-10-01"
    assert bills[0]["is_recurring"] is True
    assert bills[0]["status"] == "detected"


def test_bills_can_be_filtered_by_status(db_path):
    from assistant.core import db as core_db, mail_db
    bill_id = _record_bill(db_path)
    owner = core_db.get_user_by_chat_id(db_path, "111")["id"]
    mail_db.update_bill_status(db_path, owner, bill_id, "dismissed")
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    assert c.get("/api/email/bills?status=detected").json()["bills"] == []
    assert len(c.get("/api/email/bills?status=dismissed").json()["bills"]) == 1


def test_bills_empty_when_nothing_detected(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/bills")
    assert resp.status_code == 200
    assert resp.json()["bills"] == []


def test_bills_does_not_touch_the_mailbox(db_path):
    """Purely a database read -- it must not issue a single IMAP call, the same as
    junk-log."""
    client = FakeMailClient()
    _record_bill(db_path)
    c = _client(db_path, mail=FakeMailContext(client))
    assert c.get("/api/email/bills").status_code == 200
    assert client.calls == []


def test_bills_503_when_mail_is_not_configured(db_path):
    c = _client(db_path, mail=None)
    assert c.get("/api/email/bills").status_code == 503


def test_bills_requires_owner(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()), login_as="Partner")
    assert c.get("/api/email/bills").status_code == 403


# --- importance: provisional flags + how the learning loop is actually doing ---------
# (mail_importance.py). Read-only: the verdict that trains it is given in the Review
# queue, so nothing on this endpoint can change a flag or touch a message.

def _record_flag(db_path, **overrides):
    from assistant.core import db as core_db, mail_db
    mail_db.init_mail_db(db_path)
    owner = core_db.get_user_by_chat_id(db_path, "111")["id"]
    kwargs = dict(
        folder="INBOX", uid="1", from_address="service@mortgage.example",
        subject="Payment returned", received_at="2026-09-13",
        category="personal_finances", confidence=0.91,
        reason="Your mortgage servicer says a payment was returned.",
    )
    kwargs.update(overrides)
    return owner, mail_db.create_importance_flag(db_path, owner, **kwargs)


def test_importance_returns_flags_with_the_models_reason(db_path):
    _record_flag(db_path)
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/importance")
    assert resp.status_code == 200
    flags = resp.json()["flags"]
    assert len(flags) == 1
    assert flags[0]["category"] == "personal_finances"
    assert flags[0]["confidence"] == 0.91
    assert flags[0]["status"] == "flagged"
    assert "payment was returned" in flags[0]["reason"]


def test_importance_reports_the_accuracy_summary_from_his_own_verdicts(db_path):
    from assistant.core import mail_db
    owner, flag_id = _record_flag(db_path)
    _record_flag(db_path, uid="2", subject="Second thing")
    mail_db.mark_importance_scanned(db_path, owner, "INBOX", "1", flagged=True)
    mail_db.mark_importance_scanned(db_path, owner, "INBOX", "2", flagged=True)
    mail_db.mark_importance_scanned(db_path, owner, "INBOX", "3", flagged=False)
    mail_db.record_importance_verdict(db_path, owner, flag_id, important=True, note="yes, my mortgage")

    stats = _client(db_path, mail=FakeMailContext(FakeMailClient())).get("/api/email/importance").json()["stats"]
    assert stats["messages_judged"] == 3
    assert stats["flagged_total"] == 2
    assert stats["confirmed"] == 1 and stats["rejected"] == 0
    assert stats["awaiting_verdict"] == 1
    assert stats["precision"] == 1.0
    assert stats["examples_total"] == 1 and stats["examples_positive"] == 1


def test_importance_precision_is_null_rather_than_zero_before_any_verdict(db_path):
    """0-of-0 rendered as 0% would read as "this thing is always wrong" on day one."""
    _record_flag(db_path)
    stats = _client(db_path, mail=FakeMailContext(FakeMailClient())).get("/api/email/importance").json()["stats"]
    assert stats["precision"] is None


def test_importance_can_be_filtered_by_status(db_path):
    from assistant.core import mail_db
    owner, flag_id = _record_flag(db_path)
    mail_db.record_importance_verdict(db_path, owner, flag_id, important=False, note="nope")
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    assert c.get("/api/email/importance?status=flagged").json()["flags"] == []
    assert len(c.get("/api/email/importance?status=rejected").json()["flags"]) == 1


def test_importance_empty_when_nothing_flagged(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()))
    resp = c.get("/api/email/importance")
    assert resp.status_code == 200
    assert resp.json()["flags"] == [] and resp.json()["stats"]["flagged_total"] == 0


def test_importance_does_not_touch_the_mailbox(db_path):
    client = FakeMailClient()
    _record_flag(db_path)
    c = _client(db_path, mail=FakeMailContext(client))
    assert c.get("/api/email/importance").status_code == 200
    assert client.calls == []


def test_importance_503_when_mail_is_not_configured(db_path):
    c = _client(db_path, mail=None)
    assert c.get("/api/email/importance").status_code == 503


def test_importance_requires_owner(db_path):
    c = _client(db_path, mail=FakeMailContext(FakeMailClient()), login_as="Partner")
    assert c.get("/api/email/importance").status_code == 403
