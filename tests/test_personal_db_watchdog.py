"""personal_db.py's task-due-date watchdog primitives (due_tasks/mark_task_notified) --
the nullable notified_at marker column scheduler.py's run_task_watchdog polls against
and stamps, same pattern as db.due_reminders()/mark_reminder_sent.
"""
import pytest

from assistant.core import db, personal_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def test_due_tasks_returns_task_past_its_due_date(db_path, owner_id):
    task_id = personal_db.create_task(db_path, owner_id, "Call the vet", due_at="2020-01-01T00:00:00+00:00")

    due = personal_db.due_tasks(db_path, as_of="2020-01-02T00:00:00+00:00")

    assert [t["id"] for t in due] == [task_id]


def test_due_tasks_excludes_a_task_not_yet_due(db_path, owner_id):
    personal_db.create_task(db_path, owner_id, "Renew passport", due_at="2030-01-01T00:00:00+00:00")

    due = personal_db.due_tasks(db_path, as_of="2020-01-02T00:00:00+00:00")

    assert due == []


def test_due_tasks_excludes_a_task_with_no_due_date(db_path, owner_id):
    personal_db.create_task(db_path, owner_id, "Someday maybe")

    due = personal_db.due_tasks(db_path, as_of="2030-01-01T00:00:00+00:00")

    assert due == []


def test_due_tasks_excludes_done_and_dropped_tasks(db_path, owner_id):
    done_id = personal_db.create_task(db_path, owner_id, "Already done", due_at="2020-01-01T00:00:00+00:00")
    dropped_id = personal_db.create_task(db_path, owner_id, "Abandoned", due_at="2020-01-01T00:00:00+00:00")
    personal_db.update_task(db_path, owner_id, done_id, status="done")
    personal_db.update_task(db_path, owner_id, dropped_id, status="dropped")

    due = personal_db.due_tasks(db_path, as_of="2020-01-02T00:00:00+00:00")

    assert due == []


def test_mark_task_notified_stops_it_from_being_due_again(db_path, owner_id):
    task_id = personal_db.create_task(db_path, owner_id, "Call the vet", due_at="2020-01-01T00:00:00+00:00")

    personal_db.mark_task_notified(db_path, task_id)
    due = personal_db.due_tasks(db_path, as_of="2020-01-02T00:00:00+00:00")

    assert due == []


def test_rescheduling_a_notified_tasks_due_date_clears_the_notified_marker(db_path, owner_id):
    """Without this, a task rescheduled after it already fired once would silently
    never notify again -- the marker would still say "already handled" for a due date
    that was never actually notified."""
    task_id = personal_db.create_task(db_path, owner_id, "Call the vet", due_at="2020-01-01T00:00:00+00:00")
    personal_db.mark_task_notified(db_path, task_id)

    personal_db.update_task(db_path, owner_id, task_id, due_at="2020-06-01T00:00:00+00:00")
    due = personal_db.due_tasks(db_path, as_of="2020-06-02T00:00:00+00:00")

    assert [t["id"] for t in due] == [task_id]
