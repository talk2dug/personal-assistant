"""Reading photographed post.

The risky part is not the upload, it is believing the model. A phone photo of a letter is
a bad input -- glare, a fold across the total, half the page in shadow -- and a confident
misread lands a wrong number in his finances. So what is tested hardest here is what the
code refuses to accept: a full account number, a guessed amount, a task raised for junk.
"""
import json

import pytest

from assistant.core import db as core_db, mail_photo, personal_db


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "mail.db")
    core_db.init_db(p)
    personal_db.init_personal_db(p)
    mail_photo.init_mail_photo_db(p)
    return p


class FakeBridge:
    """Stands in for the GPU bridge. run_sync is the only thing read_photo touches."""

    def __init__(self, result, status="done", error=None):
        self.result, self.status, self.error = result, status, error
        self.calls = []

    def run_sync(self, lane, kind, prompt, images=None, options=None):
        self.calls.append({"lane": lane, "kind": kind, "images": images})
        if isinstance(self.result, Exception):
            raise self.result
        return {"status": self.status, "result": self.result, "error": self.error}


GOOD = json.dumps({
    "sender": "Internal Revenue Service", "kind": "tax",
    "summary": "Notice CP14: balance due for tax year 2023.",
    "amount": 1240.00, "due_date": "2026-10-15", "account_ref": "XXXX-XX-6789",
    "action": "Pay or dispute the balance", "deadline_risk": True,
    "confidence": "high", "unreadable": [],
})


class TestReadingTheLetter:
    def test_a_clean_letter_comes_back_structured(self, path):
        out = mail_photo.read_photo(FakeBridge(GOOD), b"jpegbytes")
        assert out["parsed"] is True
        assert out["sender"] == "Internal Revenue Service"
        assert out["kind"] == "tax" and out["amount"] == 1240.0
        assert out["due_date"] == "2026-10-15" and out["deadline_risk"] is True

    def test_a_full_account_number_is_cut_down_to_four(self, path):
        """The prompt asks for four characters. A model that ignores that must not be
        the reason a full account number lands in a database that syncs and backs up."""
        raw = json.dumps({"sender": "Bank", "kind": "bank", "summary": "Statement",
                          "account_ref": "4111 1111 1111 1234", "confidence": "high"})
        assert mail_photo.read_photo(FakeBridge(raw), b"x")["account_ref"] == "1234"

    def test_a_fenced_reply_still_parses(self, path):
        fenced = "Here you go:\n```json\n" + GOOD + "\n```\nHope that helps!"
        assert mail_photo.read_photo(FakeBridge(fenced), b"x")["parsed"] is True

    def test_an_unknown_kind_becomes_other_rather_than_being_trusted(self, path):
        raw = json.dumps({"kind": "ransom note", "summary": "?", "confidence": "high"})
        assert mail_photo.read_photo(FakeBridge(raw), b"x")["kind"] == "other"

    def test_a_missing_confidence_is_treated_as_low_not_high(self, path):
        raw = json.dumps({"sender": "X", "kind": "bill", "summary": "y"})
        assert mail_photo.read_photo(FakeBridge(raw), b"x")["confidence"] == "low"

    def test_a_currency_string_is_read_as_a_number(self, path):
        raw = json.dumps({"kind": "bill", "amount": "$1,240.00", "confidence": "high"})
        assert mail_photo.read_photo(FakeBridge(raw), b"x")["amount"] == 1240.0

    def test_a_reply_with_no_json_fails_loudly(self, path):
        out = mail_photo.read_photo(FakeBridge("I cannot read this image."), b"x")
        assert out["parsed"] is False and out["raw"]

    def test_a_failed_job_never_raises(self, path):
        out = mail_photo.read_photo(FakeBridge(None, status="error", error="gpu busy"), b"x")
        assert out["parsed"] is False and "gpu busy" in out["error"]

    def test_an_exploding_bridge_never_raises(self, path):
        out = mail_photo.read_photo(FakeBridge(RuntimeError("boom")), b"x")
        assert out["parsed"] is False and "boom" in out["error"]

    def test_no_bridge_is_an_answer_not_a_crash(self, path):
        assert mail_photo.read_photo(None, b"x")["parsed"] is False


class TestWhatEarnsATask:
    """A task per envelope turns the list he runs his day from into a pile of junk mail,
    and then he stops reading it. That costs more than a missed flyer."""

    def _reading(self, **kw):
        base = {"parsed": True, "kind": "bill", "action": "Pay it",
                "deadline_risk": False, "confidence": "high"}
        return {**base, **kw}

    def test_a_bill_with_something_to_do_earns_one(self):
        assert mail_photo.should_raise_task(self._reading()) is True

    def test_junk_mail_does_not(self):
        assert mail_photo.should_raise_task(
            self._reading(kind="junk", action="Recycle it")) is False

    def test_nothing_to_do_means_no_task_whatever_the_kind(self):
        assert mail_photo.should_raise_task(self._reading(kind="tax", action=None)) is False

    def test_a_flagged_deadline_earns_one_even_on_a_quiet_kind(self):
        assert mail_photo.should_raise_task(
            self._reading(kind="personal", deadline_risk=True)) is True

    def test_an_unparsed_reading_never_silently_earns_one(self):
        assert mail_photo.should_raise_task({"parsed": False, "action": "x"}) is False

    def test_a_shaky_read_says_so_in_the_task_itself(self):
        text = mail_photo.task_text(self._reading(sender="IRS", confidence="low",
                                                  amount=1240.0, due_date="2026-10-15"))
        assert "check the letter" in text
        assert "$1,240.00" in text and "2026-10-15" in text

    def test_a_confident_read_is_not_hedged(self):
        assert "check the letter" not in mail_photo.task_text(
            self._reading(sender="IRS", confidence="high"))


class TestFiling:
    def test_a_piece_is_stored_and_read_back(self, path):
        reading = mail_photo.read_photo(FakeBridge(GOOD), b"x")
        piece = mail_photo.record(path, 1, reading, photo_path="/tmp/a.jpg")
        found = mail_photo.recent(path, 1)[0]
        assert found["id"] == piece and found["sender"] == "Internal Revenue Service"
        assert found["photo_path"] == "/tmp/a.jpg" and found["status"] == "new"

    def test_the_raw_reply_is_kept_next_to_the_parse(self, path):
        """So a bad parse can be diagnosed without going back to the photograph."""
        reading = mail_photo.read_photo(FakeBridge(GOOD), b"x")
        mail_photo.record(path, 1, reading)
        assert "Internal Revenue Service" in mail_photo.recent(path, 1)[0]["raw"]

    def test_an_unreadable_piece_is_still_filed(self, path):
        """Dropping it silently is how a letter gets lost between the doormat and the
        desk -- which is the whole problem this is meant to solve."""
        reading = mail_photo.read_photo(FakeBridge("nope"), b"x")
        assert mail_photo.record(path, 1, reading, photo_path="/tmp/b.jpg") > 0
        assert mail_photo.recent(path, 1)[0]["photo_path"] == "/tmp/b.jpg"

    def test_another_users_mail_is_not_returned(self, path):
        mail_photo.record(path, 1, mail_photo.read_photo(FakeBridge(GOOD), b"x"))
        assert mail_photo.recent(path, 99) == []

    def test_dealt_with_mail_stops_showing_as_new(self, path):
        piece = mail_photo.record(path, 1, mail_photo.read_photo(FakeBridge(GOOD), b"x"))
        assert mail_photo.set_status(path, 1, piece, "filed") is True
        assert mail_photo.recent(path, 1, status="new") == []
        assert len(mail_photo.recent(path, 1, status="filed")) == 1

    def test_a_nonsense_status_is_refused(self, path):
        piece = mail_photo.record(path, 1, mail_photo.read_photo(FakeBridge(GOOD), b"x"))
        with pytest.raises(ValueError):
            mail_photo.set_status(path, 1, piece, "shredded")

    def test_status_cannot_be_changed_on_someone_elses_mail(self, path):
        piece = mail_photo.record(path, 1, mail_photo.read_photo(FakeBridge(GOOD), b"x"))
        assert mail_photo.set_status(path, 99, piece, "filed") is False
