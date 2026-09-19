"""The gate: calling send_text must not send a text.

This is the test that matters for the whole feature. Everything else is plumbing. A
message that leaves before Jack has seen it is not a bug he can fix -- it is a thing he
has to explain to another person.

Mirrors test_engine_confirmation.py's shape, which covers the same gate for Era.
"""
import json

import pytest

from assistant.core import business_db, cellular, db, sms_tools
from assistant.core.engine import CellularContext, _SmsClient, _dispatch_tool_call


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "sms.db")
    db.init_db(path)
    # create_pending_action_and_review writes a Review card alongside every pending
    # action, so the review tables have to exist here too.
    business_db.init_business_db(path)
    cellular.init_cellular_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    cellular.add_contact(path, "Nadia", "+15406540555", "friend")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def ctx(db_path):
    return CellularContext(mcp_client=_SmsClient(db_path),
                           sensitive_tools=set(sms_tools.SMS_SENSITIVE_TOOLS))


def _call(db_path, owner_id, ctx, name, arguments):
    return json.loads(_dispatch_tool_call(
        db_path, "America/New_York", owner_id, name, arguments,
        era=None, calendar=None, cellular_ctx=ctx))


class TestTheGate:
    def test_calling_send_text_does_not_send_anything(self, db_path, owner_id, ctx):
        out = _call(db_path, owner_id, ctx, "send_text",
                    {"to": "Nadia", "message": "Free Thursday?"})
        assert out["status"] == "awaiting_confirmation"
        assert cellular.pending_outbound(db_path) == [], "nothing may be queued yet"

    def test_the_confirmation_names_the_real_number_not_the_nickname(self, db_path, owner_id, ctx):
        """Resolving the name at gate time means the owner confirms who it will actually
        reach, rather than whatever the model chose to call her."""
        out = _call(db_path, owner_id, ctx, "send_text",
                    {"to": "nadia", "message": "Free Thursday?"})
        assert "5406540555" in out["message"]
        assert "Nadia" in out["message"]
        assert "Free Thursday?" in out["message"]

    def test_it_creates_a_pending_action_and_a_review_card(self, db_path, owner_id, ctx):
        _call(db_path, owner_id, ctx, "send_text", {"to": "Nadia", "message": "hi"})
        pending = db.get_pending_action(db_path, owner_id)
        assert pending is not None and pending["tool_name"] == "send_text"

    def test_the_review_card_shows_the_message_not_just_the_tool_name(self, db_path, owner_id, ctx):
        """So approving from the Review page is an informed decision too."""
        _call(db_path, owner_id, ctx, "send_text",
              {"to": "Nadia", "message": "I'll pick you up at 8."})
        items = business_db.list_review_items(db_path, owner_id, status="pending")
        blob = json.dumps(items)
        assert "I'll pick you up at 8." in blob
        assert "Nadia" in blob

    def test_an_unknown_recipient_is_refused_before_any_confirmation_is_raised(
            self, db_path, owner_id, ctx):
        """Do not ask the owner to confirm a message that could never be delivered."""
        out = _call(db_path, owner_id, ctx, "send_text", {"to": "Beyonce", "message": "hi"})
        assert "error" in out
        assert db.get_pending_action(db_path, owner_id) is None

    def test_an_empty_message_never_reaches_a_confirmation(self, db_path, owner_id, ctx):
        out = _call(db_path, owner_id, ctx, "send_text", {"to": "Nadia", "message": "  "})
        assert "error" in out
        assert db.get_pending_action(db_path, owner_id) is None


class TestConfirmedSend:
    def test_executing_the_pending_action_is_what_finally_queues_it(self, db_path, owner_id, ctx):
        from assistant.core.engine import execute_pending_action
        _call(db_path, owner_id, ctx, "send_text",
              {"to": "Nadia", "message": "I'll pick you up at 8."})
        pending = db.get_pending_action(db_path, owner_id)

        result = execute_pending_action(pending, cellular_ctx=ctx)

        assert result["queued"] is True
        queued = cellular.pending_outbound(db_path)
        assert len(queued) == 1
        assert queued[0]["text"] == "I'll pick you up at 8."
        assert queued[0]["number"] == "5406540555"


class TestReadingNeedsNoConfirmation:
    def test_reading_a_thread_returns_data_immediately(self, db_path, owner_id, ctx):
        cellular.record_inbound(db_path, "+15406540555", "Thursday works!", "handled")
        out = _call(db_path, owner_id, ctx, "read_text_thread", {"who": "Nadia"})
        assert out["messages"][0]["text"] == "Thursday works!"
        assert db.get_pending_action(db_path, owner_id) is None

    def test_listing_contacts_returns_data_immediately(self, db_path, owner_id, ctx):
        out = _call(db_path, owner_id, ctx, "list_text_contacts", {})
        assert any(c["name"] == "Nadia" for c in out["contacts"])
        assert db.get_pending_action(db_path, owner_id) is None


def test_no_context_means_no_sms_tools_offered():
    """The capability should be absent, not merely discouraged, when switched off."""
    from assistant.core.engine import select_tools
    names = {t["function"]["name"] for t in select_tools("text nadia", route=False)}
    assert "send_text" not in names
    with_ctx = {t["function"]["name"] for t in
                select_tools("text nadia", route=False, cellular_ctx=object())}
    assert "send_text" in with_ctx
