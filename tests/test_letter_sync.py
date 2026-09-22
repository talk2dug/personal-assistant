"""Believing LetterStream about his own mail.

Written from 2026-09-22, where Jarvis told Jack three times in a row that none of his
credit disputes had been sent. All three were in production with real USPS certified
tracking numbers; he had paid for them on LetterStream's own site. Our record only ever
held what Jarvis had done, and a system that can only see its own actions will contradict
the person who took the other ones -- with total confidence, which is the worst part.
"""
import sqlite3

import pytest

from assistant.core import db as core_db, letter_sync, personal_db


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "letters.db")
    core_db.init_db(p)
    personal_db.init_personal_db(p)
    return p


def _letter(path, doc_id="jrv1790083419-0", status="quoted"):
    item = personal_db.create_dispute_item(
        path, 1, bureau="transunion", creditor_name="Comenity",
        item_description="a balance that is not mine", reason="balance is wrong")
    letter = personal_db.create_dispute_letter(
        path, item, letter_text="To Whom It May Concern:",
        recipient_name="TransUnion Consumer Dispute Center",
        recipient_address="P.O. Box 2000", recipient_city="Chester",
        recipient_state="PA", recipient_zip="19016", mail_type="certified",
        quoted_cost="11.01", authcode="abc", job_name=doc_id.rsplit("-", 1)[0],
        doc_id=doc_id)
    with sqlite3.connect(path) as conn:
        # status is constrained to quoted|mailed; being delivered is a date, not a state.
        if status == "delivered":
            conn.execute("UPDATE dispute_letters SET status = 'mailed', "
                         "delivered_at = '2026-09-25' WHERE id = ?", (letter,))
        else:
            conn.execute("UPDATE dispute_letters SET status = ? WHERE id = ?",
                         (status, letter))
    return letter


IN_PRODUCTION = {
    "@attributes": {"type": "docstatus"},
    "doc": {"id": "jrv1790083419-0", "job": "14912191", "batch": "jrv1790083419",
            "code": "0", "status": "Job 14912191 In Production", "cost": "11.01",
            "date": "09/22/2026 4:15 pm", "tracking": "9214890142980497768033",
            "history": "PreAuth - 09/22/2026 6:23 am"},
}


class FakeLetterStream:
    def __init__(self, payload=None, raises=None):
        self._payload = payload if payload is not None else IN_PRODUCTION
        self._raises = raises
        self.asked = []

    def doc_status(self, *doc_ids):
        self.asked.append(doc_ids)
        if self._raises:
            raise self._raises
        return self._payload

    def job_status(self, *job_ids):
        raise AssertionError("jobstatus takes LetterStream's numeric job id, not ours")


class TestReadingTheirSentence:
    """LetterStream answers in prose -- 'Job 14912191 In Production' -- so the state has
    to come out of the sentence."""

    @pytest.mark.parametrize("text,expected", [
        ("Job 14912191 In Production", "mailed"),
        ("Mailed", "mailed"),
        ("In Transit", "mailed"),
        ("Delivered 09/25/2026", "delivered"),
        ("Cancelled by user", "cancelled"),
    ])
    def test_it_reads_the_state(self, text, expected):
        assert letter_sync.read_state(text) == expected

    def test_something_it_does_not_recognise_is_not_guessed(self):
        """A letter wrongly marked mailed starts a 30-day clock against a deadline that
        is not running, which is worse than not knowing."""
        assert letter_sync.read_state("Awaiting something new") is None
        assert letter_sync.read_state("") is None

    def test_delivered_beats_production(self):
        assert letter_sync.read_state("Delivered - was In Production") == "delivered"


class TestTheSync:
    def test_a_letter_he_paid_for_himself_is_found(self, path):
        """The whole failure in one test. He paid on their site; nothing here could see
        it; Jarvis told him it had not been sent."""
        letter = _letter(path)
        out = letter_sync.sync(path, 1, FakeLetterStream())
        assert out["ok"] and len(out["changed"]) == 1
        row = personal_db.get_dispute_letter(path, 1, letter)
        assert row["status"] == "mailed"

    def test_it_keeps_the_certified_tracking_number(self, path):
        letter = _letter(path)
        letter_sync.sync(path, 1, FakeLetterStream())
        row = personal_db.get_dispute_letter(path, 1, letter)
        assert row["tracking_number"] == "9214890142980497768033"

    def test_it_starts_the_thirty_day_clock(self, path):
        """The deadline is the entire point of a certified dispute: a bureau that does
        not answer inside 30 days has to delete the item. Mailed 22 Sep, due 22 Oct."""
        letter = _letter(path)
        letter_sync.sync(path, 1, FakeLetterStream())
        assert personal_db.get_dispute_letter(path, 1, letter)["response_due_at"] == "2026-10-22"

    def test_it_records_when_it_was_really_mailed_not_when_we_noticed(self, path):
        letter = _letter(path)
        letter_sync.sync(path, 1, FakeLetterStream())
        assert personal_db.get_dispute_letter(path, 1, letter)["mailed_at"].startswith("2026-09-22")

    def test_it_asks_by_doc_id_never_by_job_id(self, path):
        """jobstatus wants LetterStream's numeric id; ours is their BATCH id, and asking
        jobstatus with it returns '-928 invalid job id' for a perfectly healthy letter --
        which Jarvis reported to him as evidence nothing had been sent."""
        _letter(path)
        fake = FakeLetterStream()
        letter_sync.sync(path, 1, fake)
        assert fake.asked == [("jrv1790083419-0",)]

    def test_a_letter_that_has_not_moved_is_left_alone(self, path):
        _letter(path)
        payload = {"doc": dict(IN_PRODUCTION["doc"], status="PreAuth", tracking=None)}
        out = letter_sync.sync(path, 1, FakeLetterStream(payload))
        assert out["changed"] == [] and out["unchanged"] == 1

    def test_an_error_row_does_not_become_a_state(self, path):
        letter = _letter(path)
        payload = {"doc": {"id": "jrv1790083419-0", "code": "-928",
                           "details": "Failed: invalid job id"}}
        out = letter_sync.sync(path, 1, FakeLetterStream(payload))
        assert out["changed"] == []
        assert personal_db.get_dispute_letter(path, 1, letter)["status"] == "quoted"

    def test_letterstream_being_unreachable_is_reported_not_raised(self, path):
        _letter(path)
        out = letter_sync.sync(path, 1, FakeLetterStream(raises=RuntimeError("timeout")))
        assert out["ok"] is False and "timeout" in out["why"]

    def test_running_it_twice_changes_nothing_the_second_time(self, path):
        _letter(path)
        letter_sync.sync(path, 1, FakeLetterStream())
        again = letter_sync.sync(path, 1, FakeLetterStream())
        assert again["changed"] == []

    def test_a_delivered_letter_is_not_asked_about_again(self, path):
        _letter(path, status="delivered")
        fake = FakeLetterStream()
        out = letter_sync.sync(path, 1, fake)
        assert out["checked"] == 0 and fake.asked == []

    def test_several_letters_are_asked_about_in_one_call(self, path):
        _letter(path, doc_id="jrv1-0")
        _letter(path, doc_id="jrv2-0")
        fake = FakeLetterStream({"doc": []})
        letter_sync.sync(path, 1, fake)
        assert len(fake.asked) == 1 and len(fake.asked[0]) == 2


class TestAlreadySubmitted:
    """-960 reads as a failure and means the opposite."""

    class Err(Exception):
        def __init__(self, code, detail):
            self.code, self.detail = code, detail

    def test_minus_960_is_the_letter_already_being_in_the_mail(self):
        assert letter_sync.already_submitted(
            self.Err("-960", "Failed: doauth (id not valid or already submitted)"))

    def test_the_wording_alone_is_enough(self):
        assert letter_sync.already_submitted(
            self.Err("-1", "this job was already submitted"))

    def test_a_real_failure_is_still_a_failure(self):
        assert not letter_sync.already_submitted(self.Err("-997", "rate limit exceeded"))
        assert not letter_sync.already_submitted(self.Err("-928", "invalid job id"))
