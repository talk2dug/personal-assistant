"""Jarvis sending texts on the owner's behalf.

The rule these exist to pin down: **calling send_text must never send anything.** A text
to another human cannot be recalled, reads as having come from Jack, and lands on someone
who never opted into being messaged by software. So it goes through the same
pending_actions/review gate as a Kroger write or a mailed dispute letter, and the owner
sees the exact recipient and exact words first.

Reading is ungated on purpose -- those messages are already his, and an assistant that
can send but cannot see the reply cannot do the job this exists for ("ask her if she's
free Thursday, then tell her I'll pick her up at 8").
"""
import json

import pytest

from assistant.core import cellular, db, sms_tools


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "sms.db")
    db.init_db(path)
    cellular.init_cellular_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    cellular.add_contact(path, "Nadia", "+15406540555", "friend")
    cellular.add_contact(path, "Mum", "+15551230000", "family")
    return path


class TestResolveRecipient:
    def test_a_first_name_resolves(self, db_path):
        number, name, error = sms_tools.resolve_recipient(db_path, "nadia")
        assert number == "5406540555" and name == "Nadia" and error is None

    def test_a_raw_number_is_allowed_but_unnamed(self, db_path):
        """Permitted because the confirmation step shows it in full, and that is where a
        typo gets caught by the one person who can recognise it."""
        number, name, error = sms_tools.resolve_recipient(db_path, "+1 (202) 555-0147")
        assert number == "2025550147" and name is None and error is None

    def test_an_unknown_name_is_an_error_not_a_guess(self, db_path):
        number, _, error = sms_tools.resolve_recipient(db_path, "Beyonce")
        assert number is None and "No contact matches" in error

    def test_an_ambiguous_name_refuses_rather_than_picking_one(self, db_path):
        """Two Sarahs on file and 'text Sarah' must stop and ask. Picking one and texting
        her is exactly the mistake the whole confirmation flow exists to prevent."""
        cellular.add_contact(db_path, "Sarah Jones", "+15551110001")
        cellular.add_contact(db_path, "Sarah Blake", "+15551110002")
        number, _, error = sms_tools.resolve_recipient(db_path, "Sarah")
        assert number is None and error is not None


class TestReadingIsUngated:
    def test_an_empty_thread_says_so_rather_than_returning_nothing(self, db_path):
        out = json.loads(sms_tools.handle(db_path, "read_text_thread", {"who": "Nadia"}))
        assert out["messages"] == []
        assert "not replied" in out["note"]

    def test_a_thread_reads_as_a_conversation_not_a_transport_log(self, db_path):
        cellular.record_inbound(db_path, "+15406540555", "Sure, Thursday works", "handled")
        cellular.queue_outbound(db_path, "+15406540555", "Are you free Thursday?")
        out = json.loads(sms_tools.handle(db_path, "read_text_thread", {"who": "nadia"}))
        senders = {m["from"] for m in out["messages"]}
        assert senders == {"them", "you"}
        assert out["who"] == "Nadia"

    def test_one_persons_thread_never_leaks_anothers(self, db_path):
        cellular.record_inbound(db_path, "+15406540555", "from Nadia", "handled")
        cellular.record_inbound(db_path, "+15551230000", "from Mum", "handled")
        out = json.loads(sms_tools.handle(db_path, "read_text_thread", {"who": "Nadia"}))
        assert [m["text"] for m in out["messages"]] == ["from Nadia"]

    def test_contacts_list_includes_the_relationship(self, db_path):
        out = json.loads(sms_tools.handle(db_path, "list_text_contacts", {}))
        by_name = {c["name"]: c for c in out["contacts"]}
        assert by_name["Nadia"]["relationship"] == "friend"

    def test_no_contacts_says_so_rather_than_returning_an_empty_list(self, tmp_path):
        path = str(tmp_path / "empty.db")
        db.init_db(path); cellular.init_cellular_db(path)
        out = json.loads(sms_tools.handle(path, "list_text_contacts", {}))
        assert out["contacts"] == [] and "Nobody on file" in out["note"]


class TestSendingIsGated:
    """execute_send is what runs AFTER confirmation. The gate itself lives in engine.py
    (see test_engine_sms.py) -- these pin what happens once the owner has said yes."""

    def test_a_confirmed_send_queues_exactly_one_message(self, db_path):
        out = json.loads(sms_tools.execute_send(
            db_path, {"to": "Nadia", "message": "I'll pick you up at 8 Thursday."}))
        assert out["queued"] is True and out["to"] == "Nadia"
        pending = cellular.pending_outbound(db_path)
        assert len(pending) == 1
        assert pending[0]["text"] == "I'll pick you up at 8 Thursday."
        assert pending[0]["number"] == "5406540555"

    def test_an_empty_message_is_refused(self, db_path):
        out = json.loads(sms_tools.execute_send(db_path, {"to": "Nadia", "message": "   "}))
        assert "error" in out
        assert cellular.pending_outbound(db_path) == []

    def test_an_unknown_recipient_queues_nothing(self, db_path):
        out = json.loads(sms_tools.execute_send(db_path, {"to": "Beyonce", "message": "hi"}))
        assert "error" in out
        assert cellular.pending_outbound(db_path) == []

    def test_a_long_message_is_trimmed_before_it_costs_five_segments(self, db_path):
        sms_tools.execute_send(db_path, {"to": "Nadia", "message": "word " * 400})
        assert len(cellular.pending_outbound(db_path)[0]["text"]) <= cellular.MAX_SMS_CHARS + 3


def test_only_send_text_is_marked_sensitive():
    """Reading his own messages should never need a confirmation prompt."""
    assert sms_tools.SMS_SENSITIVE_TOOLS == {"send_text"}
    assert "read_text_thread" not in sms_tools.SMS_SENSITIVE_TOOLS
    assert "list_text_contacts" not in sms_tools.SMS_SENSITIVE_TOOLS
