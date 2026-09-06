"""Verifies push sync (Jarvis -> Apple Calendar): add_reminder creates a linked event,
cancel_reminder deletes it, and a CalDAV hiccup never breaks the reminder itself."""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeCalDAVClient:
    def __init__(self, fail_create=False, fail_delete=False):
        self.created = []
        self.deleted = []
        self.fail_create = fail_create
        self.fail_delete = fail_delete

    def create_event(self, calendar_url, summary, start, duration_minutes=30):
        if self.fail_create:
            raise RuntimeError("boom")
        uid = f"fake-uid-{len(self.created) + 1}"
        self.created.append((calendar_url, summary, start))
        return uid

    def delete_event(self, calendar_url, uid, near, window_hours=2):
        if self.fail_delete:
            raise RuntimeError("boom")
        self.deleted.append((calendar_url, uid))
        return True


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_calendar(**kwargs):
    client = FakeCalDAVClient(**kwargs)
    return engine.CalendarContext(client=client, personal_calendar="cal:personal", shared_calendar="cal:shared"), client


def _add_reminder_response(text="test reminder", due_at="2026-09-02T09:00:00", scope="private"):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": "add_reminder", "arguments": {"text": text, "due_at": due_at, "scope": scope}}}
    ]}


def test_add_reminder_creates_and_links_caldav_event(db_path, owner_id):
    calendar, client = make_calendar()
    llm = FakeLLM([_add_reminder_response(), {"role": "assistant", "content": "Reminder set."}])

    engine.handle_message(db_path, llm, owner_id, "remind me tomorrow at 9am", calendar=calendar)

    assert len(client.created) == 1
    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1
    linked = db.find_reminder_by_caldav_uid(db_path, "fake-uid-1")
    assert linked is not None
    assert linked["caldav_calendar"] == "cal:personal"


def test_shared_reminder_goes_to_shared_calendar(db_path, owner_id):
    calendar, client = make_calendar()
    llm = FakeLLM([
        _add_reminder_response(scope="shared"),
        {"role": "assistant", "content": "Reminder set."},
    ])

    engine.handle_message(db_path, llm, owner_id, "remind us tomorrow at 9am", calendar=calendar)

    assert client.created[0][0] == "cal:shared"


def test_caldav_create_failure_does_not_break_reminder_creation(db_path, owner_id):
    calendar, client = make_calendar(fail_create=True)
    llm = FakeLLM([_add_reminder_response(), {"role": "assistant", "content": "Reminder set."}])

    reply = engine.handle_message(db_path, llm, owner_id, "remind me tomorrow at 9am", calendar=calendar)

    reminders = db.list_reminders(db_path, owner_id)
    assert len(reminders) == 1  # still created locally despite the CalDAV failure
    assert reminders[0]["caldav_uid"] is None
    assert "set" in reply.lower() or "reminder" in reply.lower()


def test_cancel_reminder_deletes_linked_caldav_event(db_path, owner_id):
    calendar, client = make_calendar()
    reminder_id = db.add_reminder(db_path, owner_id, "test", "2026-09-02T13:00:00+00:00", "private")
    db.set_caldav_link(db_path, reminder_id, "uid-xyz", "cal:personal")

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "cancel_reminder", "arguments": {"reminder_id": reminder_id}}}
        ]},
        {"role": "assistant", "content": "Cancelled."},
    ])

    engine.handle_message(db_path, llm, owner_id, "cancel that reminder", calendar=calendar)

    assert client.deleted == [("cal:personal", "uid-xyz")]


def test_cancel_failure_does_not_break_local_cancellation(db_path, owner_id):
    calendar, client = make_calendar(fail_delete=True)
    reminder_id = db.add_reminder(db_path, owner_id, "test", "2026-09-02T13:00:00+00:00", "private")
    db.set_caldav_link(db_path, reminder_id, "uid-xyz", "cal:personal")

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "cancel_reminder", "arguments": {"reminder_id": reminder_id}}}
        ]},
        {"role": "assistant", "content": "Cancelled."},
    ])

    engine.handle_message(db_path, llm, owner_id, "cancel that reminder", calendar=calendar)

    assert db.list_reminders(db_path, owner_id) == []  # locally cancelled regardless of CalDAV failure


def test_no_calendar_context_means_no_caldav_calls(db_path, owner_id):
    llm = FakeLLM([_add_reminder_response(), {"role": "assistant", "content": "Reminder set."}])

    engine.handle_message(db_path, llm, owner_id, "remind me tomorrow at 9am", calendar=None)

    reminders = db.list_reminders(db_path, owner_id)
    assert reminders[0]["caldav_uid"] is None
