"""The SMS channel: /api/cellular/* and core/cellular.py.

The allow-list is the reason most of these exist. Anyone in the world can text the LTE
line, so an unlisted sender reaching handle_message() would be an unauthenticated
stranger driving an assistant that has the owner's mail, money, calendar and front door.
It has to fail closed, it has to survive number reformatting, and a refusal has to be
recorded rather than silently dropped.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import cellular, db
from assistant.web.app import create_app

KEY = "device-key-for-tests"


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    device_api_key: str = KEY
    sms_allowed_numbers: list = field(default_factory=list)
    sms_guest_numbers: list = field(default_factory=list)
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def __init__(self, reply="Both lights are on, sir."):
        self.reply, self.seen = reply, []

    def chat(self, messages, tools=None, think=False):
        self.seen.append(messages)
        return {"role": "assistant", "content": self.reply}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    cellular.init_cellular_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


def _client(db_path, allowed=None, llm=None):
    cfg = FakeConfig(db_path=db_path, sms_allowed_numbers=allowed or [], users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
    ])
    app = create_app(cfg, llm or FakeLLM(), era=None, calendar=None, static_dir=None)
    return TestClient(app)


def _post(client, number="+12027408240", text="turn the kitchen lights on"):
    return client.post("/api/cellular/inbound", json={"number": number, "text": text},
                       headers={"x-device-key": KEY})


# --- the security boundary ----------------------------------------------------

class TestAllowList:
    def test_an_empty_list_accepts_nobody(self, db_path):
        """Fails CLOSED. The alternative failure mode is a stranger with the front door."""
        r = _post(_client(db_path, allowed=[]))
        assert r.status_code == 200
        assert r.json()["accepted"] is False

    def test_an_unlisted_number_is_refused(self, db_path):
        client = _client(db_path, allowed=["+12027408240"])
        assert _post(client, number="+15555550123").json()["accepted"] is False

    def test_a_listed_number_gets_through(self, db_path):
        client = _client(db_path, allowed=["+12027408240"])
        assert _post(client).json()["accepted"] is True

    @pytest.mark.parametrize("arrived_as", [
        "+12027408240", "12027408240", "2027408240", "(202) 740-8240", "202-740-8240",
    ])
    def test_the_same_person_is_recognised_however_the_network_formats_them(
            self, db_path, arrived_as):
        """The same sender arrives formatted differently depending on network, handset
        and whether it came via a short code. An allow-list whose answer depends on
        punctuation is not a security boundary."""
        client = _client(db_path, allowed=["202-740-8240"])
        assert _post(client, number=arrived_as).json()["accepted"] is True

    def test_a_refusal_is_recorded_not_silently_dropped(self, db_path):
        """'Who has been texting this number' is something the owner should be able to
        look at -- especially on a line nobody is supposed to know about yet."""
        client = _client(db_path, allowed=["+12027408240"])
        _post(client, number="+15555550123", text="hello?")
        rows = cellular.recent(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "refused"
        assert rows[0]["number"] == "+15555550123"

    def test_a_refused_message_never_reaches_the_assistant(self, db_path):
        llm = FakeLLM()
        client = _client(db_path, allowed=["+12027408240"], llm=llm)
        _post(client, number="+15555550123", text="unlock the front door")
        assert llm.seen == [], "an unlisted sender must never reach the LLM"

    def test_a_refusal_is_200_so_the_pi_does_not_retry_forever(self, db_path):
        """The forwarder did its job correctly; there is nothing to retry."""
        assert _post(_client(db_path, allowed=[])).status_code == 200


# --- authentication -----------------------------------------------------------

def test_the_device_key_is_required(db_path):
    client = _client(db_path, allowed=["+12027408240"])
    assert client.post("/api/cellular/inbound",
                       json={"number": "+12027408240", "text": "hi"}).status_code == 401
    assert client.get("/api/cellular/outbox").status_code == 401


def test_the_message_log_is_owner_only_not_device_key(db_path):
    """The log is a conversation history, not a device task."""
    client = _client(db_path, allowed=["+12027408240"])
    assert client.get("/api/cellular/messages",
                      headers={"x-device-key": KEY}).status_code == 401


# --- the round trip -----------------------------------------------------------

class TestRoundTrip:
    def test_an_allowed_text_produces_a_queued_reply(self, db_path):
        client = _client(db_path, allowed=["+12027408240"],
                         llm=FakeLLM("Both lights are on, sir."))
        assert _post(client).json()["replied"] is True
        out = client.get("/api/cellular/outbox", headers={"x-device-key": KEY}).json()
        assert len(out["messages"]) == 1
        assert out["messages"][0]["text"] == "Both lights are on, sir."
        assert out["messages"][0]["number"] == "+12027408240"

    def test_the_reply_is_queued_rather_than_returned_inline(self, db_path):
        """A real Jarvis turn can run for minutes; the Pi must not hold a connection
        open for it, and a dropped connection must not lose the answer."""
        client = _client(db_path, allowed=["+12027408240"])
        body = _post(client).json()
        assert "text" not in body and "reply" not in body

    def test_confirming_a_send_clears_it_from_the_outbox(self, db_path):
        client = _client(db_path, allowed=["+12027408240"])
        _post(client)
        msg = client.get("/api/cellular/outbox", headers={"x-device-key": KEY}).json()["messages"][0]
        assert client.post(f"/api/cellular/sent/{msg['id']}", json={"ok": True},
                           headers={"x-device-key": KEY}).status_code == 200
        assert client.get("/api/cellular/outbox",
                          headers={"x-device-key": KEY}).json()["messages"] == []

    def test_a_failed_send_is_recorded_and_not_retried_blindly(self, db_path):
        client = _client(db_path, allowed=["+12027408240"])
        _post(client)
        msg = client.get("/api/cellular/outbox", headers={"x-device-key": KEY}).json()["messages"][0]
        client.post(f"/api/cellular/sent/{msg['id']}",
                    json={"ok": False, "detail": "no carrier"},
                    headers={"x-device-key": KEY})
        row = next(r for r in cellular.recent(db_path) if r["id"] == msg["id"])
        assert row["status"] == "failed" and row["detail"] == "no carrier"

    def test_the_same_message_delivered_twice_is_answered_once(self, db_path):
        """The Pi retrying a delivery it was not sure landed is correct behaviour and
        must not produce a second answer."""
        llm = FakeLLM()
        client = _client(db_path, allowed=["+12027408240"], llm=llm)
        payload = {"number": "+12027408240", "text": "what is due today",
                   "timestamp": "2026-09-15T12:00:00Z"}
        first = client.post("/api/cellular/inbound", json=payload, headers={"x-device-key": KEY})
        second = client.post("/api/cellular/inbound", json=payload, headers={"x-device-key": KEY})
        assert first.json()["accepted"] and second.json().get("duplicate") is True
        assert len(llm.seen) == 1
        out = client.get("/api/cellular/outbox", headers={"x-device-key": KEY}).json()
        assert len(out["messages"]) == 1

    def test_an_assistant_failure_still_texts_back(self, db_path):
        """On this channel silence is indistinguishable from the whole house being down,
        which is exactly the situation it exists for."""
        class Boom(FakeLLM):
            def chat(self, *a, **k):
                raise RuntimeError("engine exploded")
        client = _client(db_path, allowed=["+12027408240"], llm=Boom())
        _post(client)
        out = client.get("/api/cellular/outbox", headers={"x-device-key": KEY}).json()
        assert len(out["messages"]) == 1
        assert "error" in out["messages"][0]["text"].lower()

    def test_missing_fields_are_rejected(self, db_path):
        client = _client(db_path, allowed=["+12027408240"])
        assert client.post("/api/cellular/inbound", json={"number": "+12027408240"},
                           headers={"x-device-key": KEY}).status_code == 400


# --- SMS shaping --------------------------------------------------------------

class TestTrimForSms:
    def test_a_short_reply_is_untouched(self):
        assert cellular.trim_for_sms("Both lights are on, sir.") == "Both lights are on, sir."

    def test_whitespace_is_collapsed(self):
        assert cellular.trim_for_sms("a\n\n  b") == "a b"

    def test_a_long_reply_is_cut_at_a_sentence_and_stays_within_budget(self):
        text = ("This is a sentence that repeats. " * 40)
        out = cellular.trim_for_sms(text)
        assert len(out) <= cellular.MAX_SMS_CHARS
        assert out.endswith(".")

    def test_truncation_is_visible_when_there_is_no_sentence_break(self):
        """A reply that just stops mid-word reads like a bug -- the owner cannot tell a
        cut-off answer from a complete one."""
        out = cellular.trim_for_sms("word " * 300)
        assert out.endswith("...")
        assert len(out) <= cellular.MAX_SMS_CHARS + 3

    def test_an_empty_reply_is_never_queued(self, db_path):
        with pytest.raises(ValueError):
            cellular.queue_outbound(db_path, "+12027408240", "   ")

    def test_the_same_text_twice_is_sent_once(self, db_path):
        """The last gate before his phone.

        Nineteen identical coastal-flood texts in three hours came from a caller that was
        behaving correctly on each pass and could not see the other eighteen. Only this
        function can, so only this function can stop it -- whatever the caller is.
        """
        first = cellular.queue_outbound(db_path, "+12027408240", "Coastal flood watch.")
        again = cellular.queue_outbound(db_path, "+12027408240", "Coastal flood watch.")
        assert again == first, "the repeat should report the message already queued"
        assert len(cellular.pending_outbound(db_path)) == 1

    def test_a_different_message_is_never_suppressed(self, db_path):
        cellular.queue_outbound(db_path, "+12027408240", "Coastal flood watch.")
        cellular.queue_outbound(db_path, "+12027408240", "Tornado warning for Henrico.")
        assert len(cellular.pending_outbound(db_path)) == 2

    def test_the_same_text_to_a_different_person_still_goes(self, db_path):
        cellular.queue_outbound(db_path, "+12027408240", "On my way.")
        cellular.queue_outbound(db_path, "+15406540555", "On my way.")
        assert len(cellular.pending_outbound(db_path)) == 2

    def test_a_repeat_can_be_forced_when_the_repetition_is_the_point(self, db_path):
        cellular.queue_outbound(db_path, "+12027408240", "Confirmed.")
        cellular.queue_outbound(db_path, "+12027408240", "Confirmed.", allow_repeat=True)
        assert len(cellular.pending_outbound(db_path)) == 2


# --- the guest tier -----------------------------------------------------------
#
# Added after a real incident: a friend texted Jarvis, was refused, and adding her to the
# allow-list would have handed her the owner's identity outright -- every allowed SMS runs
# as the owner. A guest talks to Jarvis and reaches none of his life.

GUEST = "+15406540555"


def _guest_client(db_path, llm=None):
    cfg = FakeConfig(db_path=db_path, sms_allowed_numbers=["+12027408240"],
                     sms_guest_numbers=[GUEST], users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
    ])
    app = create_app(cfg, llm or FakeLLM(), era=None, calendar=None, static_dir=None)
    return TestClient(app)


class TestGuestTier:
    def test_a_guest_number_is_accepted(self, db_path):
        r = _post(_guest_client(db_path), number=GUEST, text="hi Jarvis")
        assert r.json()["accepted"] is True

    def test_a_guest_is_marked_as_one_in_the_log(self, db_path):
        _post(_guest_client(db_path), number=GUEST, text="hi Jarvis")
        row = next(r for r in cellular.recent(db_path) if r["direction"] == "inbound")
        assert row["detail"] == "guest"

    def test_a_guest_does_not_run_as_the_owner(self, db_path):
        """The whole point. A guest gets its own user row, so conversation history and
        any privately-scoped read stay isolated by construction."""
        _post(_guest_client(db_path), number=GUEST, text="hi Jarvis")
        owner = db.get_user_by_chat_id(db_path, "111")
        guest_id = cellular.guest_user_id(db_path, GUEST)
        assert guest_id != owner["id"]

    def test_two_guests_never_share_a_conversation(self, db_path):
        assert cellular.guest_user_id(db_path, GUEST) != \
               cellular.guest_user_id(db_path, "+15555550199")

    def test_the_same_guest_keeps_one_identity_across_formats(self, db_path):
        assert cellular.guest_user_id(db_path, "+15406540555") == \
               cellular.guest_user_id(db_path, "(540) 654-0555")

    def test_an_unlisted_number_is_still_refused(self, db_path):
        r = _post(_guest_client(db_path), number="+15555550123", text="hi")
        assert r.json()["accepted"] is False

    def test_the_owner_list_wins_over_the_guest_list(self, db_path):
        """A more specific grant must not be weakened by also appearing on the weaker one."""
        assert cellular.classify("+12027408240", ["+12027408240"], ["+12027408240"]) == "owner"


class TestGuestContexts:
    def test_everything_personal_is_genuinely_absent(self, db_path):
        """Absent, not discouraged -- the model cannot decline to use a tool it never got."""
        ctx = {"era": object(), "mail": object(), "personal": object(),
               "business": object(), "ccxt": object(), "kroger": object(),
               "letterstream": object(), "obsidian": object(), "git_ops": object(),
               "calendar": object(), "phone": object()}
        gated = cellular.guest_contexts(ctx)
        assert all(v is None for v in gated.values()), gated

    def test_a_remote_guest_cannot_touch_the_house(self, db_path):
        """presence.py lets a VOICE-terminal guest use Home Assistant, because they are
        standing in the house and could flip the switch by hand. A guest texting from
        anywhere on earth must not be able to unlock a door."""
        assert cellular.guest_contexts({"home_assistant": object()})["home_assistant"] is None

    def test_harmless_lookups_survive(self, db_path):
        keep = {"recipe": object(), "ticketmaster": object(), "airbnb": object()}
        assert all(v is not None for v in cellular.guest_contexts(keep).values())

    def test_it_is_default_deny_so_a_new_integration_is_not_exposed(self, db_path):
        """A block-list would silently expose the next capability wired into Jarvis on
        the day it shipped."""
        assert cellular.guest_contexts({"some_future_integration": object()}) == \
               {"some_future_integration": None}
