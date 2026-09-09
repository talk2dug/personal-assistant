"""business_db.py's Review-queue staleness watchdog primitives (stale_review_items/
mark_review_item_watchdog_notified) -- the nullable watchdog_notified_at marker column
scheduler.py's run_review_watchdog polls against and stamps, same pattern as
db.due_reminders()/mark_reminder_sent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import business_db, db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def _now_plus(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_stale_review_items_finds_an_old_pending_item(db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Approve the new logo")

    stale = business_db.stale_review_items(db_path, hours=2, as_of=_now_plus(3))

    assert [i["id"] for i in stale] == [item_id]


def test_stale_review_items_excludes_a_recent_item(db_path, owner_id):
    business_db.create_review_item(db_path, owner_id, "Approve the new logo")

    stale = business_db.stale_review_items(db_path, hours=2, as_of=_now_plus(0))

    assert stale == []


def test_stale_review_items_excludes_a_decided_item(db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Approve the new logo")
    business_db.decide_review_item(db_path, owner_id, item_id, "approved")

    stale = business_db.stale_review_items(db_path, hours=2, as_of=_now_plus(3))

    assert stale == []


def test_mark_review_item_watchdog_notified_stops_it_from_being_stale_again(db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Approve the new logo")

    business_db.mark_review_item_watchdog_notified(db_path, item_id)
    stale = business_db.stale_review_items(db_path, hours=2, as_of=_now_plus(3))

    assert stale == []
