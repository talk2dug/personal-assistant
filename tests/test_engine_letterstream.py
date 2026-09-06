"""Verifies the confirmation gate for LetterStream: authorizing a mailing releases real
postage that cannot be recalled once accepted, so it must never reach mcp_client.call_tool
except through an explicit user confirmation. Quoting one (send_mail) is NOT gated, since
LetterStream's own preauth step means nothing is sent or charged by it alone. Mirrors
test_engine_kroger.py / test_engine_ccxt.py's suites.
"""
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


class FakeMCPClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": ["ok"]}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_letterstream():
    mcp = FakeMCPClient()
    sensitive = {"letterstream_authorize_mail"}
    return engine.LetterStreamContext(mcp_client=mcp, sensitive_tools=sensitive), mcp


def test_authorize_creates_pending_action_not_a_real_release(db_path, owner_id):
    letterstream, mcp = make_letterstream()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "letterstream_authorize_mail", "arguments": {"authcode": "abc123"}}}
        ]},
        {"role": "assistant", "content": "That'll cost $1.19 to mail to Bob — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "yes send it", letterstream=letterstream)

    assert mcp.calls == [], "a real mailing must never release before explicit confirmation"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "letterstream_authorize_mail"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_authorization(db_path, owner_id):
    letterstream, mcp = make_letterstream()
    db.create_pending_action(db_path, owner_id, "letterstream_authorize_mail", {"authcode": "abc123"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", letterstream=letterstream)

    assert mcp.calls == [("letterstream_authorize_mail", {"authcode": "abc123"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_releases_the_mailing(db_path, owner_id):
    letterstream, mcp = make_letterstream()
    db.create_pending_action(db_path, owner_id, "letterstream_authorize_mail", {"authcode": "abc123"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", letterstream=letterstream)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


def test_send_mail_quote_is_not_gated(db_path, owner_id):
    """LetterStream's own preauth design means send_mail cannot spend money or mail
    anything by itself, so it must execute directly -- gating it too would make every
    price quote require a confirmation before the owner has even seen the cost."""
    letterstream, mcp = make_letterstream()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "letterstream_send_mail", "arguments": {
                "letter_text": "Hi Bob", "recipient_name": "Bob", "recipient_address": "1 Bob St",
                "recipient_city": "Bobtown", "recipient_state": "AZ", "recipient_zip": "85281",
            }}}
        ]},
        {"role": "assistant", "content": "That would cost $1.19 — want me to send it?"},
    ])

    engine.handle_message(db_path, llm, owner_id, "quote mailing Bob a letter", letterstream=letterstream)

    assert len(mcp.calls) == 1 and mcp.calls[0][0] == "letterstream_send_mail"
    assert db.get_pending_action(db_path, owner_id) is None


def test_letterstream_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", letterstream=None)
    assert reply == "Hi there"


def test_a_pending_letterstream_action_is_detected_with_no_other_context_configured(db_path, owner_id):
    """handle_message's early pending-action check must include letterstream, or a
    confirmation reply sent while no era/phone/mail/HA/kroger/ccxt context exists would
    fall through to a normal turn instead of resolving the confirmation."""
    letterstream, mcp = make_letterstream()
    db.create_pending_action(db_path, owner_id, "letterstream_authorize_mail", {"authcode": "abc123"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", letterstream=letterstream)

    assert mcp.calls == [("letterstream_authorize_mail", {"authcode": "abc123"})]
    assert "done" in reply.lower()
