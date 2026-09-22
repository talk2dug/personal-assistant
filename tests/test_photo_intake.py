"""Deciding what a photograph IS before deciding what to do with it.

Jack sent a recipe to the mail pipeline deliberately, to find out whether anything was
reasoning. Nothing was: the mail reader read a cookbook page as a letter, found no sender
and no amount, filed it in a mail table and said nothing back. His rule:

    "If I send a photo Jarvis needs to know what to do with it.
     If it doesn't know then it needs to ask."

So the two things tested hardest are the two halves of that: it routes what it recognises,
and it ASKS rather than guessing when it does not -- including when it is certain the
photo is none of the things it can handle.
"""
import json
import os

import pytest

from assistant.core import (business_db, db as core_db, mail_photo, personal_db,
                            photo_intake)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "intake.db")
    core_db.init_db(p)
    personal_db.init_personal_db(p)
    business_db.init_business_db(p)
    mail_photo.init_mail_photo_db(p)
    return p


@pytest.fixture
def photo(tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"jpegbytes")
    return str(f)


class Bridge:
    def __init__(self, kind="other", confidence="high", summary=None):
        self.kind, self.confidence = kind, confidence
        self.summary = summary or f"A photo of {kind}."
        self.calls = 0

    def run_sync(self, *a, **k):
        self.calls += 1
        return {"status": "done", "error": None, "result": json.dumps(
            {"kind": self.kind, "summary": self.summary,
             "title": self.kind.title(), "confidence": self.confidence})}


class TestWhatHeSaidWins:
    """His idea, and it is the right one: a person telling you what he sent is better
    evidence than a vision model inferring it."""

    @pytest.mark.parametrize("subject,expected", [
        ("art work", "artwork"),
        ("Artwork for the store", "artwork"),
        ("Art", "artwork"),
        ("todays mail", "mail"),
        ("recipe", "recipe"),
        ("fridge check", "pantry"),
    ])
    def test_the_subject_line_routes_it(self, subject, expected):
        assert photo_intake.intent_from_subject(subject) == expected

    @pytest.mark.parametrize("subject", ["a smart cartoon", "started early",
                                         "holiday photos", "", None])
    def test_a_word_that_merely_contains_one_does_not_match(self, subject):
        """'smart' and 'cartoon' both contain 'art'. Matching inside words would file
        his holiday snaps as artwork."""
        assert photo_intake.intent_from_subject(subject) is None

    def test_artwork_is_filed_without_a_single_model_call(self, path, photo, tmp_path):
        bridge = Bridge("mail")          # the model would have said something else
        out = photo_intake.handle(path, 1, bridge, b"x", photo,
                                  subject="Art work for the store",
                                  media_path=str(tmp_path / "media"))
        assert out["kind"] == "artwork" and out["acted"] is True
        assert bridge.calls == 0, "he already said what it was"
        filed = os.listdir(str(tmp_path / "media" / "artwork"))
        assert len(filed) == 1 and filed[0].endswith(".jpg")

    def test_the_filed_name_carries_what_he_called_it(self, path, photo, tmp_path):
        photo_intake.handle(path, 1, Bridge(), b"x", photo,
                            subject="Art work - logo draft v2",
                            media_path=str(tmp_path / "media"))
        assert "logo-draft-v2" in os.listdir(str(tmp_path / "media" / "artwork"))[0].lower()

    def test_a_subject_with_no_keyword_is_still_asked_about(self, path, photo, tmp_path):
        """"Logo draft v2" sounds like artwork to a person and contains none of the
        words the router knows. It must ask rather than quietly filing it somewhere."""
        out = photo_intake.handle(path, 1, Bridge("other", "high"), b"x", photo,
                                  subject="Logo draft v2",
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is False

    def test_two_photos_with_one_subject_do_not_overwrite_each_other(self, path, photo,
                                                                    tmp_path):
        for _ in range(3):
            photo_intake.handle(path, 1, Bridge(), b"x", photo, subject="art",
                                media_path=str(tmp_path / "media"))
        assert len(os.listdir(str(tmp_path / "media" / "artwork"))) == 3


class TestWhenItKnows:
    def test_a_recognised_recipe_is_routed_not_asked_about(self, path, photo, tmp_path):
        out = photo_intake.handle(path, 1, Bridge("recipe", "high"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is True and out["kind"] == "recipe"
        assert "recipe" in out["reply"].lower()

    def test_post_is_read_as_post(self, path, photo, tmp_path, monkeypatch):
        monkeypatch.setattr(mail_photo, "read_photo", lambda b, i: {
            "parsed": True, "sender": "IRS", "summary": "Balance due",
            "amount": 1240.0, "due_date": "2026-10-15", "kind": "tax",
            "action": "Pay it", "deadline_risk": True, "confidence": "high"})
        out = photo_intake.handle(path, 1, Bridge("mail", "high"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is True
        assert "IRS" in out["reply"] and "1,240" in out["reply"]


class TestWhenItDoesNot:
    """The half he actually asked for."""

    def test_an_unrecognisable_photo_is_a_question_not_a_guess(self, path, photo, tmp_path):
        out = photo_intake.handle(path, 1, Bridge("other", "high"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is False
        assert out["reply"].rstrip().endswith("?")

    def test_being_certain_it_is_none_of_them_still_asks(self):
        """Certainty that something is 'other' is exactly the moment to ask."""
        assert photo_intake.should_act(
            {"kind": "other", "confidence": "high"}) is False

    def test_a_hedged_guess_is_not_acted_on(self):
        assert photo_intake.should_act({"kind": "mail", "confidence": "medium"}) is False
        assert photo_intake.should_act({"kind": "mail", "confidence": "high"}) is True

    def test_the_question_names_what_it_thinks_it_saw(self, path, photo, tmp_path):
        """'I don't know what this is' is a worse question than 'is this a recipe?'."""
        out = photo_intake.handle(path, 1, Bridge("mail", "low"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert "mail" in out["reply"] and "not certain" in out["reply"]

    def test_the_question_is_filed_so_it_survives_a_missed_text(self, path, photo, tmp_path):
        out = photo_intake.handle(path, 1, Bridge("other", "low"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["review_id"], "it should be on the Needs You board too"

    def test_a_dead_vision_bridge_asks_rather_than_inventing(self, path, photo, tmp_path):
        out = photo_intake.handle(path, 1, None, b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is False and out["reply"]

    def test_there_is_always_something_to_say_back(self, path, photo, tmp_path):
        """A photo he sent that produces silence is the exact failure this fixes."""
        for bridge in (Bridge("other", "low"), Bridge("recipe", "high"), None):
            out = photo_intake.handle(path, 1, bridge, b"x", photo,
                                      media_path=str(tmp_path / "media"))
            assert out["reply"].strip()


class TestItLearnsWhatHeSends:
    """His words: "He learns the types of things I send and learns what my intentions
    are." Every question Jarvis asks is filed as a review card under photo_intake, so
    answering one is already a recorded ruling -- this is the loop that reads them back.
    """

    def test_past_answers_reach_the_next_triage(self, path, photo, tmp_path,
                                                monkeypatch):
        seen = {}

        class Watching:
            def run_sync(self, lane, kind, prompt, images=None, options=None, fmt=None):
                seen["prompt"] = prompt
                return {"status": "done", "error": None, "result": json.dumps(
                    {"kind": "other", "summary": "?", "title": None,
                     "confidence": "low"})}

        monkeypatch.setattr(photo_intake, "learned_from",
                            lambda db, uid: "\n\nHE HAS TOLD YOU BEFORE: a photo of a "
                                            "canvas is artwork.")
        photo_intake.handle(path, 1, Watching(), b"x", photo,
                            media_path=str(tmp_path / "media"))
        assert "canvas is artwork" in seen["prompt"]

    def test_a_subject_keyword_skips_the_lookup_entirely(self, path, photo, tmp_path,
                                                         monkeypatch):
        """No model call means no prompt to teach -- he already said what it was."""
        called = []
        monkeypatch.setattr(photo_intake, "learned_from",
                            lambda db, uid: called.append(1) or "")
        photo_intake.handle(path, 1, Bridge(), b"x", photo, subject="art work",
                            media_path=str(tmp_path / "media"))
        assert called == []

    def test_losing_the_history_never_breaks_the_read(self, path, photo, tmp_path,
                                                      monkeypatch):
        """Remembering is a nicety; answering is the job."""
        monkeypatch.setattr(
            photo_intake, "review_examples",
            None, raising=False)
        import assistant.core.review_examples as re_mod
        monkeypatch.setattr(re_mod, "build_verdict_briefing",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone")))
        assert photo_intake.learned_from(path, 1) == ""
        out = photo_intake.handle(path, 1, Bridge("recipe", "high"), b"x", photo,
                                  media_path=str(tmp_path / "media"))
        assert out["acted"] is True


class TestWhatCountsAsOneThing:
    """It depends entirely on what the photos ARE, and getting it backwards loses work.

    Several pictures of a letter are pages of ONE letter -- read separately they became
    two bills and two tasks. Several pictures of artwork are SEVERAL designs -- grouped
    together, he sent eleven and got one, because the rule built for the two-sided bill
    was applied to everything.
    """

    def test_eleven_designs_are_eleven_designs(self, path, tmp_path):
        from assistant.core import design_assets

        photos = []
        for i in range(11):
            f = tmp_path / f"art{i}.jpg"
            f.write_bytes(b"jpegbytes")
            photos.append(str(f))
        out = photo_intake.handle(path, 1, Bridge(), [b"x"] * 11, photos,
                                  subject="Art", media_path=str(tmp_path / "media"))
        assert out["kind"] == "artwork" and out["acted"] is True
        assert len(design_assets.catalogue(path, 1)) == 11
        assert len(os.listdir(str(tmp_path / "media" / "artwork"))) == 11
        assert "11 designs" in out["reply"]

    def test_several_pages_of_post_are_still_one_letter(self, path, tmp_path,
                                                        monkeypatch):
        from assistant.core import mail_photo

        monkeypatch.setattr(mail_photo, "read_photo", lambda b, i: {
            "parsed": True, "sender": "County", "summary": "Tax bill",
            "amount": 100.0, "due_date": "2026-10-05", "kind": "bill",
            "action": "Pay it", "deadline_risk": True, "confidence": "high"})
        photos = []
        for i in range(2):
            f = tmp_path / f"page{i}.jpg"
            f.write_bytes(b"jpegbytes")
            photos.append(str(f))
        photo_intake.handle(path, 1, Bridge("mail", "high"), [b"x", b"x"], photos,
                            subject="Bill", media_path=str(tmp_path / "media"))
        assert len(mail_photo.recent(path, 1)) == 1, "one letter, not two"

    def test_one_design_still_reads_naturally(self, path, tmp_path):
        one = tmp_path / "one.jpg"
        one.write_bytes(b"jpegbytes")
        out = photo_intake.handle(path, 1, Bridge(), b"x", str(one),
                                  subject="art", media_path=str(tmp_path / "media"))
        assert "Filed with the artwork" in out["reply"]
        assert "1 designs" not in out["reply"]
