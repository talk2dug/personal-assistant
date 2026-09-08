"""Verifies the watchdog jobs' wiring: run_task_watchdog (mechanical, notify() directly)
and run_review_watchdog (judgement-needed, routed through handle_message first) --
see docs/watchdog-system-design.md section 3 for why these are split that way. The
underlying due_tasks/stale_review_items query logic has its own tests in
test_personal_db_watchdog.py/test_business_db_watchdog.py; this only checks the wiring,
the same way test_scheduler_mail_junk.py only checks run_mail_junk_scan's wiring.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import business_db, db, personal_db, scheduler


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner(db_path):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    return db.get_user_by_chat_id(db_path, "111")


def _age_review_item(db_path, item_id, hours_old):
    created_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE review_items SET created_at = ? WHERE id = ?", (created_at, item_id))
    conn.commit()
    conn.close()


def test_run_task_watchdog_notifies_and_marks_a_due_task(db_path, owner):
    task_id = personal_db.create_task(db_path, owner["id"], "Call the vet", due_at="2020-01-01T00:00:00+00:00")
    notified = []

    results = scheduler.run_task_watchdog(db_path, lambda chat_id, text: notified.append((chat_id, text)))

    assert results == [{"task_id": task_id, "notified": True}]
    assert notified == [("111", "Task due: Call the vet")]
    assert personal_db.due_tasks(db_path) == []


def test_run_task_watchdog_is_a_noop_with_nothing_due(db_path, owner):
    personal_db.create_task(db_path, owner["id"], "Someday maybe")

    results = scheduler.run_task_watchdog(db_path, lambda chat_id, text: pytest.fail("should not notify"))

    assert results == []


def test_run_review_watchdog_nudges_through_handle_message_and_marks_notified(db_path, owner, monkeypatch):
    item_id = business_db.create_review_item(db_path, owner["id"], "Approve the new logo", kind="art")
    _age_review_item(db_path, item_id, hours_old=3)

    seen = {}

    def fake_handle_message(db_path_arg, llm, owner_id, text, **kwargs):
        seen["owner_id"] = owner_id
        seen["text"] = text
        return "Sir, the new logo design is still waiting on your decision."

    monkeypatch.setattr(scheduler, "handle_message", fake_handle_message)
    notified = []

    results = scheduler.run_review_watchdog(
        db_path, llm=object(), notify=lambda chat_id, text: notified.append((chat_id, text)), hours=2,
    )

    assert results == [{"item_id": item_id, "notified": True}]
    assert seen["owner_id"] == owner["id"]
    assert "Approve the new logo" in seen["text"]
    assert notified == [("111", "Sir, the new logo design is still waiting on your decision.")]
    assert business_db.stale_review_items(db_path, hours=2) == []


def test_run_review_watchdog_is_a_noop_with_nothing_stale(db_path, owner, monkeypatch):
    business_db.create_review_item(db_path, owner["id"], "Approve the new logo")
    monkeypatch.setattr(
        scheduler, "handle_message",
        lambda *a, **k: pytest.fail("should not have run a prompt for a fresh item"),
    )

    results = scheduler.run_review_watchdog(db_path, llm=object(), notify=lambda *a: None, hours=2)

    assert results == []
