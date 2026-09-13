"""Verifies the confirmation gate for iCloud Mail: send_email must never reach
MailClient.call_tool except through an explicit user confirmation, mirroring the
Era/phone gate this reuses (test_engine_confirmation.py)."""
import pytest

from assistant.core import business_db, db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeMailClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_mail(sensitive=True):
    client = FakeMailClient()
    sensitive_tools = {"send_email", "archive_email", "delete_email"} if sensitive else set()
    return engine.MailContext(mcp_client=client, sensitive_tools=sensitive_tools), client


def test_send_email_creates_pending_action_not_a_real_send(db_path, owner_id):
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "send_email", "arguments": {
                "to": "someone@example.com", "subject": "hi", "body": "hello there",
            }}}
        ]},
        {"role": "assistant", "content": "I'd like to send that email — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "email someone@example.com saying hello there", mail=mail)

    assert client.calls == []  # never actually sent
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "send_email"
    assert "confirm" in reply.lower()


def test_confirming_sends_the_real_email(db_path, owner_id):
    mail, client = make_mail()
    db.create_pending_action(db_path, owner_id, "send_email", {"to": "x@example.com", "subject": "hi", "body": "hey"})
    llm = FakeLLM([])  # keyword match handles "yes", no LLM call needed

    reply = engine.handle_message(db_path, llm, owner_id, "yes", mail=mail)

    assert client.calls == [("send_email", {"to": "x@example.com", "subject": "hi", "body": "hey"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_sends(db_path, owner_id):
    mail, client = make_mail()
    db.create_pending_action(db_path, owner_id, "send_email", {"to": "x@example.com", "subject": "hi", "body": "hey"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", mail=mail)

    assert client.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


def test_read_tools_call_directly_without_confirmation(db_path, owner_id):
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_emails", "arguments": {"limit": 5}}}
        ]},
        {"role": "assistant", "content": "Here's what's in your inbox."},
    ])

    engine.handle_message(db_path, llm, owner_id, "what's in my inbox", mail=mail)

    assert client.calls == [("list_emails", {"limit": 5})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_mail_tools_absent_when_no_mail_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "hi", mail=None)

    assert reply == "Hi there"


def test_archive_email_creates_pending_action_not_a_real_archive(db_path, owner_id):
    """archive_email is in mail_sensitive_tools by default (config.py) -- confirmed here
    at the engine level, the same gate send_email goes through."""
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "archive_email", "arguments": {"uid": "101", "folder": "INBOX"}}}
        ]},
        {"role": "assistant", "content": "I'd like to archive that email — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "archive that email from Bob", mail=mail)

    assert client.calls == []  # never actually archived
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "archive_email"
    assert "confirm" in reply.lower()


def test_confirming_archive_actually_archives(db_path, owner_id):
    mail, client = make_mail()
    db.create_pending_action(db_path, owner_id, "archive_email", {"uid": "101", "folder": "INBOX"})
    llm = FakeLLM([])  # keyword match handles "yes", no LLM call needed

    reply = engine.handle_message(db_path, llm, owner_id, "yes", mail=mail)

    assert client.calls == [("archive_email", {"uid": "101", "folder": "INBOX"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_delete_email_creates_pending_action_not_a_real_delete(db_path, owner_id):
    """delete_email is in mail_sensitive_tools by default and, per project 19's tracker,
    must stay gated permanently -- never autonomous regardless of any future classifier
    confidence. This pins that it always stages rather than executing."""
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "delete_email", "arguments": {"uid": "101", "folder": "INBOX"}}}
        ]},
        {"role": "assistant", "content": "I'd like to delete that email — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "delete that email from Bob", mail=mail)

    assert client.calls == []  # never actually deleted
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "delete_email"
    assert "confirm" in reply.lower()


def test_confirming_delete_actually_deletes(db_path, owner_id):
    mail, client = make_mail()
    db.create_pending_action(db_path, owner_id, "delete_email", {"uid": "101", "folder": "INBOX"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", mail=mail)

    assert client.calls == [("delete_email", {"uid": "101", "folder": "INBOX"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_mark_email_read_executes_immediately_without_confirmation(db_path, owner_id):
    """mark_email_read is deliberately NOT in mail_sensitive_tools (config.py) -- just a
    flag, trivially reversible, same treatment as the existing read tools."""
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "mark_email_read", "arguments": {"uid": "101", "folder": "INBOX"}}}
        ]},
        {"role": "assistant", "content": "Marked it read."},
    ])

    engine.handle_message(db_path, llm, owner_id, "mark that email read", mail=mail)

    assert client.calls == [("mark_email_read", {"uid": "101", "folder": "INBOX"})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_list_mail_folders_executes_immediately_without_confirmation(db_path, owner_id):
    """list_mail_folders is informational -- no account state changes, so it runs
    directly like list_emails/search_emails/read_email."""
    mail, client = make_mail()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_mail_folders", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "Here are your email folders."},
    ])

    engine.handle_message(db_path, llm, owner_id, "what email folders do I have", mail=mail)

    assert client.calls == [("list_mail_folders", {})]
    assert db.get_pending_action(db_path, owner_id) is None
