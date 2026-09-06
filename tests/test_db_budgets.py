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


# --- category spending cache -------------------------------------------------------

def test_upsert_and_list_category_spending(db_path):
    db.upsert_era_category_spending(db_path, "fcat_dining", "last_30_days", "Dining out", 1327.71, 12.8, 63)
    rows = db.list_era_category_spending(db_path, "last_30_days")
    assert len(rows) == 1
    assert rows[0]["label"] == "Dining out"
    assert rows[0]["amount"] == 1327.71
    assert rows[0]["transaction_count"] == 63


def test_category_spending_scoped_by_period(db_path):
    db.upsert_era_category_spending(db_path, "fcat_dining", "this_month", "Dining out", 50.0, 10.0, 3)
    db.upsert_era_category_spending(db_path, "fcat_dining", "last_30_days", "Dining out", 1327.71, 12.8, 63)

    assert db.list_era_category_spending(db_path, "this_month")[0]["amount"] == 50.0
    assert db.list_era_category_spending(db_path, "last_30_days")[0]["amount"] == 1327.71


def test_category_spending_ordered_by_amount_desc(db_path):
    db.upsert_era_category_spending(db_path, "fcat_a", "this_month", "Small", 10.0, None, None)
    db.upsert_era_category_spending(db_path, "fcat_b", "this_month", "Big", 500.0, None, None)
    rows = db.list_era_category_spending(db_path, "this_month")
    assert [r["label"] for r in rows] == ["Big", "Small"]


def test_upsert_category_spending_updates_by_key_and_period(db_path):
    db.upsert_era_category_spending(db_path, "fcat_dining", "this_month", "Dining out", 100.0, None, 5)
    db.upsert_era_category_spending(db_path, "fcat_dining", "this_month", "Dining out", 150.0, None, 7)
    rows = db.list_era_category_spending(db_path, "this_month")
    assert len(rows) == 1
    assert rows[0]["amount"] == 150.0
    assert rows[0]["transaction_count"] == 7


def test_category_spending_rejects_invalid_period(db_path):
    with pytest.raises(ValueError):
        db.upsert_era_category_spending(db_path, "fcat_x", "yesterday", "X", 1.0, None, None)
    with pytest.raises(ValueError):
        db.list_era_category_spending(db_path, "yesterday")


# --- budgets -------------------------------------------------------------------------

def test_create_and_list_budget(db_path, owner_id):
    budget_id = db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 400.0)
    budgets = db.list_budgets(db_path, owner_id)
    assert len(budgets) == 1
    assert budgets[0]["id"] == budget_id
    assert budgets[0]["category_label"] == "Dining out"
    assert budgets[0]["monthly_limit"] == 400.0


def test_creating_budget_for_same_category_updates_limit(db_path, owner_id):
    first_id = db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 400.0)
    second_id = db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 300.0)

    assert first_id == second_id
    budgets = db.list_budgets(db_path, owner_id)
    assert len(budgets) == 1
    assert budgets[0]["monthly_limit"] == 300.0


def test_budgets_scoped_to_owner(db_path, owner_id):
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")
    db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 400.0)
    db.create_budget(db_path, partner_id, "fcat_shopping", "Shopping", 200.0)

    assert [b["category_label"] for b in db.list_budgets(db_path, owner_id)] == ["Dining out"]
    assert [b["category_label"] for b in db.list_budgets(db_path, partner_id)] == ["Shopping"]


def test_delete_budget(db_path, owner_id):
    budget_id = db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 400.0)
    assert db.delete_budget(db_path, budget_id) is True
    assert db.list_budgets(db_path, owner_id) == []
    assert db.delete_budget(db_path, budget_id) is False
