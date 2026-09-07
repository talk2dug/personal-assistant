"""Draft-reply generation: which messages get drafted, what a scan stores, and that
nothing here ever sends anything -- the FakeMailClient below has no send() at all,
so a stray call to it would fail the test outright rather than silently pass."""
import json

import pytest

from assistant.core import business_db, db, mail_db, mail_triage


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return path, owner_id


class FakeMailClient:
    """No send() method on purpose -- see module docstring."""

    def __init__(self, headers, bodies):
        self._headers = headers
        self._bodies = bodies

    def list_recent(self, folder="INBOX", limit=10):
        return {"emails": self._headers[:limit]}

    def read_message(self, uid, folder="INBOX"):
        return self._bodies[uid]


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)

    def chat(self, messages, tools=None, think=False):
        content = self._replies.pop(0)
        return {"role": "assistant", "content": content}


NEEDS_REPLY_JSON = json.dumps({
    "needs_reply": True, "category": "order_question", "reasoning": "asking about shipping",
    "draft_subject": "Re: Where's my order?", "draft_body": "Thanks for reaching out -- [confirm tracking].",
})
NO_REPLY_JSON = json.dumps({"needs_reply": False, "category": "receipt", "reasoning": "just a receipt",
                            "draft_subject": "", "draft_body": ""})


def _message(from_addr="customer@example.com", subject="Where's my order?", body="When will it ship?"):
    return {"from": from_addr, "to": "me@example.com", "subject": subject, "date": "2026-09-01", "body": body}


# --- classify_and_draft --------------------------------------------------------------

def test_classify_and_draft_returns_a_draft_when_needed():
    llm = FakeLLM([NEEDS_REPLY_JSON])
    result = mail_triage.classify_and_draft(llm, _message())
    assert result["draft_subject"] == "Re: Where's my order?"
    assert "[confirm tracking]" in result["draft_body"]


def test_classify_and_draft_returns_none_when_not_needed():
    llm = FakeLLM([NO_REPLY_JSON])
    assert mail_triage.classify_and_draft(llm, _message(subject="Your receipt")) is None


def test_automated_senders_are_never_drafted_and_never_reach_the_llm():
    class ExplodingLLM:
        def chat(self, **kwargs):
            raise AssertionError("the LLM must never be called for an automated sender")

    for addr in ("no-reply@shop.com", "noreply@shop.com", "mailer-daemon@mail.example.com",
                 "notifications@service.com"):
        assert mail_triage.classify_and_draft(ExplodingLLM(), _message(from_addr=addr)) is None


@pytest.mark.parametrize("garbage", ["not json at all", "", "{\"needs_reply\": true}"])
def test_malformed_or_incomplete_llm_output_is_treated_as_no_reply_needed(garbage):
    llm = FakeLLM([garbage])
    assert mail_triage.classify_and_draft(llm, _message()) is None


def test_llm_prose_wrapped_json_is_still_parsed():
    wrapped = f"Sure, here you go:\n```json\n{NEEDS_REPLY_JSON}\n```"
    llm = FakeLLM([wrapped])
    result = mail_triage.classify_and_draft(llm, _message())
    assert result["needs_reply"] is True


# --- run_mail_triage_once -------------------------------------------------------------

def test_scan_drafts_only_the_messages_that_need_a_reply(db_path):
    path, owner_id = db_path
    headers = [{"uid": "1", "from": "customer@example.com", "subject": "Where's my order?", "date": "", "unread": True},
               {"uid": "2", "from": "shop@example.com", "subject": "Your receipt", "date": "", "unread": True}]
    bodies = {"1": _message(uid="1") if False else _message(), "2": _message(subject="Your receipt")}
    mail = FakeMailClient(headers, bodies)
    llm = FakeLLM([NEEDS_REPLY_JSON, NO_REPLY_JSON])

    result = mail_triage.run_mail_triage_once(path, llm, mail, owner_id)

    assert result == {"scanned": 2, "drafted": 1}
    drafts = mail_db.list_drafts(path, owner_id)
    assert len(drafts) == 1 and drafts[0]["uid"] == "1"


def test_scan_files_a_review_item_for_each_new_draft(db_path):
    path, owner_id = db_path
    headers = [{"uid": "1", "from": "customer@example.com", "subject": "Where's my order?", "date": "", "unread": True}]
    mail = FakeMailClient(headers, {"1": _message()})
    llm = FakeLLM([NEEDS_REPLY_JSON])

    mail_triage.run_mail_triage_once(path, llm, mail, owner_id)

    items = business_db.list_review_items(path, owner_id, status="pending")
    assert len(items) == 1
    assert items[0]["ref_table"] == "email_drafts"
    assert items[0]["kind"] == "other"


def test_scan_skips_uids_that_already_have_a_draft(db_path):
    path, owner_id = db_path
    headers = [{"uid": "1", "from": "customer@example.com", "subject": "Where's my order?", "date": "", "unread": True}]
    mail = FakeMailClient(headers, {"1": _message()})
    llm = FakeLLM([NEEDS_REPLY_JSON])

    first = mail_triage.run_mail_triage_once(path, llm, mail, owner_id)
    assert first == {"scanned": 1, "drafted": 1}

    # A second scan must not call the LLM again (FakeLLM would raise IndexError on an
    # empty queue if it were asked) and must not duplicate the draft or the review card.
    second = mail_triage.run_mail_triage_once(path, llm, mail, owner_id)
    assert second == {"scanned": 0, "drafted": 0}
    assert len(mail_db.list_drafts(path, owner_id)) == 1
    assert len(business_db.list_review_items(path, owner_id, status="pending")) == 1


def test_scan_skips_unreadable_messages(db_path):
    path, owner_id = db_path
    headers = [{"uid": "1", "from": "customer@example.com", "subject": "x", "date": "", "unread": True}]
    mail = FakeMailClient(headers, {"1": {"error": "no message with uid 1"}})
    llm = FakeLLM([])  # must never be reached

    result = mail_triage.run_mail_triage_once(path, llm, mail, owner_id)
    assert result == {"scanned": 1, "drafted": 0}


def test_scan_survives_a_classification_error(db_path):
    path, owner_id = db_path
    headers = [{"uid": "1", "from": "customer@example.com", "subject": "x", "date": "", "unread": True}]
    mail = FakeMailClient(headers, {"1": _message()})

    class BrokenLLM:
        def chat(self, **kwargs):
            raise RuntimeError("boom")

    result = mail_triage.run_mail_triage_once(path, BrokenLLM(), mail, owner_id)
    assert result == {"scanned": 1, "drafted": 0}
