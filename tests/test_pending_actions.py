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


def test_no_pending_action_by_default(db_path, owner_id):
    assert db.get_pending_action(db_path, owner_id) is None


def test_created_action_is_awaiting_confirmation(db_path, owner_id):
    action_id = db.create_pending_action(
        db_path, owner_id, "billing__upgrade", {"target_plan": "automate"}
    )
    pending = db.get_pending_action(db_path, owner_id)

    assert pending["id"] == action_id
    assert pending["tool_name"] == "billing__upgrade"
    assert pending["arguments"] == {"target_plan": "automate"}
    assert pending["status"] == "awaiting_confirmation"


def test_confirming_clears_pending_state(db_path, owner_id):
    action_id = db.create_pending_action(db_path, owner_id, "billing__upgrade", {})
    db.resolve_pending_action(db_path, action_id, "confirmed")

    assert db.get_pending_action(db_path, owner_id) is None


def test_cancelling_clears_pending_state(db_path, owner_id):
    action_id = db.create_pending_action(db_path, owner_id, "billing__upgrade", {})
    db.resolve_pending_action(db_path, action_id, "cancelled")

    assert db.get_pending_action(db_path, owner_id) is None


def test_pending_actions_are_isolated_per_user(db_path, owner_id):
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")
    db.create_pending_action(db_path, owner_id, "billing__upgrade", {})

    assert db.get_pending_action(db_path, owner_id) is not None
    assert db.get_pending_action(db_path, partner_id) is None


def test_resolve_rejects_invalid_status(db_path, owner_id):
    action_id = db.create_pending_action(db_path, owner_id, "billing__upgrade", {})
    with pytest.raises(ValueError):
        db.resolve_pending_action(db_path, action_id, "awaiting_confirmation")
