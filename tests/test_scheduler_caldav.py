from datetime import date, datetime, timedelta, timezone

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


def test_all_day_event_creates_a_reminder_at_midnight_utc(db_path, owner_id):
    """An all-day event's DTSTART (icalendar's dtstart.dt) comes back as a plain
    date, not a datetime -- it has no .tzinfo attribute at all, unlike a timed
    event's start. This crashed sync_calendar every cycle in production the moment
    the owner's calendar had one within the sync window (a birthday, a holiday, a
    multi-day trip) -- reproduces that real failure with a real `date` object."""
    all_day = date.today() + timedelta(days=3)
    client = FakeCalDAVClient([{"uid": "uid-1", "summary": "Anniversary", "start": all_day}])

    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1
    assert reminders[0]["text"] == "Anniversary"
    expected = datetime(all_day.year, all_day.month, all_day.day, tzinfo=timezone.utc).isoformat()
    assert reminders[0]["due_at"] == expected


def test_all_day_event_alongside_a_timed_event_both_sync_cleanly(db_path, owner_id):
    """The real production failure only ever showed up when an all-day event landed in
    the same sync pass as ordinary timed events -- confirms the date-vs-datetime branch
    doesn't disturb the already-working timed-event path."""
    all_day = date.today() + timedelta(days=1)
    timed = _dt()
    client = FakeCalDAVClient([
        {"uid": "uid-allday", "summary": "Holiday", "start": all_day},
        {"uid": "uid-timed", "summary": "Dentist", "start": timed},
    ])

    sync_calendar(client, db_path, CAL_URL, "private", owner_id)

    reminders = {r["caldav_uid"]: r for r in db.list_reminders(db_path, owner_id)}
    assert reminders["uid-allday"]["text"] == "Holiday"
    assert reminders["uid-timed"]["due_at"] == timed.astimezone(timezone.utc).isoformat()
