"""meal_plan_db.py's pay-cycle helper -- get_pay_periods wraps finance.find_pay_periods
with this deployment's real income sources (manual_recurring_charges taking priority over
era_recurring_charge_cache, since manual entries are the owner's own correction of Era's
cruder auto-detection). Phase 1 only: schema init + the pay-cycle helper, no meal-plan
CRUD yet.
"""
from datetime import date

import pytest

from assistant.core import db, meal_plan_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    meal_plan_db.init_meal_plan_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def test_get_pay_periods_prefers_manual_entries_over_era_when_both_exist(db_path, owner_id):
    """The real, confirmed case: Era's own detection sees one payroll deposit and calls it
    'monthly'; the owner has manually corrected this with the true semi-monthly pattern.
    Both sources must not be unioned -- that would double-count the same real paycheck."""
    db.upsert_era_recurring_charge(
        db_path, "era-payroll", "Hyper Sol Payroll", 4759.86, "income", "monthly", "2026-09-28")
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (15th)", 4759.86, "income", "monthly_on_day", "2026-09-15")
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (last day)", 4759.86, "income", "monthly_on_last_day", "2026-09-30")

    periods = meal_plan_db.get_pay_periods(db_path, owner_id, today=date(2026, 9, 20))

    current = next(p for p in periods if p["is_current"])
    assert current["start_date"] == "2026-09-15"
    assert current["end_date"] == "2026-09-30"
    # Era's own "Hyper Sol Payroll" description never appears -- manual entries won.
    assert "Hyper Sol Payroll" not in (current["start_description"] + current["end_description"])


def test_get_pay_periods_falls_back_to_era_when_no_manual_income_exists(db_path, owner_id):
    db.upsert_era_recurring_charge(
        db_path, "era-payroll", "Hyper Sol Payroll", 4759.86, "income", "monthly_on_day", "2026-09-15")
    db.upsert_era_recurring_charge(
        db_path, "era-rent", "Landlord", 1500.0, "expense", "monthly_on_day", "2026-09-01")

    periods = meal_plan_db.get_pay_periods(db_path, owner_id, today=date(2026, 9, 20))

    # Only one income source (monthly_on_day payroll, occurring Aug 15/Sep 15/Oct 15 within
    # the default +/-45 day window) -- the expense charge must not contribute a boundary of
    # its own (which would otherwise show up as a period starting/ending on the 1st).
    assert all(p["start_date"] != "2026-09-01" and p["end_date"] != "2026-09-01" for p in periods)
    current = next(p for p in periods if p["is_current"])
    assert current["start_date"] == "2026-09-15"
    assert current["start_description"] == "Hyper Sol Payroll"


def test_get_pay_periods_excluded_era_income_is_ignored(db_path, owner_id):
    """A user-excluded Era detection (e.g. a mis-flagged internal transfer) must stay out
    of meal-plan period math the same way it already stays out of cash-flow projections."""
    db.upsert_era_recurring_charge(
        db_path, "era-payroll", "Payroll", 4000.0, "income", "monthly_on_day", "2026-09-15")
    db.upsert_era_recurring_charge(
        db_path, "era-noise", "Internal Transfer", 100.0, "income", "monthly_on_day", "2026-09-20")
    db.set_era_recurring_charge_excluded(db_path, "era-noise", True)

    periods = meal_plan_db.get_pay_periods(db_path, owner_id, today=date(2026, 9, 10))

    # Only Payroll's own monthly_on_day occurrences should exist -- if the excluded
    # transfer leaked in, a spurious Sep 15 -> Sep 20 period would appear.
    assert all(p["end_date"] != "2026-09-20" for p in periods)


def test_get_pay_periods_with_no_income_data_at_all_returns_empty(db_path, owner_id):
    assert meal_plan_db.get_pay_periods(db_path, owner_id, today=date(2026, 9, 20)) == []
