from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def users(db_path):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")
    return owner_id, partner_id


def _iso(offset_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def test_private_reminder_not_visible_to_other_user(db_path, users):
    owner_id, partner_id = users
    db.add_reminder(db_path, owner_id, "owner's secret thing", _iso(60), "private")

    assert len(db.list_reminders(db_path, owner_id)) == 1
    assert len(db.list_reminders(db_path, partner_id)) == 0


def test_shared_reminder_visible_to_both_users(db_path, users):
    owner_id, partner_id = users
    db.add_reminder(db_path, owner_id, "date night", _iso(60), "shared")

    assert len(db.list_reminders(db_path, owner_id)) == 1
    assert len(db.list_reminders(db_path, partner_id)) == 1


def test_list_mixes_own_private_with_shared_but_not_others_private(db_path, users):
    owner_id, partner_id = users
    db.add_reminder(db_path, owner_id, "owner private", _iso(60), "private")
    db.add_reminder(db_path, partner_id, "partner private", _iso(60), "private")
    db.add_reminder(db_path, owner_id, "shared one", _iso(60), "shared")

    owner_view = {r["text"] for r in db.list_reminders(db_path, owner_id)}
    partner_view = {r["text"] for r in db.list_reminders(db_path, partner_id)}

    assert owner_view == {"owner private", "shared one"}
    assert partner_view == {"partner private", "shared one"}


def test_scope_filter_narrows_results(db_path, users):
    owner_id, _ = users
    db.add_reminder(db_path, owner_id, "private one", _iso(60), "private")
    db.add_reminder(db_path, owner_id, "shared one", _iso(60), "shared")

    assert [r["text"] for r in db.list_reminders(db_path, owner_id, scope_filter="private")] == ["private one"]
    assert [r["text"] for r in db.list_reminders(db_path, owner_id, scope_filter="shared")] == ["shared one"]


def test_cannot_cancel_someone_elses_private_reminder(db_path, users):
    owner_id, partner_id = users
    reminder_id = db.add_reminder(db_path, owner_id, "owner private", _iso(60), "private")

    assert db.cancel_reminder(db_path, partner_id, reminder_id) is False
    assert len(db.list_reminders(db_path, owner_id)) == 1


def test_can_cancel_own_private_reminder(db_path, users):
    owner_id, _ = users
    reminder_id = db.add_reminder(db_path, owner_id, "owner private", _iso(60), "private")

    assert db.cancel_reminder(db_path, owner_id, reminder_id) is True
    assert len(db.list_reminders(db_path, owner_id)) == 0


def test_either_user_can_cancel_a_shared_reminder(db_path, users):
    owner_id, partner_id = users
    reminder_id = db.add_reminder(db_path, owner_id, "shared one", _iso(60), "shared")

    assert db.cancel_reminder(db_path, partner_id, reminder_id) is True
    assert len(db.list_reminders(db_path, owner_id)) == 0


def test_due_reminders_only_returns_unsent_and_past_due(db_path, users):
    owner_id, _ = users
    past_due_id = db.add_reminder(db_path, owner_id, "should fire", _iso(-5), "private")
    db.add_reminder(db_path, owner_id, "not yet", _iso(60), "private")

    due = db.due_reminders(db_path)
    assert [r["id"] for r in due] == [past_due_id]

    db.mark_reminder_sent(db_path, past_due_id)
    assert db.due_reminders(db_path) == []


def test_conversation_history_is_isolated_per_user(db_path, users):
    owner_id, partner_id = users
    db.add_message(db_path, owner_id, "user", "owner says hi")
    db.add_message(db_path, partner_id, "user", "partner says hi")

    owner_history = db.recent_messages(db_path, owner_id)
    partner_history = db.recent_messages(db_path, partner_id)

    assert [m["content"] for m in owner_history] == ["owner says hi"]
    assert [m["content"] for m in partner_history] == ["partner says hi"]
