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


# --- mark_email_importance: the conversational half of mail_importance.py's loop ------
# Records what the owner SAID about a message. It is in MAIL_TOOLS for discoverability,
# but it is not a mail operation: it must never reach the mailbox to change anything.

def _dispatch(db_path, owner_id, mail, arguments):
    import json as _json
    return _json.loads(engine._dispatch_tool_call(
        db_path, "America/New_York", owner_id, "mark_email_importance", arguments,
        None, None, mail=mail))


def test_marking_a_message_important_in_chat_records_a_training_example(db_path, owner_id):
    from assistant.core import mail_db
    mail, client = make_mail()

    result = _dispatch(db_path, owner_id, mail, {
        "uid": "101", "important": True, "note": "anything from my sister is important"})

    assert result["ok"] is True and result["recorded"] == "important"
    examples = mail_db.list_importance_examples(db_path, owner_id)
    assert len(examples) == 1
    assert examples[0]["label"] == 1 and examples[0]["source"] == "chat"
    assert examples[0]["note"] == "anything from my sister is important"
    # The only mailbox call allowed is the read that fetches the sender/subject, which is
    # what makes the example usable as a few-shot line. Nothing mutating.
    assert [name for name, _ in client.calls] == ["read_email"]


def test_marking_a_message_not_important_records_a_negative_example(db_path, owner_id):
    from assistant.core import mail_db
    mail, _ = make_mail()

    result = _dispatch(db_path, owner_id, mail, {
        "uid": "101", "important": False, "note": "that account is closed"})

    assert result["recorded"] == "not important"
    assert mail_db.list_importance_examples(db_path, owner_id, label=False)[0]["label"] == 0


def test_a_chat_verdict_about_an_already_flagged_message_updates_that_flag(db_path, owner_id):
    from assistant.core import mail_db
    mail_db.init_mail_db(db_path)
    flag_id = mail_db.create_importance_flag(
        db_path, owner_id, folder="INBOX", uid="101", from_address="a@b.example",
        subject="A thing", received_at="d", category="personal_business",
        confidence=0.9, reason="Looked like an account problem.")
    mail, client = make_mail()

    _dispatch(db_path, owner_id, mail, {"uid": "101", "important": False, "note": "nope"})

    assert mail_db.get_importance_flag(db_path, owner_id, flag_id)["status"] == "rejected"
    # No mailbox call at all here: the flag row already carries the sender and subject.
    assert client.calls == []


def test_the_verdict_survives_a_failed_header_read(db_path, owner_id):
    """A mail server hiccup must not cost him the verdict he just gave."""
    from assistant.core import mail_db

    class BrokenMailClient:
        calls = []

        def call_tool(self, name, arguments):
            raise RuntimeError("IMAP down")

    mail = engine.MailContext(mcp_client=BrokenMailClient(), sensitive_tools=set())
    result = _dispatch(db_path, owner_id, mail, {"uid": "101", "important": True})

    assert result["ok"] is True
    assert len(mail_db.list_importance_examples(db_path, owner_id)) == 1


def test_mark_email_importance_needs_a_uid_and_a_verdict(db_path, owner_id):
    mail, _ = make_mail()
    assert "error" in _dispatch(db_path, owner_id, mail, {"important": True})
    assert "error" in _dispatch(db_path, owner_id, mail, {"uid": "101"})


def test_mark_email_importance_is_not_a_sensitive_tool(db_path, owner_id):
    """It records what he already told us. Staging a confirmation for "yes that mattered"
    would make the loop more annoying than the flagging it exists to improve."""
    mail, _ = make_mail()
    _dispatch(db_path, owner_id, mail, {"uid": "101", "important": True})
    assert db.get_pending_action(db_path, owner_id) is None
