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


# --- excluding Era-detected recurring charges --------------------------------------

def test_era_recurring_charge_not_excluded_by_default(db_path):
    db.upsert_era_recurring_charge(db_path, "key-1", "Moved From Chime", 418.94, "income", "monthly", "2026-09-28")
    charges = db.list_era_recurring_charges(db_path)
    assert charges[0]["excluded"] is False


def test_exclude_era_recurring_charge(db_path):
    db.upsert_era_recurring_charge(db_path, "key-1", "Moved From Chime", 418.94, "income", "monthly", "2026-09-28")
    ok = db.set_era_recurring_charge_excluded(db_path, "key-1", True)
    assert ok is True

    all_charges = db.list_era_recurring_charges(db_path, include_excluded=True)
    assert all_charges[0]["excluded"] is True

    forecast_charges = db.list_era_recurring_charges(db_path, include_excluded=False)
    assert forecast_charges == []


def test_exclusion_survives_a_resync(db_path):
    """The whole point: re-syncing from Era (upsert) must not silently un-exclude
    something the user already flagged as noise."""
    db.upsert_era_recurring_charge(db_path, "key-1", "Moved From Chime", 418.94, "income", "monthly", "2026-09-28")
    db.set_era_recurring_charge_excluded(db_path, "key-1", True)

    # simulate the next scheduled sync re-upserting the same charge with fresh data
    db.upsert_era_recurring_charge(db_path, "key-1", "Moved From Chime", 420.00, "income", "monthly", "2026-10-28")

    charges = db.list_era_recurring_charges(db_path, include_excluded=True)
    assert charges[0]["excluded"] is True  # still excluded
    assert charges[0]["amount"] == 420.00  # but data still refreshed


def test_set_excluded_on_unknown_key_returns_false(db_path):
    assert db.set_era_recurring_charge_excluded(db_path, "nonexistent", True) is False


# --- manual recurring charges -------------------------------------------------------

def test_create_and_list_manual_recurring_charge(db_path, owner_id):
    charge_id = db.create_manual_recurring_charge(
        db_path, owner_id, "Rent (15th)", 925.0, "expense", "monthly_on_day", "2026-09-15",
    )
    charges = db.list_manual_recurring_charges(db_path, owner_id)
    assert len(charges) == 1
    assert charges[0]["id"] == charge_id
    assert charges[0]["description"] == "Rent (15th)"
    assert charges[0]["cadence"] == "monthly_on_day"


def test_manual_recurring_charges_scoped_to_owner(db_path, owner_id):
    partner_id = db.upsert_user(db_path, "222", "GF", "partner")
    db.create_manual_recurring_charge(db_path, owner_id, "Rent", 925.0, "expense", "monthly_on_day", "2026-09-15")
    db.create_manual_recurring_charge(db_path, partner_id, "Car payment", 300.0, "expense", "monthly", "2026-09-01")

    assert [c["description"] for c in db.list_manual_recurring_charges(db_path, owner_id)] == ["Rent"]
    assert [c["description"] for c in db.list_manual_recurring_charges(db_path, partner_id)] == ["Car payment"]


def test_manual_recurring_charge_rejects_invalid_direction(db_path, owner_id):
    with pytest.raises(ValueError):
        db.create_manual_recurring_charge(db_path, owner_id, "X", 1.0, "sideways", "monthly", "2026-09-01")


def test_delete_manual_recurring_charge(db_path, owner_id):
    charge_id = db.create_manual_recurring_charge(
        db_path, owner_id, "Rent", 925.0, "expense", "monthly_on_day", "2026-09-15",
    )
    assert db.delete_manual_recurring_charge(db_path, charge_id) is True
    assert db.list_manual_recurring_charges(db_path, owner_id) == []
    assert db.delete_manual_recurring_charge(db_path, charge_id) is False
