"""email_drafts storage: dedup on (owner, folder, uid), never clobbering an existing
(possibly owner-edited) draft, and the 'edited' status transition that lets the review
surface tell an untouched suggestion from one the owner has already worked on."""
import pytest

from assistant.core import mail_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    mail_db.init_mail_db(path)
    return path


def _make(db_path, owner=1, uid="100", **overrides):
    fields = dict(
        folder="INBOX", uid=uid, from_address="customer@example.com", subject="Question about my order",
        received_at="2026-09-01", category="order_question", reasoning="asks about shipping",
        draft_subject="Re: Question about my order", draft_body="Thanks for reaching out...",
    )
    fields.update(overrides)
    return mail_db.create_draft(db_path, owner, **fields)


def test_create_and_get_draft(db_path):
    draft_id = _make(db_path)
    draft = mail_db.get_draft(db_path, 1, draft_id)
    assert draft["subject"] == "Question about my order"
    assert draft["status"] == "drafted"


def test_create_is_idempotent_per_uid(db_path):
    """Re-scanning the same message must not create a second row or disturb the first."""
    first_id = _make(db_path)
    mail_db.update_draft(db_path, 1, first_id, draft_body="an owner-edited reply")

    second_id = _make(db_path, draft_body="a different LLM draft")

    assert second_id == first_id
    draft = mail_db.get_draft(db_path, 1, first_id)
    assert draft["draft_body"] == "an owner-edited reply"


def test_has_draft(db_path):
    assert mail_db.has_draft(db_path, 1, "INBOX", "100") is False
    _make(db_path)
    assert mail_db.has_draft(db_path, 1, "INBOX", "100") is True


def test_different_uids_get_separate_drafts(db_path):
    id_a = _make(db_path, uid="100")
    id_b = _make(db_path, uid="200")
    assert id_a != id_b
    assert len(mail_db.list_drafts(db_path, 1)) == 2


def test_editing_a_fresh_draft_marks_it_edited(db_path):
    draft_id = _make(db_path)
    mail_db.update_draft(db_path, 1, draft_id, draft_body="rewritten by the owner")
    draft = mail_db.get_draft(db_path, 1, draft_id)
    assert draft["status"] == "edited"
    assert draft["draft_body"] == "rewritten by the owner"


def test_explicit_status_wins_over_auto_edited(db_path):
    draft_id = _make(db_path)
    mail_db.update_draft(db_path, 1, draft_id, draft_body="x", status="dismissed")
    assert mail_db.get_draft(db_path, 1, draft_id)["status"] == "dismissed"


def test_update_rejects_bad_status(db_path):
    draft_id = _make(db_path)
    with pytest.raises(ValueError):
        mail_db.update_draft(db_path, 1, draft_id, status="bogus")


def test_update_missing_draft_returns_false(db_path):
    assert mail_db.update_draft(db_path, 1, 999, status="dismissed") is False


def test_list_drafts_filters_by_status(db_path):
    a = _make(db_path, uid="100")
    _make(db_path, uid="200")
    mail_db.update_draft(db_path, 1, a, status="dismissed")

    drafted = mail_db.list_drafts(db_path, 1, status="drafted")
    assert len(drafted) == 1 and drafted[0]["uid"] == "200"


def test_count_pending_drafts_includes_drafted_and_edited(db_path):
    a = _make(db_path, uid="100")
    _make(db_path, uid="200")
    mail_db.update_draft(db_path, 1, a, draft_body="edited")

    assert mail_db.count_pending_drafts(db_path, 1) == 2


def test_drafts_are_scoped_to_owner(db_path):
    draft_id = _make(db_path, owner=1)
    assert mail_db.get_draft(db_path, 2, draft_id) is None
    assert mail_db.list_drafts(db_path, 2) == []


# --- junk-scan audit log (Phase 3 of project 19: visibility, not a new gate) -------

def test_log_junk_action_and_list(db_path):
    mail_db.log_junk_action(
        db_path, "500", "INBOX", "spammer@example.com", "You won a prize!",
        6.5, ["prize", "click here"], moved=True, moved_to="Junk",
    )
    entries = mail_db.list_junk_log(db_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["uid"] == "500"
    assert entry["from_address"] == "spammer@example.com"
    assert entry["subject"] == "You won a prize!"
    assert entry["score"] == 6.5
    assert entry["reasons"] == ["prize", "click here"]
    assert entry["moved"] is True
    assert entry["moved_to"] == "Junk"


def test_list_junk_log_most_recent_first(db_path):
    mail_db.log_junk_action(db_path, "1", "INBOX", "a@x.com", "first", 5.0, [], moved=True, moved_to="Junk")
    mail_db.log_junk_action(db_path, "2", "INBOX", "b@x.com", "second", 5.0, [], moved=True, moved_to="Junk")
    entries = mail_db.list_junk_log(db_path)
    assert [e["uid"] for e in entries] == ["2", "1"]


def test_list_junk_log_respects_limit(db_path):
    for i in range(5):
        mail_db.log_junk_action(db_path, str(i), "INBOX", "a@x.com", "s", 5.0, [], moved=True, moved_to="Junk")
    assert len(mail_db.list_junk_log(db_path, limit=2)) == 2


def test_log_junk_action_records_a_failed_move_with_no_destination(db_path):
    mail_db.log_junk_action(db_path, "9", "INBOX", "a@x.com", "s", 5.0, [], moved=False)
    entry = mail_db.list_junk_log(db_path)[0]
    assert entry["moved"] is False
    assert entry["moved_to"] is None


def test_list_junk_log_empty_by_default(db_path):
    assert mail_db.list_junk_log(db_path) == []
