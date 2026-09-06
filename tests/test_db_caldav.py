import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def _iso(offset_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def test_migration_is_idempotent(db_path):
    db.init_db(db_path)  # calling init_db again must not raise (columns already exist)
    db.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    assert {"caldav_uid", "caldav_calendar"} <= cols


def test_new_reminder_has_no_caldav_link_by_default(db_path, owner_id):
    reminder_id = db.add_reminder(db_path, owner_id, "test", _iso(60), "private")
    reminders = db.list_reminders(db_path, owner_id)
    assert reminders[0]["caldav_uid"] is None
    assert reminders[0]["caldav_calendar"] is None


def test_set_and_find_caldav_link(db_path, owner_id):
    reminder_id = db.add_reminder(db_path, owner_id, "test", _iso(60), "private")
    db.set_caldav_link(db_path, reminder_id, "uid-123", "https://caldav.icloud.com/cal/")

    found = db.find_reminder_by_caldav_uid(db_path, "uid-123")
    assert found["id"] == reminder_id
    assert found["caldav_uid"] == "uid-123"
    assert found["caldav_calendar"] == "https://caldav.icloud.com/cal/"


def test_find_by_caldav_uid_returns_none_when_unlinked(db_path, owner_id):
    db.add_reminder(db_path, owner_id, "test", _iso(60), "private")
    assert db.find_reminder_by_caldav_uid(db_path, "nonexistent-uid") is None


def test_reminders_linked_to_calendar_excludes_unlinked_and_sent(db_path, owner_id):
    cal_url = "https://caldav.icloud.com/cal/"
    linked_id = db.add_reminder(db_path, owner_id, "linked", _iso(60), "private")
    db.set_caldav_link(db_path, linked_id, "uid-a", cal_url)

    unlinked_id = db.add_reminder(db_path, owner_id, "unlinked", _iso(60), "private")

    sent_id = db.add_reminder(db_path, owner_id, "sent", _iso(60), "private")
    db.set_caldav_link(db_path, sent_id, "uid-b", cal_url)
    db.mark_reminder_sent(db_path, sent_id)

    linked = db.reminders_linked_to_calendar(db_path, cal_url)
    assert [r["id"] for r in linked] == [linked_id]


def test_update_reminder_from_remote_applies_apple_side_edit(db_path, owner_id):
    reminder_id = db.add_reminder(db_path, owner_id, "old text", _iso(60), "private")
    new_due = _iso(120)
    db.update_reminder_from_remote(db_path, reminder_id, "new text", new_due)

    reminders = db.list_reminders(db_path, owner_id)
    assert reminders[0]["text"] == "new text"
    assert reminders[0]["due_at"] == new_due
