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


# --- savings goals ---------------------------------------------------------------

def test_create_and_list_savings_goal(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Japan trip", 3000.0, "2027-06-01")
    goals = db.list_savings_goals(db_path, owner_id)
    assert len(goals) == 1
    assert goals[0]["id"] == goal_id
    assert goals[0]["name"] == "Japan trip"
    assert goals[0]["target_amount"] == 3000.0
    assert goals[0]["target_date"] == "2027-06-01"


def test_goal_without_target_date_is_allowed(db_path, owner_id):
    db.create_savings_goal(db_path, owner_id, "Emergency fund", 10000.0)
    goals = db.list_savings_goals(db_path, owner_id)
    assert goals[0]["target_date"] is None


def test_goals_scoped_to_owner(db_path, owner_id):
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")
    db.create_savings_goal(db_path, owner_id, "Owner's goal", 100.0)
    db.create_savings_goal(db_path, partner_id, "Partner's goal", 200.0)

    assert [g["name"] for g in db.list_savings_goals(db_path, owner_id)] == ["Owner's goal"]
    assert [g["name"] for g in db.list_savings_goals(db_path, partner_id)] == ["Partner's goal"]


def test_update_savings_goal_partial_fields(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Trip", 1000.0, "2027-01-01")
    ok = db.update_savings_goal(db_path, goal_id, target_amount=1500.0)
    assert ok is True

    goal = db.list_savings_goals(db_path, owner_id)[0]
    assert goal["target_amount"] == 1500.0
    assert goal["name"] == "Trip"  # unchanged
    assert goal["target_date"] == "2027-01-01"  # unchanged


def test_update_savings_goal_can_clear_target_date(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Trip", 1000.0, "2027-01-01")
    db.update_savings_goal(db_path, goal_id, target_date=None)

    goal = db.list_savings_goals(db_path, owner_id)[0]
    assert goal["target_date"] is None


def test_update_nonexistent_goal_returns_false(db_path, owner_id):
    assert db.update_savings_goal(db_path, 9999, name="nope") is False


def test_delete_savings_goal(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Trip", 1000.0)
    assert db.delete_savings_goal(db_path, goal_id) is True
    assert db.list_savings_goals(db_path, owner_id) == []
    assert db.delete_savings_goal(db_path, goal_id) is False  # already gone


# --- Era account cache -------------------------------------------------------------

def test_upsert_and_list_era_accounts(db_path):
    db.upsert_era_account(db_path, "acct-1", "Chime Checking", "checking", 263.77, 263.77)
    accounts = db.list_era_accounts(db_path)
    assert len(accounts) == 1
    assert accounts[0]["name"] == "Chime Checking"
    assert accounts[0]["balance"] == 263.77


def test_upsert_era_account_updates_existing_by_key(db_path):
    db.upsert_era_account(db_path, "acct-1", "Chime Checking", "checking", 100.0, 100.0)
    db.upsert_era_account(db_path, "acct-1", "Chime Checking", "checking", 200.0, 200.0)

    accounts = db.list_era_accounts(db_path)
    assert len(accounts) == 1  # not duplicated
    assert accounts[0]["balance"] == 200.0


# --- Era recurring charge cache ----------------------------------------------------

def test_upsert_and_list_recurring_charges(db_path):
    db.upsert_era_recurring_charge(db_path, "charge-1", "Netflix", 15.99, "expense", "monthly", "2026-09-15")
    charges = db.list_era_recurring_charges(db_path)
    assert len(charges) == 1
    assert charges[0]["description"] == "Netflix"
    assert charges[0]["direction"] == "expense"


def test_recurring_charge_rejects_invalid_direction(db_path):
    with pytest.raises(ValueError):
        db.upsert_era_recurring_charge(db_path, "charge-1", "Netflix", 15.99, "sideways", "monthly", None)


def test_upsert_recurring_charge_updates_existing_by_key(db_path):
    db.upsert_era_recurring_charge(db_path, "charge-1", "Netflix", 15.99, "expense", "monthly", "2026-09-15")
    db.upsert_era_recurring_charge(db_path, "charge-1", "Netflix", 17.99, "expense", "monthly", "2026-10-15")

    charges = db.list_era_recurring_charges(db_path)
    assert len(charges) == 1
    assert charges[0]["amount"] == 17.99
    assert charges[0]["next_expected_date"] == "2026-10-15"
