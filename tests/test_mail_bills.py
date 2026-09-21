"""Bill detection from email content (mail_bills.py) -- project 13's "detects bills and
records amount/due date/recurring status".

Same safety shape as test_mail_triage.py, and for the same reason: the FakeMailClient
below has NO send/archive/delete/mark_read method at all, so if this pass ever grew a
mailbox mutation the tests would fail outright rather than quietly pass. Reading and
recording is the whole feature.

Also pinned here: nothing in this pass writes to manual_recurring_charges. A fuzzy
classifier silently inserting rows the owner's budget projections key off would corrupt
real financial math -- a recurring-looking bill is only ever *proposed* in the review card.
"""
import json
from datetime import date

import pytest

from assistant.core import business_db, db, mail_bills, mail_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    owner_id = db.upsert_user(path, "111", "Dug", "owner")
    return path, owner_id


class FakeMailClient:
    """No send/archive/delete/mark_read on purpose -- see module docstring."""

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
        self.prompts = []

    def chat(self, messages, tools=None, think=False):
        self.prompts.append(messages)
        return {"role": "assistant", "content": self._replies.pop(0)}


def _bill_json(**overrides):
    payload = {
        "is_bill": True, "payee": "City Power & Light", "amount": "$142.53",
        "due_date": "2026-10-01", "due_date_text": "due October 1, 2026",
        "is_recurring": True, "cadence": "monthly", "confidence": "high",
        "reasoning": "Monthly electric statement with a balance due.",
    }
    payload.update(overrides)
    return json.dumps(payload)


NOT_A_BILL_JSON = json.dumps({
    "is_bill": False, "payee": "", "amount": "", "due_date": "", "due_date_text": "",
    "is_recurring": False, "cadence": "", "confidence": "high",
    "reasoning": "A receipt for a payment already made.",
})


def _message(from_addr="billing@citypower.example", subject="Your October statement",
             body="Amount due $142.53 by October 1, 2026."):
    return {"from": from_addr, "to": "me@example.com", "subject": subject,
            "date": "2026-09-13", "body": body}


# --- classify_bill -------------------------------------------------------------------

def test_classify_bill_extracts_amount_due_date_and_recurrence():
    llm = FakeLLM([_bill_json()])
    result = mail_bills.classify_bill(llm, _message())
    assert result["payee"] == "City Power & Light"
    assert result["amount_text"] == "$142.53"
    assert result["amount"] == 142.53
    assert result["due_date"] == "2026-10-01"
    assert result["is_recurring"] is True
    assert result["cadence"] == "monthly"


def test_a_receipt_for_an_already_paid_charge_is_not_a_bill():
    llm = FakeLLM([NOT_A_BILL_JSON])
    assert mail_bills.classify_bill(llm, _message(subject="Payment received")) is None


def test_a_claimed_bill_with_neither_an_amount_nor_a_due_date_is_dropped():
    """"Your statement is ready" with nothing owed and no date is not worth a review card,
    however confidently the model labels it a bill."""
    llm = FakeLLM([_bill_json(amount="", due_date="", due_date_text="")])
    assert mail_bills.classify_bill(llm, _message()) is None


def test_a_bill_with_a_due_date_but_no_stated_amount_is_still_recorded():
    llm = FakeLLM([_bill_json(amount="")])
    result = mail_bills.classify_bill(llm, _message())
    assert result is not None
    assert result["amount_text"] == ""
    assert result["amount"] is None
    assert result["due_date"] == "2026-10-01"


@pytest.mark.parametrize("garbage", ["not json at all", "", '{"is_bill": true}'])
def test_malformed_or_incomplete_llm_output_is_treated_as_not_a_bill(garbage):
    llm = FakeLLM([garbage])
    assert mail_bills.classify_bill(llm, _message()) is None


def test_llm_prose_wrapped_json_is_still_parsed():
    llm = FakeLLM([f"Sure:\n```json\n{_bill_json()}\n```"])
    assert mail_bills.classify_bill(llm, _message())["amount"] == 142.53


def test_automated_senders_are_deliberately_still_classified():
    """The exact inverse of mail_triage's AUTOMATED_SENDER_PATTERNS skip: almost every
    real bill arrives from no-reply@ or billing@, so filtering those out here would filter
    out the entire feature."""
    llm = FakeLLM([_bill_json(), _bill_json(), _bill_json()])
    for addr in ("no-reply@utility.com", "noreply@bank.com", "notifications@insurer.com"):
        assert mail_bills.classify_bill(llm, _message(from_addr=addr)) is not None


# --- amount / due-date normalization (never coerce into false precision) --------------

@pytest.mark.parametrize("raw,expected", [
    ("$142.53", 142.53), ("142.53", 142.53), ("$1,234.56", 1234.56),
    ("USD 89.00", 89.0), ("$0.99", 0.99),
])
def test_an_unambiguous_amount_is_parsed_to_a_real_number(raw, expected):
    assert mail_bills._parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "varies", "$80-$120", "between $80 and $120", "TBD"])
def test_an_ambiguous_amount_stays_text_only(raw):
    """Anything that isn't exactly one number is stored as the model gave it and the
    numeric column is left NULL -- same reasoning meal_plan_shopping_items keeps
    model-reasoned quantities as TEXT rather than inventing precision."""
    assert mail_bills._parse_amount(raw) is None


@pytest.mark.parametrize("raw", ["", "next month", "2026-13-45", "Oct 1", None])
def test_an_unparseable_due_date_is_dropped_rather_than_guessed(raw):
    assert mail_bills._parse_due_date(raw) is None


def test_a_valid_iso_due_date_is_kept():
    assert mail_bills._parse_due_date("2026-10-01") == "2026-10-01"


def test_the_models_raw_date_phrasing_is_preserved_even_when_the_iso_date_is_unusable():
    llm = FakeLLM([_bill_json(due_date="sometime in October", due_date_text="early October")])
    result = mail_bills.classify_bill(llm, _message())
    assert result["due_date"] is None
    assert result["due_date_text"] == "early October"


# --- reminder scheduling --------------------------------------------------------------

def test_a_reminder_lands_ahead_of_the_due_date_not_on_it():
    due_at = mail_bills.reminder_due_at("2026-10-01", today=date(2026, 9, 13))
    assert due_at == "2026-09-28T09:00:00"


def test_an_imminent_bill_is_nudged_today_rather_than_on_its_own_due_date():
    """Due tomorrow: three days ahead is already past, so the nudge is now-ish. It must
    never slide forward onto the due date itself, which would be no warning at all."""
    due_at = mail_bills.reminder_due_at("2026-09-14", today=date(2026, 9, 13))
    assert due_at == "2026-09-13T09:00:00"


@pytest.mark.parametrize("due_date", ["2026-09-13", "2026-09-01", "2019-04-04"])
def test_no_reminder_for_a_due_date_that_is_today_or_already_past(due_date):
    assert mail_bills.reminder_due_at(due_date, today=date(2026, 9, 13)) is None


def test_no_reminder_for_an_implausibly_distant_due_date():
    """A model that hallucinates 2130 shouldn't plant a reminder that sits in the table
    forever -- the bill row is still recorded, it just doesn't get a nudge."""
    assert mail_bills.reminder_due_at("2130-01-01", today=date(2026, 9, 13)) is None


def test_no_reminder_without_a_date_at_all():
    assert mail_bills.reminder_due_at(None, today=date(2026, 9, 13)) is None


# --- run_mail_bill_scan_once ----------------------------------------------------------

def _headers(*uids_and_senders):
    return [{"uid": uid, "from": sender, "subject": "s", "date": "", "unread": True}
            for uid, sender in uids_and_senders]


def test_scan_records_only_the_messages_that_are_bills(db_path):
    path, owner_id = db_path
    headers = _headers(("1", "billing@citypower.example"), ("2", "shop@example.com"))
    bodies = {"1": _message(), "2": _message(subject="Payment received")}
    llm = FakeLLM([_bill_json(), NOT_A_BILL_JSON])

    result = mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(headers, bodies), owner_id, today=date(2026, 9, 13))

    assert result["scanned"] == 2
    assert result["detected"] == 1
    bills = mail_db.list_bills(path, owner_id)
    assert len(bills) == 1
    assert bills[0]["uid"] == "1"
    assert bills[0]["payee"] == "City Power & Light"
    assert bills[0]["amount"] == 142.53
    assert bills[0]["amount_text"] == "$142.53"
    assert bills[0]["due_date"] == "2026-10-01"
    assert bills[0]["is_recurring"] is True
    assert bills[0]["cadence"] == "monthly"
    assert bills[0]["status"] == "detected"


def test_scan_files_a_review_item_for_each_detected_bill(db_path):
    path, owner_id = db_path
    llm = FakeLLM([_bill_json()])

    mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    items = business_db.list_review_items(path, owner_id, status="pending")
    assert len(items) == 1
    assert items[0]["ref_table"] == "email_bills"
    assert items[0]["kind"] == "other"
    assert items[0]["ref_id"] == mail_db.list_bills(path, owner_id)[0]["id"]


def test_a_detected_bill_lands_in_the_personal_review_lane(db_path):
    path, owner_id = db_path
    llm = FakeLLM([_bill_json()])

    mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    items = business_db.list_review_items(path, owner_id, status="pending")
    assert items[0]["pipeline"] == "personal"


def test_a_future_due_date_becomes_a_real_reminder_linked_back_to_the_bill(db_path):
    path, owner_id = db_path
    llm = FakeLLM([_bill_json()])

    result = mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    assert result["reminders"] == 1
    bill = mail_db.list_bills(path, owner_id)[0]
    assert bill["reminder_id"] is not None
    reminders = db.list_reminders(path, owner_id)
    assert len(reminders) == 1
    assert reminders[0]["id"] == bill["reminder_id"]
    assert reminders[0]["due_at"] == "2026-09-28T09:00:00"
    assert "City Power & Light" in reminders[0]["text"]
    assert "$142.53" in reminders[0]["text"]


def test_a_bill_with_no_usable_due_date_is_still_recorded_but_gets_no_reminder(db_path):
    path, owner_id = db_path
    llm = FakeLLM([_bill_json(due_date="", due_date_text="on receipt")])

    result = mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    assert result["detected"] == 1
    assert result["reminders"] == 0
    assert mail_db.list_bills(path, owner_id)[0]["reminder_id"] is None
    assert db.list_reminders(path, owner_id) == []


def test_rerunning_duplicates_nothing_and_never_re_asks_the_llm(db_path):
    """Idempotency across all four side effects: the bill row, the review card, the
    reminder, and the LLM call itself (FakeLLM raises IndexError if asked again)."""
    path, owner_id = db_path
    headers = _headers(("1", "billing@citypower.example"), ("2", "shop@example.com"))
    bodies = {"1": _message(), "2": _message(subject="Payment received")}
    mail = FakeMailClient(headers, bodies)
    llm = FakeLLM([_bill_json(), NOT_A_BILL_JSON])

    first = mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))
    assert (first["scanned"], first["detected"], first["reminders"]) == (2, 1, 1)

    second = mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))
    assert (second["scanned"], second["detected"], second["reminders"]) == (0, 0, 0)
    assert len(mail_db.list_bills(path, owner_id)) == 1
    assert len(business_db.list_review_items(path, owner_id, status="pending")) == 1
    assert len(db.list_reminders(path, owner_id)) == 1


def test_a_message_that_was_not_a_bill_is_never_re_asked_about_either(db_path):
    """The scan ledger records negatives too, so a newsletter that arrives once doesn't
    cost an LLM round trip on every tick for as long as it stays in the inbox."""
    path, owner_id = db_path
    mail = FakeMailClient(_headers(("2", "shop@example.com")), {"2": _message(subject="Payment received")})
    llm = FakeLLM([NOT_A_BILL_JSON])

    mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))
    second = mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))

    assert second == {"scanned": 0, "detected": 0, "reminders": 0, "duplicates": 0}
    assert mail_db.has_scanned_for_bill(path, owner_id, "INBOX", "2") is True


def test_messages_the_junk_scan_already_flagged_are_skipped(db_path):
    """"Recent non-junk mail": the junk pass normally moves junk out of INBOX before this
    runs, but a flagged message whose move failed is still sitting there -- it must not
    get a bill review card on the strength of a scam invoice."""
    path, owner_id = db_path
    mail_db.init_mail_db(path)
    mail_db.log_junk_action(path, "9", "INBOX", "scam@example.com", "INVOICE OVERDUE",
                            8.0, ["urgent"], moved=False)
    mail = FakeMailClient(_headers(("9", "scam@example.com")), {"9": _message()})
    llm = FakeLLM([])  # must never be reached

    result = mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))
    assert result == {"scanned": 0, "detected": 0, "reminders": 0, "duplicates": 0}
    assert mail_db.list_bills(path, owner_id) == []


def test_scan_skips_unreadable_messages_and_retries_them_next_pass(db_path):
    """A transient IMAP read failure must not burn the uid in the ledger -- otherwise one
    bad fetch means that bill is never looked at again."""
    path, owner_id = db_path
    mail = FakeMailClient(_headers(("1", "billing@citypower.example")),
                          {"1": {"error": "no message with uid 1"}})
    llm = FakeLLM([])  # must never be reached

    result = mail_bills.run_mail_bill_scan_once(path, llm, mail, owner_id, today=date(2026, 9, 13))
    assert result == {"scanned": 1, "detected": 0, "reminders": 0, "duplicates": 0}
    assert mail_db.has_scanned_for_bill(path, owner_id, "INBOX", "1") is False


def test_scan_survives_a_classification_error_and_retries_that_uid_later(db_path):
    path, owner_id = db_path
    mail = FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()})

    class BrokenLLM:
        def chat(self, **kwargs):
            raise RuntimeError("boom")

    result = mail_bills.run_mail_bill_scan_once(path, BrokenLLM(), mail, owner_id, today=date(2026, 9, 13))
    assert result == {"scanned": 1, "detected": 0, "reminders": 0, "duplicates": 0}
    assert mail_db.has_scanned_for_bill(path, owner_id, "INBOX", "1") is False


def test_a_recurring_bill_is_proposed_but_never_written_to_manual_recurring_charges(db_path):
    """The owner's budget/projection math keys off manual_recurring_charges -- a fuzzy
    email classifier writing there silently would corrupt real financial projections."""
    path, owner_id = db_path
    llm = FakeLLM([_bill_json()])

    mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    assert db.list_manual_recurring_charges(path, owner_id) == []
    detail = business_db.list_review_items(path, owner_id, status="pending")[0]["detail"]
    assert "recurring" in detail.lower()
    assert "hasn't been added" in detail.lower() or "not been added" in detail.lower()


def test_the_prompt_carries_the_message_and_todays_date_for_relative_due_dates(db_path):
    """"due in 10 days" is only resolvable if the model is told what day it is."""
    path, owner_id = db_path
    llm = FakeLLM([_bill_json()])

    mail_bills.run_mail_bill_scan_once(
        path, llm, FakeMailClient(_headers(("1", "billing@citypower.example")), {"1": _message()}),
        owner_id, today=date(2026, 9, 13))

    user_prompt = llm.prompts[0][1]["content"]
    assert "2026-09-13" in user_prompt
    assert "$142.53" in user_prompt


# --- mail_db storage ------------------------------------------------------------------

def test_bill_status_can_be_moved_through_its_lifecycle(db_path):
    path, owner_id = db_path
    mail_db.init_mail_db(path)
    bill_id = mail_db.create_bill(
        path, owner_id, folder="INBOX", uid="1", from_address="billing@x.com", subject="s",
        received_at="", payee="P", amount_text="$5", amount=5.0, due_date="2026-10-01",
        due_date_text="Oct 1", is_recurring=False, cadence="", confidence="high", reasoning="r")

    assert mail_db.update_bill_status(path, owner_id, bill_id, "confirmed") is True
    assert mail_db.get_bill(path, owner_id, bill_id)["status"] == "confirmed"
    assert mail_db.list_bills(path, owner_id, status="detected") == []
    assert len(mail_db.list_bills(path, owner_id, status="confirmed")) == 1


def test_an_invalid_bill_status_is_refused(db_path):
    path, owner_id = db_path
    mail_db.init_mail_db(path)
    bill_id = mail_db.create_bill(
        path, owner_id, folder="INBOX", uid="1", from_address="b@x.com", subject="s",
        received_at="", payee="P", amount_text="", amount=None, due_date=None,
        due_date_text="", is_recurring=False, cadence="", confidence="", reasoning="")
    with pytest.raises(ValueError):
        mail_db.update_bill_status(path, owner_id, bill_id, "paid-ish")


def test_creating_a_bill_twice_for_the_same_uid_returns_the_same_row(db_path):
    path, owner_id = db_path
    mail_db.init_mail_db(path)
    kwargs = dict(folder="INBOX", uid="1", from_address="b@x.com", subject="s", received_at="",
                  payee="P", amount_text="$5", amount=5.0, due_date=None, due_date_text="",
                  is_recurring=False, cadence="", confidence="", reasoning="")
    first = mail_db.create_bill(path, owner_id, **kwargs)
    second = mail_db.create_bill(path, owner_id, **kwargs)
    assert first == second
    assert len(mail_db.list_bills(path, owner_id)) == 1


# --- scheduler registration ------------------------------------------------------------

class _MailContext:
    def __init__(self, client):
        self.mcp_client = client


def test_the_bill_scan_is_registered_on_its_own_slow_cadence(tmp_path):
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        mail=_MailContext(FakeMailClient([], {})), llm=FakeLLM([]),
        mail_bills_interval_minutes=180,
    )
    try:
        job = started.get_job("mail_bills_agent")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 180 * 60
    finally:
        started.shutdown(wait=False)


def test_no_bill_scan_without_an_llm(tmp_path):
    from assistant.core import scheduler

    path = str(tmp_path / "sched.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    started = scheduler.start(
        path, notify=lambda *a: None, poll_interval_seconds=3600,
        mail=_MailContext(FakeMailClient([], {})),
    )
    try:
        assert started.get_job("mail_bills_agent") is None
    finally:
        started.shutdown(wait=False)


class TestRepeatNotices:
    """Shopify sent the same "a bill payment failed" email every two days. Keyed on the
    email uid alone -- right for "one row per message", wrong for "one row per bill" -- one
    $39 charge became four bills, four reminders nagging him, and $156 on the calendar.
    """

    def test_a_repeat_notice_matches_the_open_bill(self, tmp_path):
        from assistant.core import db as core_db, mail_db

        path = str(tmp_path / "bills.db")
        core_db.init_db(path)
        mail_db.init_mail_db(path)
        core_db.upsert_user(path, "111", "Dug", "owner")
        uid = core_db.get_user_by_chat_id(path, "111")["id"]

        first = mail_db.create_bill(
            path, uid, folder="INBOX", uid="101", from_address="billing@shopify.com",
            subject="A bill payment failed", received_at="2026-09-14", payee="Shopify",
            amount_text="$39.00", amount=39.0, due_date="2026-09-16", due_date_text="",
            is_recurring=True, cadence="monthly", confidence="high", reasoning="")
        found = mail_db.find_open_duplicate(path, uid, "Shopify", 39.0)
        assert found is not None and found["id"] == first

    def test_a_different_amount_is_a_different_bill(self, tmp_path):
        from assistant.core import db as core_db, mail_db

        path = str(tmp_path / "bills.db")
        core_db.init_db(path); mail_db.init_mail_db(path)
        core_db.upsert_user(path, "111", "Dug", "owner")
        uid = core_db.get_user_by_chat_id(path, "111")["id"]
        mail_db.create_bill(path, uid, folder="INBOX", uid="101", from_address="a@b.c",
                            subject="s", received_at="2026-09-14", payee="Shopify",
                            amount_text="$39.00", amount=39.0, due_date=None,
                            due_date_text="", is_recurring=True, cadence="monthly",
                            confidence="high", reasoning="")
        assert mail_db.find_open_duplicate(path, uid, "Shopify", 79.0) is None

    def test_a_bill_with_no_amount_is_never_matched_this_way(self, tmp_path):
        """"Payee with no amount" is too broad to be evidence of anything."""
        from assistant.core import db as core_db, mail_db

        path = str(tmp_path / "bills.db")
        core_db.init_db(path); mail_db.init_mail_db(path)
        core_db.upsert_user(path, "111", "Dug", "owner")
        uid = core_db.get_user_by_chat_id(path, "111")["id"]
        assert mail_db.find_open_duplicate(path, uid, "DreamHost", None) is None

    def test_a_paid_bill_does_not_suppress_the_next_one(self, tmp_path):
        """Next month's genuine invoice is a new bill. Suppressing it would be a worse
        failure than the duplicate this exists to stop."""
        from assistant.core import db as core_db, mail_db

        path = str(tmp_path / "bills.db")
        core_db.init_db(path); mail_db.init_mail_db(path)
        core_db.upsert_user(path, "111", "Dug", "owner")
        uid = core_db.get_user_by_chat_id(path, "111")["id"]
        bill = mail_db.create_bill(path, uid, folder="INBOX", uid="101", from_address="a@b.c",
                                   subject="s", received_at="2026-09-14", payee="Shopify",
                                   amount_text="$39.00", amount=39.0, due_date=None,
                                   due_date_text="", is_recurring=True, cadence="monthly",
                                   confidence="high", reasoning="")
        mail_db.update_bill_status(path, uid, bill, "paid")
        assert mail_db.find_open_duplicate(path, uid, "Shopify", 39.0) is None

    def test_a_due_date_only_moves_forward(self, tmp_path):
        """A dunning notice restates a later date. Taking the newest blindly would let a
        bill walk its own deadline into the future forever."""
        from assistant.core import db as core_db, mail_db

        path = str(tmp_path / "bills.db")
        core_db.init_db(path); mail_db.init_mail_db(path)
        core_db.upsert_user(path, "111", "Dug", "owner")
        uid = core_db.get_user_by_chat_id(path, "111")["id"]
        bill = mail_db.create_bill(path, uid, folder="INBOX", uid="101", from_address="a@b.c",
                                   subject="s", received_at="2026-09-14", payee="Shopify",
                                   amount_text="$39.00", amount=39.0, due_date="2026-09-20",
                                   due_date_text="", is_recurring=True, cadence="monthly",
                                   confidence="high", reasoning="")
        mail_db.note_duplicate_notice(path, bill, "2026-09-18")
        assert mail_db.get_bill(path, uid, bill)["due_date"] == "2026-09-20"
        mail_db.note_duplicate_notice(path, bill, "2026-09-24")
        assert mail_db.get_bill(path, uid, bill)["due_date"] == "2026-09-24"
