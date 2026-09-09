"""meal_plan_db.py's pay-cycle helper -- get_pay_periods wraps finance.find_pay_periods
with this deployment's real income sources (manual_recurring_charges taking priority over
era_recurring_charge_cache, since manual entries are the owner's own correction of Era's
cruder auto-detection) -- plus the meal-plan CRUD: creating a plan, upserting entries
(re-planning a slot overwrites, never duplicates), the owner-scoping every function
enforces (a bad/foreign meal_plan_id is a quiet no-op, not an exception), and the
inventory-aware shopping list (gathering a plan's recipe ingredients against real
inventory, saving the model's reasoned quantities, matching against Kroger).
"""
from datetime import date

import pytest

from assistant.core import db, kitchen_db, meal_plan_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    kitchen_db.init_kitchen_db(path)
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


# --- meal plan CRUD ----------------------------------------------------------------

def test_create_and_get_meal_plan(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")

    plan = meal_plan_db.get_meal_plan(db_path, owner_id, plan_id)
    assert plan["period_start"] == "2026-09-15"
    assert plan["period_end"] == "2026-09-30"
    assert plan["status"] == "draft"
    assert plan["max_deliveries"] == 2  # default


def test_create_meal_plan_with_custom_max_deliveries(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30", max_deliveries=1)
    assert meal_plan_db.get_meal_plan(db_path, owner_id, plan_id)["max_deliveries"] == 1


def test_get_current_meal_plan_returns_most_recent_draft_or_active(db_path, owner_id):
    meal_plan_db.create_meal_plan(db_path, owner_id, "2026-08-15", "2026-08-31")
    second_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")

    current = meal_plan_db.get_current_meal_plan(db_path, owner_id)
    assert current["id"] == second_id


def test_get_current_meal_plan_skips_completed_and_abandoned(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.finalize_meal_plan(db_path, owner_id, plan_id)  # -> active, still current
    assert meal_plan_db.get_current_meal_plan(db_path, owner_id)["id"] == plan_id


def test_add_meal_plan_entry_and_list(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")

    entry = meal_plan_db.add_meal_plan_entry(
        db_path, owner_id, plan_id, "2026-09-16", "dinner", "Weeknight Chili", servings_planned=4)

    assert entry["title"] == "Weeknight Chili"
    assert entry["source"] == "fresh"  # default

    entries = meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id)
    assert len(entries) == 1
    assert entries[0]["id"] == entry["id"]


def test_add_meal_plan_entry_upserts_on_date_and_meal_type(db_path, owner_id):
    """Re-planning a slot overwrites it in place, never duplicates."""
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Chili")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Tacos")

    entries = meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id)
    assert len(entries) == 1
    assert entries[0]["title"] == "Tacos"


def test_add_meal_plan_entry_different_meal_types_same_day_coexist(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "lunch", "Leftover Chili")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Tacos")

    entries = meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id)
    assert len(entries) == 2


def test_list_meal_plan_entries_ordered_by_date_then_meal_type(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Tacos")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "breakfast", "Oatmeal")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-15", "dinner", "Chili")

    entries = meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id)
    assert [(e["plan_date"], e["meal_type"]) for e in entries] == [
        ("2026-09-15", "dinner"), ("2026-09-16", "breakfast"), ("2026-09-16", "dinner"),
    ]


def test_add_meal_plan_entry_unknown_plan_returns_none(db_path, owner_id):
    assert meal_plan_db.add_meal_plan_entry(db_path, owner_id, 999, "2026-09-16", "dinner", "Chili") is None


def test_remove_meal_plan_entry(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    entry = meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Chili")

    assert meal_plan_db.remove_meal_plan_entry(db_path, owner_id, entry["id"]) is True
    assert meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id) == []
    assert meal_plan_db.remove_meal_plan_entry(db_path, owner_id, entry["id"]) is False


def test_finalize_meal_plan_flips_status(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    assert meal_plan_db.finalize_meal_plan(db_path, owner_id, plan_id) is True
    assert meal_plan_db.get_meal_plan(db_path, owner_id, plan_id)["status"] == "active"


def test_finalize_unknown_meal_plan_returns_false(db_path, owner_id):
    assert meal_plan_db.finalize_meal_plan(db_path, owner_id, 999) is False


def test_get_meal_plan_with_entries_defaults_to_current_plan(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Chili")

    result = meal_plan_db.get_meal_plan_with_entries(db_path, owner_id)
    assert result["plan"]["id"] == plan_id
    assert len(result["entries"]) == 1


def test_get_meal_plan_with_entries_returns_none_when_no_plan_exists(db_path, owner_id):
    assert meal_plan_db.get_meal_plan_with_entries(db_path, owner_id) is None


def test_meal_plan_functions_are_scoped_per_owner(db_path, owner_id):
    """A plan belonging to a different owner must be invisible/unmodifiable."""
    other_owner_id = db.upsert_user(db_path, "222", "Other", "owner")
    plan_id = meal_plan_db.create_meal_plan(db_path, other_owner_id, "2026-09-15", "2026-09-30")

    assert meal_plan_db.get_meal_plan(db_path, owner_id, plan_id) is None
    assert meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id) == []
    assert meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Chili") is None
    assert meal_plan_db.finalize_meal_plan(db_path, owner_id, plan_id) is False


# --- inventory-aware shopping list --------------------------------------------------

def test_gather_meal_plan_ingredients_nets_against_inventory(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(
        db_path, owner_id, "Weeknight Chili",
        [{"name": "ground beef", "quantity": "1", "unit": "lb"}, {"name": "kidney beans", "quantity": "1", "unit": "can"}],
        ["Brown the beef.", "Add beans."],
    )
    kitchen_db.upsert_inventory_item(db_path, owner_id, "Ground Beef", quantity_set=2, unit="lb")
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(
        db_path, owner_id, plan_id, "2026-09-16", "dinner", "Weeknight Chili", recipe_id=recipe_id)

    result = meal_plan_db.gather_meal_plan_ingredients(db_path, owner_id, plan_id)

    assert result["meal_plan_id"] == plan_id
    beef = next(i for i in result["ingredients_needed"] if i["ingredient"]["name"] == "ground beef")
    assert beef["inventory_candidates"] == [{"item": "Ground Beef", "quantity": 2, "unit": "lb", "status": "have"}]
    assert beef["from_meal"] == {"plan_date": "2026-09-16", "meal_type": "dinner", "title": "Weeknight Chili"}
    beans = next(i for i in result["ingredients_needed"] if i["ingredient"]["name"] == "kidney beans")
    assert beans["inventory_candidates"] == []  # nothing on hand plausibly matches


def test_gather_meal_plan_ingredients_skips_freeform_entries(db_path, owner_id):
    """An entry with no recipe_id ('order pizza') contributes nothing to gather."""
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Order pizza")

    result = meal_plan_db.gather_meal_plan_ingredients(db_path, owner_id, plan_id)
    assert result["ingredients_needed"] == []


def test_gather_meal_plan_ingredients_unknown_plan_returns_none(db_path, owner_id):
    assert meal_plan_db.gather_meal_plan_ingredients(db_path, owner_id, 999) is None


def test_save_and_list_meal_plan_shopping_items(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")

    result = meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [
        {"item": "ground beef", "quantity_needed": "1 lb", "quantity_on_hand": "none", "quantity_to_buy": "1 lb", "category": "meat"},
        {"item": "kidney beans", "quantity_to_buy": "1 can", "category": "pantry"},
    ])

    assert len(result["items"]) == 2
    listed = meal_plan_db.list_meal_plan_shopping_items(db_path, owner_id, plan_id)
    assert {i["item"] for i in listed} == {"ground beef", "kidney beans"}
    assert all(i["status"] == "proposed" for i in listed)


def test_save_meal_plan_shopping_items_replaces_wholesale(db_path, owner_id):
    """Regenerating the list is a full do-over, not an incremental merge."""
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "ground beef"}])

    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "kidney beans"}])

    listed = meal_plan_db.list_meal_plan_shopping_items(db_path, owner_id, plan_id)
    assert [i["item"] for i in listed] == ["kidney beans"]


def test_save_meal_plan_shopping_items_unknown_plan_returns_none(db_path, owner_id):
    assert meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, 999, [{"item": "x"}]) is None


def test_save_meal_plan_shopping_items_skips_blank_item_names(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "  "}, {"item": "eggs"}])
    listed = meal_plan_db.list_meal_plan_shopping_items(db_path, owner_id, plan_id)
    assert [i["item"] for i in listed] == ["eggs"]


class FakeKrogerMCPClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def call_tool(self, name, arguments):
        import json
        self.calls.append((name, arguments))
        return {"is_error": False, "content": [json.dumps(self._payload)]}


def test_match_meal_plan_items_to_kroger_fills_product_id_and_on_sale(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "chicken breast"}])
    kroger = FakeKrogerMCPClient({"results": [
        {"term": "chicken breast", "success": True, "data": [{
            "product_id": "p1", "description": "Chicken Breast", "brand": "Kroger",
            "item": {"size": "1 lb"},
            "pricing": {"formatted_regular": "$6.99", "formatted_sale": "$4.99", "on_sale": True},
        }]},
    ]})

    result = meal_plan_db.match_meal_plan_items_to_kroger(db_path, owner_id, plan_id, kroger)

    assert result["matches"][0]["product_id"] == "p1"
    assert result["matches"][0]["on_sale"] is True
    saved = meal_plan_db.list_meal_plan_shopping_items(db_path, owner_id, plan_id)
    assert saved[0]["kroger_product_id"] == "p1"
    assert saved[0]["on_sale"] == 1


def test_match_meal_plan_items_to_kroger_with_no_saved_items_returns_empty(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    result = meal_plan_db.match_meal_plan_items_to_kroger(db_path, owner_id, plan_id, FakeKrogerMCPClient({}))
    assert result == {"meal_plan_id": plan_id, "matches": []}
