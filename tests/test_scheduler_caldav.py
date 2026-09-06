from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db
from assistant.core.scheduler import sync_calendar


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


CAL_URL = "cal:personal"


class FakeCalDAVClient:
    def __init__(self, events):
        self._events = events  # list of {"uid", "summary", "start"}

    def list_events(self, calendar_url, start, end):
        return self._events


def _dt(offset_minutes=60):
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def test_new_remote_event_creates_a_reminder(db_path, owner_id):
    start = _dt()
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Dentist", "start": start}])

    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1
    assert reminders[0]["text"] == "Dentist"
    assert reminders[0]["caldav_uid"] == "uid-1"


def test_changed_remote_event_updates_the_linked_reminder(db_path, owner_id):
    original_start = _dt(60)
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Dentist", "start": original_start}])
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    new_start = _dt(120)
    client._events = [{"uid": "uid-1", "summary": "Dentist (rescheduled)", "start": new_start}]
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1
    assert reminders[0]["text"] == "Dentist (rescheduled)"
    assert reminders[0]["due_at"] == new_start.astimezone(timezone.utc).isoformat()


def test_deleted_remote_event_cancels_the_reminder(db_path, owner_id):
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Dentist", "start": _dt()}])
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)
    assert len(db.list_reminders(db_path, owner_id)) == 1

    client._events = []  # event removed from Apple Calendar
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    assert db.list_reminders(db_path, owner_id) == []


def test_no_op_when_nothing_changed(db_path, owner_id):
    start = _dt()
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Dentist", "start": start}])
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)
    sync_calendar(client, db_path, CAL_URL, "private", owner_id)  # re-sync, nothing changed

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1  # not duplicated


def test_locally_created_reminder_not_synced_here_is_untouched(db_path, owner_id):
    """A reminder created via Jarvis (not yet linked to any CalDAV event) shouldn't be
    affected by a pull sync that doesn't mention it at all."""
    reminder_id = db.add_reminder(db_path, owner_id, "local only", _dt().isoformat(), "private")
    client = FakeCalDAVClient([])

    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1
    assert reminders[0]["id"] == reminder_id


def test_shared_scope_events_create_shared_reminders(db_path, owner_id):
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Date night", "start": _dt()}])

    sync_calendar(client, db_path, "cal:shared", "shared", owner_id)

    reminders = db.list_reminders(db_path, owner_id)
    assert reminders[0]["scope"] == "shared"
