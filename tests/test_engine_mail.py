"""Verifies the confirmation gate for iCloud Mail: send_email must never reach
MailClient.call_tool except through an explicit user confirmation, mirroring the
Era/phone gate this reuses (test_engine_confirmation.py)."""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
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
    sensitive_tools = {"send_email"} if sensitive else set()
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
