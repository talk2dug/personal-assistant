"""Covers the users.role table-rebuild migration (SQLite can't ALTER a CHECK
constraint in place, so widening it to allow 'guest' means recreating the table) and
the guest-user helper presence.py depends on."""
import pytest

from assistant.core import db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


def test_existing_users_and_ids_survive_the_role_migration(db_path):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")

    # Re-running init_db (as every process startup does) must be a no-op on data that
    # already migrated, and must never renumber existing rows -- reminders/conversations
    # elsewhere reference these ids directly.
    db.init_db(db_path)

    assert db.get_user_by_id(db_path, owner_id) == {
        "id": owner_id, "telegram_chat_id": "111", "display_name": "Dug", "role": "owner"}
    assert db.get_user_by_id(db_path, partner_id)["role"] == "partner"


def test_guest_role_is_now_accepted(db_path):
    guest_id = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")
    row = db.get_user_by_id(db_path, guest_id)
    assert row["role"] == "guest"
    assert row["display_name"] == "Guest (touch1)"


def test_guest_lookup_is_idempotent(db_path):
    first = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")
    second = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")
    assert first == second


def test_guests_on_different_devices_are_different_users(db_path):
    touch1_guest = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")
    laptop1_guest = db.get_or_create_guest_user(db_path, "__device_guest__:laptop1", "Guest (laptop1)")
    assert touch1_guest != laptop1_guest


def test_guest_conversation_history_is_isolated(db_path):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    guest_id = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")

    db.add_message(db_path, owner_id, "user", "owner's private message")
    db.add_message(db_path, guest_id, "user", "stranger's message")

    assert [m["content"] for m in db.recent_messages(db_path, guest_id)] == ["stranger's message"]
    assert [m["content"] for m in db.recent_messages(db_path, owner_id)] == ["owner's private message"]


def test_guest_cannot_see_owners_private_reminders(db_path):
    from datetime import datetime, timedelta, timezone

    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    guest_id = db.get_or_create_guest_user(db_path, "__device_guest__:touch1", "Guest (touch1)")
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.add_reminder(db_path, owner_id, "owner's secret errand", due, "private")

    assert db.list_reminders(db_path, guest_id) == []
